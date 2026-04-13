import ast
import logging
import json
import re
import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session
from open_webui.internal.db import Base, JSONField, get_db, get_db_context
from open_webui.models.tags import TagModel, Tag, Tags
from open_webui.models.folders import Folders
from open_webui.models.chat_messages import ChatMessage, ChatMessages
from open_webui.models.files import Files
from open_webui.utils.misc import sanitize_data_for_db, sanitize_text_for_db

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    String,
    Text,
    JSON,
    Index,
    UniqueConstraint,
)
from sqlalchemy import or_, func, select, and_, text
from sqlalchemy.sql import exists
from sqlalchemy.sql.expression import bindparam

####################
# Chat DB Schema
####################

log = logging.getLogger(__name__)

DEFAULT_CHAT_TAIL_MESSAGES = 50
MAX_CHAT_TAIL_MESSAGES = 200
LEAKED_VISION_SPECIALIST_KEYS = {"observations", "uncertainties", "summary"}
COLLAPSIBLE_DOCUMENT_FILE_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx"}


def _normalize_message_file_ref(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    if lowered in {"null", "undefined"}:
        return ""
    return normalized


def _infer_message_file_name(file_item: dict) -> str:
    for key in ("name", "filename", "fileName"):
        value = _normalize_message_file_ref(file_item.get(key))
        if value:
            return value

    for key in ("url", "path", "output_path", "target_path", "id"):
        value = _normalize_message_file_ref(file_item.get(key))
        if not value:
            continue
        basename = value.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if basename:
            return basename

    return ""


def _collapsible_message_file_key(file_item: dict) -> str:
    name = _infer_message_file_name(file_item).strip().lower()
    if "." not in name:
        return ""

    extension = name.rsplit(".", 1)[-1]
    if extension not in COLLAPSIBLE_DOCUMENT_FILE_EXTENSIONS:
        return ""

    return name


def _message_file_size(file_item: dict) -> Optional[int]:
    value = file_item.get("size", file_item.get("size_bytes"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _collapse_assistant_generated_file_variants(files: list[dict]) -> list[dict]:
    collapsed: list[dict] = []
    index_by_key: dict[str, int] = {}

    for file_item in files:
        key = _collapsible_message_file_key(file_item)
        if not key:
            collapsed.append(file_item)
            continue

        existing_index = index_by_key.get(key)
        if existing_index is None:
            index_by_key[key] = len(collapsed)
            collapsed.append(file_item)
            continue

        existing = collapsed[existing_index]
        existing_size = _message_file_size(existing)
        incoming_size = _message_file_size(file_item)

        if (
            existing_size is not None
            and incoming_size is not None
            and existing_size > incoming_size
        ):
            continue

        collapsed[existing_index] = file_item

    return collapsed


def _extract_leading_json_block(text: str) -> tuple[object, int]:
    if not isinstance(text, str):
        return None, 0

    stripped = text.lstrip()
    leading_whitespace = len(text) - len(stripped)
    if not stripped:
        return None, 0

    if stripped.startswith("```"):
        match = re.match(
            r"^```(?:json)?\s*\n(?P<body>[\s\S]*?)\n```",
            stripped,
            flags=re.IGNORECASE,
        )
        if not match:
            return None, 0
        try:
            payload = json.loads(match.group("body").strip())
        except json.JSONDecodeError:
            return None, 0
        return payload, leading_whitespace + match.end()

    if not stripped.startswith("{"):
        return None, 0

    try:
        payload, end_index = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None, 0

    return payload, leading_whitespace + end_index


def _looks_like_leaked_vision_specialist_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False

    keys = {str(key).strip() for key in payload.keys()}
    if not keys or not keys.issubset(LEAKED_VISION_SPECIALIST_KEYS):
        return False
    if "summary" not in keys or not (keys & {"observations", "uncertainties"}):
        return False

    summary = payload.get("summary")
    observations = payload.get("observations")
    uncertainties = payload.get("uncertainties")

    if not isinstance(summary, str) or not summary.strip():
        return False
    if observations is not None and not isinstance(observations, list):
        return False
    if uncertainties is not None and not isinstance(uncertainties, list):
        return False

    return True


def _strip_leaked_vision_specialist_prefix(text: object) -> object:
    if not isinstance(text, str):
        return text

    payload, end_index = _extract_leading_json_block(text)
    if not _looks_like_leaked_vision_specialist_payload(payload):
        return text

    remainder = text[end_index:].lstrip()
    if not remainder:
        return text
    if remainder.startswith(("```", "{", "[")):
        return text

    return remainder


def _extract_text_from_output_parts(parts: object) -> str:
    if not isinstance(parts, list):
        return ""

    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text_value = part.get("text")
        if text_value is None:
            continue
        chunks.append(text_value if isinstance(text_value, str) else str(text_value))

    return "".join(chunks)


def _parse_tool_output_payload(value: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value

    candidate = value.strip()
    if not candidate:
        return value

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            return value
        return parsed if isinstance(parsed, (dict, list)) else value


def _extract_function_call_output_payload(item: dict) -> object:
    for key in ("output", "result", "content", "text"):
        value = item.get(key)
        if value in (None, "", []):
            continue
        if key == "output" and isinstance(value, list):
            text_value = _extract_text_from_output_parts(value)
            return _parse_tool_output_payload(text_value) if text_value else value
        return _parse_tool_output_payload(value)
    return ""


def _serialize_tool_output_payload(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _is_search_tool_output_payload(value: object) -> bool:
    if not isinstance(value, dict):
        return False

    query = value.get("query")
    if not isinstance(query, str) or not query.strip():
        return False

    return any(key in value for key in ("results", "data", "images", "raw"))


_SEARCH_TOOL_NAMES = {
    "internet_search",
    "web_search",
    "search_web",
    "search",
    "联网搜索",
    "搜索",
}
_SEARCH_TOOL_NAME_LOOKUP = {candidate.lower() for candidate in _SEARCH_TOOL_NAMES}


def _is_search_tool_name(tool_name: str) -> bool:
    return str(tool_name or "").strip().lower() in _SEARCH_TOOL_NAME_LOOKUP


def _normalize_search_tool_io_pair(
    function_call_item: dict, function_output_item: dict
) -> bool:
    tool_name = str(
        function_call_item.get("name") or function_output_item.get("name") or ""
    ).strip()
    if not _is_search_tool_name(tool_name):
        return False

    parsed_payload = _extract_function_call_output_payload(function_output_item)
    if not _is_search_tool_output_payload(parsed_payload):
        return False

    query = str(parsed_payload.get("query") or "").strip()
    if not query:
        return False

    raw_arguments = function_call_item.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    changed = False

    if not str(arguments.get("query") or "").strip():
        arguments = {**arguments, "query": query}
        function_call_item["arguments"] = arguments
        changed = True

    if str(arguments.get("query") or "").strip() != query:
        return changed

    visible_payload = dict(parsed_payload)
    visible_payload.pop("query", None)
    if not visible_payload:
        return changed

    serialized_payload = _serialize_tool_output_payload(visible_payload)
    replacement_output = [{"type": "input_text", "text": serialized_payload}]

    if function_output_item.get("output") != replacement_output:
        function_output_item["output"] = replacement_output
        changed = True

    if "result" in function_output_item and function_output_item.get("result") != serialized_payload:
        function_output_item["result"] = serialized_payload
        changed = True

    return changed


def _normalize_message_tool_output_contract(output: object) -> tuple[object, bool]:
    if not isinstance(output, list):
        return output, False

    changed = False
    function_call_by_key: dict[str, dict] = {}

    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call":
            continue
        item_key = str(item.get("call_id") or item.get("id") or "").strip()
        if item_key:
            function_call_by_key[item_key] = item

    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call_output":
            continue
        item_key = str(item.get("call_id") or item.get("id") or "").strip()
        if not item_key:
            continue
        function_call_item = function_call_by_key.get(item_key)
        if function_call_item and _normalize_search_tool_io_pair(
            function_call_item, item
        ):
            changed = True

    return output, changed


def _sanitize_message_output(output: object) -> tuple[object, bool]:
    if not isinstance(output, list):
        return output, False

    changed = False
    output, contract_changed = _normalize_message_tool_output_contract(output)
    if contract_changed:
        changed = True
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue

        content = item.get("content")
        if not isinstance(content, list):
            continue

        updated_content = []
        item_changed = False
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") in {"text", "input_text", "output_text"}
            ):
                original_text = part.get("text")
                sanitized_text = _strip_leaked_vision_specialist_prefix(original_text)
                if sanitized_text != original_text:
                    updated_content.append({**part, "text": sanitized_text})
                    item_changed = True
                    continue
            updated_content.append(part)

        if item_changed:
            item["content"] = updated_content
            changed = True

    return output, changed


def _serialize_message_output_content(output: object, fallback_content: object) -> str:
    if isinstance(output, list):
        blocks: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            blocks.append(_extract_text_from_output_parts(item.get("content")))
        content = "\n".join(block for block in blocks if block).strip()
        if content:
            return content
    if isinstance(fallback_content, str):
        return _strip_leaked_vision_specialist_prefix(fallback_content).strip()
    return ""


def _sanitize_assistant_message(message: object) -> tuple[object, bool]:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return message, False

    changed = False
    output = message.get("output")
    if isinstance(output, list):
        _, output_changed = _sanitize_message_output(output)
        if output_changed:
            message["output"] = output
            message["content"] = _serialize_message_output_content(
                output, message.get("content", "")
            )
            changed = True

    content = message.get("content")
    if isinstance(content, str):
        sanitized_content = _strip_leaked_vision_specialist_prefix(content)
        if sanitized_content != content:
            message["content"] = sanitized_content
            changed = True

    return message, changed


def _sanitize_chat_message_collection(messages: object) -> tuple[object, bool]:
    changed = False

    if isinstance(messages, dict):
        for message_id, message in messages.items():
            sanitized_message, message_changed = _sanitize_assistant_message(message)
            if message_changed:
                messages[message_id] = sanitized_message
                changed = True
        return messages, changed

    if isinstance(messages, list):
        for index, message in enumerate(messages):
            sanitized_message, message_changed = _sanitize_assistant_message(message)
            if message_changed:
                messages[index] = sanitized_message
                changed = True
        return messages, changed

    return messages, False


def _normalize_chat_history_tool_outputs(chat_payload: object) -> tuple[object, bool]:
    if not isinstance(chat_payload, dict):
        return chat_payload, False

    changed = False
    history = chat_payload.get("history")
    if isinstance(history, dict):
        messages = history.get("messages")
        _, message_collection_changed = _sanitize_chat_message_collection(messages)
        if message_collection_changed:
            changed = True

    top_level_messages = chat_payload.get("messages")
    _, top_level_changed = _sanitize_chat_message_collection(top_level_messages)
    if top_level_changed:
        changed = True

    return chat_payload, changed


def _sanitize_chat_history_specialist_leaks(chat_payload: object) -> tuple[object, bool]:
    if not isinstance(chat_payload, dict):
        return chat_payload, False

    history = chat_payload.get("history")
    if not isinstance(history, dict):
        return chat_payload, False

    messages = history.get("messages")
    if not isinstance(messages, dict):
        return chat_payload, False

    changed = False
    for message_id, message in messages.items():
        sanitized_message, message_changed = _sanitize_assistant_message(message)
        if message_changed:
            messages[message_id] = sanitized_message
            changed = True

    return chat_payload, changed


class Chat(Base):
    __tablename__ = "chat"

    id = Column(String, primary_key=True, unique=True)
    user_id = Column(String)
    title = Column(Text)
    chat = Column(JSON)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)

    share_id = Column(Text, unique=True, nullable=True)
    archived = Column(Boolean, default=False)
    pinned = Column(Boolean, default=False, nullable=True)

    meta = Column(JSON, server_default="{}")
    folder_id = Column(Text, nullable=True)

    __table_args__ = (
        # Performance indexes for common queries
        # WHERE folder_id = ...
        Index("folder_id_idx", "folder_id"),
        # WHERE user_id = ... AND pinned = ...
        Index("user_id_pinned_idx", "user_id", "pinned"),
        # WHERE user_id = ... AND archived = ...
        Index("user_id_archived_idx", "user_id", "archived"),
        # WHERE user_id = ... ORDER BY updated_at DESC
        Index("updated_at_user_id_idx", "updated_at", "user_id"),
        # WHERE folder_id = ... AND user_id = ...
        Index("folder_id_user_id_idx", "folder_id", "user_id"),
    )


class ChatModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    title: str
    chat: dict

    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch

    share_id: Optional[str] = None
    archived: bool = False
    pinned: Optional[bool] = False

    meta: dict = {}
    folder_id: Optional[str] = None


class ChatFile(Base):
    __tablename__ = "chat_file"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text, nullable=False)

    chat_id = Column(Text, ForeignKey("chat.id", ondelete="CASCADE"), nullable=False)
    message_id = Column(Text, nullable=True)
    file_id = Column(Text, ForeignKey("file.id", ondelete="CASCADE"), nullable=False)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("chat_id", "file_id", name="uq_chat_file_chat_file"),
    )


class ChatFileModel(BaseModel):
    id: str
    user_id: str

    chat_id: str
    message_id: Optional[str] = None
    file_id: str

    created_at: int
    updated_at: int

    model_config = ConfigDict(from_attributes=True)


####################
# Forms
####################


class ChatForm(BaseModel):
    chat: dict
    folder_id: Optional[str] = None
    meta: Optional[dict] = {}


class ChatImportForm(ChatForm):
    meta: Optional[dict] = {}
    pinned: Optional[bool] = False
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class ChatsImportForm(BaseModel):
    chats: list[ChatImportForm]


class ChatTitleMessagesForm(BaseModel):
    title: str
    messages: list[dict]


class ChatTitleForm(BaseModel):
    title: str


class ChatResponse(BaseModel):
    id: str
    user_id: str
    title: str
    chat: dict
    updated_at: int  # timestamp in epoch
    created_at: int  # timestamp in epoch
    share_id: Optional[str] = None  # id of the chat to be shared
    archived: bool
    pinned: Optional[bool] = False
    meta: dict = {}
    folder_id: Optional[str] = None


class ChatTitleIdResponse(BaseModel):
    id: str
    title: str
    updated_at: int
    created_at: int


class SharedChatResponse(BaseModel):
    id: str
    title: str
    share_id: Optional[str] = None
    updated_at: int
    created_at: int


class ChatListResponse(BaseModel):
    items: list[ChatModel]
    total: int


class ChatUsageStatsResponse(BaseModel):
    id: str  # chat id

    models: dict = {}  # models used in the chat with their usage counts
    message_count: int  # number of messages in the chat

    history_models: dict = {}  # models used in the chat history with their usage counts
    history_message_count: int  # number of messages in the chat history
    history_user_message_count: int  # number of user messages in the chat history
    history_assistant_message_count: (
        int  # number of assistant messages in the chat history
    )

    average_response_time: (
        float  # average response time of assistant messages in seconds
    )
    average_user_message_content_length: (
        float  # average length of user message contents
    )
    average_assistant_message_content_length: (
        float  # average length of assistant message contents
    )

    tags: list[str] = []  # tags associated with the chat

    last_message_at: int  # timestamp of the last message
    updated_at: int
    created_at: int

    model_config = ConfigDict(extra="allow")


class ChatUsageStatsListResponse(BaseModel):
    items: list[ChatUsageStatsResponse]
    total: int
    model_config = ConfigDict(extra="allow")


class MessageStats(BaseModel):
    id: str
    role: str
    model: Optional[str] = None
    content_length: int
    token_count: Optional[int] = None
    timestamp: Optional[int] = None
    rating: Optional[int] = None  # Derived from message.annotation.rating
    tags: Optional[list[str]] = None  # Derived from message.annotation.tags


class ChatHistoryStats(BaseModel):
    messages: dict[str, MessageStats]
    currentId: Optional[str] = None


class ChatBody(BaseModel):
    history: ChatHistoryStats


class AggregateChatStats(BaseModel):
    average_response_time: float
    average_user_message_content_length: float
    average_assistant_message_content_length: float
    models: dict[str, int]
    message_count: int
    history_models: dict[str, int]
    history_message_count: int
    history_user_message_count: int
    history_assistant_message_count: int


class ChatStatsExport(BaseModel):
    id: str
    user_id: str
    created_at: int
    updated_at: int
    tags: list[str] = []
    stats: AggregateChatStats
    chat: ChatBody


class ChatTable:
    def _clean_null_bytes(self, obj):
        """Recursively remove null bytes from strings in dict/list structures."""
        return sanitize_data_for_db(obj)

    def _ensure_chat_payload_identity(self, chat_payload: object, chat_id: str) -> object:
        if not isinstance(chat_payload, dict):
            return chat_payload

        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return chat_payload

        if chat_payload.get("id") == normalized_chat_id:
            return chat_payload

        return {**chat_payload, "id": normalized_chat_id}

    def _normalize_chat_payload_for_storage(
        self, chat_payload: object, chat_id: str
    ) -> object:
        normalized = self._ensure_chat_payload_identity(chat_payload, chat_id)
        normalized, _ = _normalize_chat_history_tool_outputs(normalized)
        normalized, _ = _sanitize_chat_history_specialist_leaks(normalized)
        return normalized

    def _normalize_file_ref(self, value) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.lower() in {"null", "undefined"}:
            return None
        return normalized

    def _sanitize_files_list(self, files: list) -> tuple[list, bool]:
        if not isinstance(files, list):
            return files, False

        changed = False
        sanitized_files: list = []

        for item in files:
            if isinstance(item, dict):
                item_type = str(item.get("type", "file"))
                normalized_id = self._normalize_file_ref(item.get("id"))
                normalized_url = self._normalize_file_ref(item.get("url"))
                normalized_ref = normalized_url or normalized_id
                has_inline_content = (
                    isinstance(item.get("content"), str)
                    and item.get("content").strip() != ""
                )

                if item_type == "folder" and normalized_id is None:
                    changed = True
                    continue

                if normalized_ref is None and not has_inline_content:
                    changed = True
                    continue

                sanitized_item = {**item}

                if normalized_ref is not None:
                    sanitized_item["url"] = normalized_ref
                    if normalized_id is not None:
                        sanitized_item["id"] = normalized_id
                    elif not (
                        normalized_ref.startswith("http://")
                        or normalized_ref.startswith("https://")
                        or normalized_ref.startswith("data:")
                    ):
                        sanitized_item["id"] = normalized_ref
                else:
                    if "url" in sanitized_item:
                        sanitized_item.pop("url", None)
                    if "id" in sanitized_item:
                        sanitized_item.pop("id", None)

                if sanitized_item != item:
                    changed = True

                sanitized_files.append(sanitized_item)
                continue

            if isinstance(item, str):
                normalized = self._normalize_file_ref(item)
                if normalized is None:
                    changed = True
                    continue
                if normalized != item:
                    changed = True
                sanitized_files.append(normalized)
                continue

            if item is None:
                changed = True
                continue

            sanitized_files.append(item)

        if len(sanitized_files) != len(files):
            changed = True

        return sanitized_files, changed

    def _sanitize_chat_file_refs(self, chat_payload: dict) -> tuple[dict, bool]:
        if not isinstance(chat_payload, dict):
            return chat_payload, False

        changed = False

        if isinstance(chat_payload.get("files"), list):
            sanitized_files, files_changed = self._sanitize_files_list(
                chat_payload.get("files")
            )
            if files_changed:
                chat_payload["files"] = sanitized_files
                changed = True

        history = chat_payload.get("history")
        if isinstance(history, dict):
            messages = history.get("messages")
            if isinstance(messages, dict):
                for message in messages.values():
                    if not isinstance(message, dict):
                        continue

                    if isinstance(message.get("files"), list):
                        sanitized_files, files_changed = self._sanitize_files_list(
                            message.get("files")
                        )
                        if files_changed:
                            message["files"] = sanitized_files
                            changed = True

        return chat_payload, changed

    def _sanitize_chat_row(self, chat_item):
        """
        Clean a Chat SQLAlchemy model's title + chat JSON,
        and return True if anything changed.
        """
        changed = False

        # Clean title
        if chat_item.title:
            cleaned = self._clean_null_bytes(chat_item.title)
            if cleaned != chat_item.title:
                chat_item.title = cleaned
                changed = True

        # Clean JSON
        if chat_item.chat:
            cleaned = self._clean_null_bytes(chat_item.chat)
            cleaned = self._normalize_chat_payload_for_storage(cleaned, chat_item.id)
            cleaned, file_refs_changed = self._sanitize_chat_file_refs(cleaned)
            if file_refs_changed or cleaned != chat_item.chat:
                chat_item.chat = cleaned
                changed = True

        return changed

    def _extract_chat_message_id(self, chat_id: str, composite_id: object) -> Optional[str]:
        if not isinstance(chat_id, str) or not isinstance(composite_id, str):
            return None

        prefix = f"{chat_id}-"
        if not composite_id.startswith(prefix):
            return None

        message_id = composite_id[len(prefix) :].strip()
        return message_id or None

    def _message_file_key(self, file_item: object) -> str:
        if isinstance(file_item, str):
            return self._normalize_file_ref(file_item) or ""

        if not isinstance(file_item, dict):
            return ""

        refs = [
            self._normalize_file_ref(file_item.get("id")),
            self._normalize_file_ref(file_item.get("url")),
            self._normalize_file_ref(
                file_item.get("path")
                or file_item.get("output_path")
                or file_item.get("target_path")
                or file_item.get("file_path")
            ),
            self._normalize_file_ref(
                file_item.get("name")
                or file_item.get("filename")
                or file_item.get("fileName")
            ),
        ]
        size = _message_file_size(file_item)
        refs.append(str(size) if size is not None else "")
        return "||".join(item or "" for item in refs)

    def _message_file_from_file_model(self, file_model) -> dict:
        file_meta = file_model.meta or {}
        content_type = (
            str(file_meta.get("content_type") or "application/octet-stream").strip()
            or "application/octet-stream"
        )
        name = str(file_meta.get("name") or file_model.filename or "").strip()

        file_item = {
            "type": "image" if content_type.startswith("image/") else "file",
            "id": file_model.id,
            "url": f"/api/v1/files/{file_model.id}/content",
            "name": name or file_model.id,
            "filename": str(file_model.filename or name or file_model.id),
            "content_type": content_type,
            "status": "uploaded",
            "error": "",
            "file": {
                "id": file_model.id,
                "user_id": file_model.user_id,
                "hash": file_model.hash,
                "filename": file_model.filename,
                "data": file_model.data,
                "meta": file_meta,
                "created_at": file_model.created_at,
                "updated_at": file_model.updated_at,
            },
        }

        size = file_meta.get("size")
        if isinstance(size, bool):
            size = None
        elif isinstance(size, float):
            size = int(size)
        elif isinstance(size, str):
            try:
                size = int(size)
            except ValueError:
                size = None

        if size is not None:
            file_item["size"] = size

        return file_item

    def _merge_message_files(
        self, existing_files: object, supplemental_files: list[dict], role: object
    ) -> list:
        merged_candidates: list = []

        if isinstance(existing_files, list):
            merged_candidates.extend(existing_files)
        if isinstance(supplemental_files, list):
            merged_candidates.extend(supplemental_files)

        sanitized_files, _ = self._sanitize_files_list(merged_candidates)

        merged_files: list = []
        seen_keys: set[str] = set()
        for file_item in sanitized_files:
            dedupe_key = self._message_file_key(file_item)
            if dedupe_key and dedupe_key in seen_keys:
                continue
            if dedupe_key:
                seen_keys.add(dedupe_key)
            merged_files.append(file_item)

        if (
            str(role or "").strip().lower() == "assistant"
            and merged_files
            and all(isinstance(item, dict) for item in merged_files)
        ):
            merged_files = _collapse_assistant_generated_file_variants(merged_files)

        return merged_files

    def _message_output_item_key(self, item: object) -> str:
        if not isinstance(item, dict):
            return ""

        item_type = str(item.get("type") or "").strip()
        if not item_type:
            return ""

        call_id = str(item.get("call_id") or "").strip()
        if call_id:
            return f"{item_type}:{call_id}"

        item_id = str(item.get("id") or "").strip()
        if item_id:
            return f"{item_type}:{item_id}"

        return ""

    def _message_output_has_structured_items(self, output: object) -> bool:
        if not isinstance(output, list):
            return False

        return any(
            isinstance(item, dict) and item.get("type") != "message" for item in output
        )

    def _merge_message_output_items(
        self, existing_item: object, incoming_item: object
    ) -> object:
        if not isinstance(existing_item, dict):
            return incoming_item
        if not isinstance(incoming_item, dict):
            return existing_item

        merged_item = {**existing_item, **incoming_item}

        existing_status = str(existing_item.get("status") or "").strip().lower()
        incoming_status = str(incoming_item.get("status") or "").strip().lower()
        if existing_status in {"completed", "success", "error", "timeout"} and (
            not incoming_status or incoming_status == "in_progress"
        ):
            merged_item["status"] = existing_item.get("status")

        for key in ("content", "output", "summary"):
            existing_value = existing_item.get(key)
            incoming_value = incoming_item.get(key)
            if incoming_value in (None, "", []):
                if existing_value not in (None, "", []):
                    merged_item[key] = existing_value
            elif isinstance(existing_value, list) and isinstance(incoming_value, list):
                if len(existing_value) > len(incoming_value):
                    merged_item[key] = existing_value

        existing_files = existing_item.get("files")
        incoming_files = incoming_item.get("files")
        if isinstance(existing_files, list) or isinstance(incoming_files, list):
            merged_item["files"] = self._merge_message_files(
                existing_files,
                incoming_files if isinstance(incoming_files, list) else [],
                "assistant",
            )

        return merged_item

    def _merge_message_output(
        self,
        existing_output: object,
        incoming_output: object,
        fallback_content: object = "",
    ) -> object:
        if not isinstance(existing_output, list):
            return incoming_output
        if not isinstance(incoming_output, list):
            return existing_output

        merged_output = json.loads(json.dumps(existing_output, ensure_ascii=False))
        incoming_messages: list[dict] = []
        existing_index_by_key: dict[str, int] = {}

        for index, item in enumerate(merged_output):
            key = self._message_output_item_key(item)
            if key:
                existing_index_by_key[key] = index

        for item in incoming_output:
            if not isinstance(item, dict):
                continue

            if item.get("type") == "message" and item.get("role") == "assistant":
                incoming_messages.append(item)
                continue

            key = self._message_output_item_key(item)
            if key and key in existing_index_by_key:
                target_index = existing_index_by_key[key]
                merged_output[target_index] = self._merge_message_output_items(
                    merged_output[target_index], item
                )
                continue

            merged_output.append(json.loads(json.dumps(item, ensure_ascii=False)))
            if key:
                existing_index_by_key[key] = len(merged_output) - 1

        if incoming_messages:
            latest_message = json.loads(
                json.dumps(incoming_messages[-1], ensure_ascii=False)
            )
            existing_message_index = next(
                (
                    index
                    for index in range(len(merged_output) - 1, -1, -1)
                    if isinstance(merged_output[index], dict)
                    and merged_output[index].get("type") == "message"
                    and merged_output[index].get("role") == "assistant"
                ),
                None,
            )
            if existing_message_index is None:
                merged_output.append(latest_message)
            else:
                merged_output[existing_message_index] = self._merge_message_output_items(
                    merged_output[existing_message_index], latest_message
                )
        elif (
            self._message_output_has_structured_items(existing_output)
            and not self._message_output_has_structured_items(incoming_output)
        ):
            fallback_text = _serialize_message_output_content(
                incoming_output, fallback_content
            )
            if fallback_text:
                existing_message_index = next(
                    (
                        index
                        for index in range(len(merged_output) - 1, -1, -1)
                        if isinstance(merged_output[index], dict)
                        and merged_output[index].get("type") == "message"
                        and merged_output[index].get("role") == "assistant"
                    ),
                    None,
                )
                if existing_message_index is not None:
                    merged_output[existing_message_index]["content"] = [
                        {"type": "output_text", "text": fallback_text}
                    ]

        return merged_output

    def _merge_message_payload(
        self, existing_message: object, incoming_message: object
    ) -> object:
        if not isinstance(existing_message, dict):
            return incoming_message
        if not isinstance(incoming_message, dict):
            return existing_message

        merged_message = {**existing_message, **incoming_message}
        role = incoming_message.get("role", existing_message.get("role"))

        if isinstance(existing_message.get("files"), list) or isinstance(
            incoming_message.get("files"), list
        ):
            merged_message["files"] = self._merge_message_files(
                existing_message.get("files"),
                incoming_message.get("files")
                if isinstance(incoming_message.get("files"), list)
                else [],
                role,
            )

        if isinstance(existing_message.get("output"), list) or isinstance(
            incoming_message.get("output"), list
        ):
            merged_message["output"] = self._merge_message_output(
                existing_message.get("output"),
                incoming_message.get("output"),
                incoming_message.get("content", existing_message.get("content", "")),
            )
            merged_message["content"] = _serialize_message_output_content(
                merged_message.get("output"),
                incoming_message.get("content", existing_message.get("content", "")),
            )

        if existing_message.get("done") is True and incoming_message.get("done") is not True:
            merged_message["done"] = True

        return merged_message

    def _hydrate_chat_message_files(
        self, chat_payload: dict, chat_id: str, db: Optional[Session] = None
    ) -> tuple[dict, bool]:
        if not isinstance(chat_payload, dict):
            return chat_payload, False

        history = chat_payload.get("history")
        if not isinstance(history, dict):
            return chat_payload, False

        messages = history.get("messages")
        if not isinstance(messages, dict) or not messages:
            return chat_payload, False

        message_files: dict[str, list[dict]] = {}

        with get_db_context(db) as db:
            try:
                chat_messages = ChatMessages.get_messages_by_chat_id(chat_id, db=db)
                for chat_message in chat_messages:
                    message_id = self._extract_chat_message_id(chat_id, chat_message.id)
                    if not message_id or not isinstance(chat_message.files, list):
                        continue
                    if not chat_message.files:
                        continue
                    message_files.setdefault(message_id, []).extend(chat_message.files)
            except Exception as e:
                log.warning(
                    "Failed to load chat_message files for chat %s: %s", chat_id, e
                )

            try:
                chat_file_rows = (
                    db.query(ChatFile)
                    .filter_by(chat_id=chat_id)
                    .filter(ChatFile.message_id.isnot(None))
                    .order_by(ChatFile.created_at.asc())
                    .all()
                )

                file_ids = []
                for row in chat_file_rows:
                    file_id = str(row.file_id or "").strip()
                    if file_id:
                        file_ids.append(file_id)

                file_models_by_id = {
                    file_model.id: file_model
                    for file_model in Files.get_files_by_ids(list(set(file_ids)), db=db)
                }

                for row in chat_file_rows:
                    message_id = str(row.message_id or "").strip()
                    if not message_id:
                        continue

                    file_model = file_models_by_id.get(str(row.file_id or "").strip())
                    if file_model is None:
                        continue

                    message_files.setdefault(message_id, []).append(
                        self._message_file_from_file_model(file_model)
                    )
            except Exception as e:
                log.warning(
                    "Failed to load chat_file attachments for chat %s: %s", chat_id, e
                )

        if not message_files:
            return chat_payload, False

        hydrated_messages: Optional[dict] = None
        changed = False

        for message_id, supplemental_files in message_files.items():
            message = messages.get(message_id)
            if not isinstance(message, dict) or not supplemental_files:
                continue

            merged_files = self._merge_message_files(
                message.get("files"), supplemental_files, message.get("role")
            )
            if merged_files == message.get("files"):
                continue

            if hydrated_messages is None:
                hydrated_messages = dict(messages)

            hydrated_messages[message_id] = {**message, "files": merged_files}
            changed = True

        if not changed or hydrated_messages is None:
            return chat_payload, False

        return {
            **chat_payload,
            "history": {
                **history,
                "messages": hydrated_messages,
            },
        }, True

    def _prepare_chat_row_for_read(
        self, chat_item: Chat, db: Session
    ) -> Chat:
        changed = self._sanitize_chat_row(chat_item)

        hydrated_chat, hydrated_changed = self._hydrate_chat_message_files(
            chat_item.chat, chat_item.id, db=db
        )
        if hydrated_changed:
            chat_item.chat = hydrated_chat
            changed = True

        if changed:
            db.commit()
            db.refresh(chat_item)

        return chat_item

    def _resolve_history_current_id(self, history: dict) -> Optional[str]:
        messages = history.get("messages") if isinstance(history, dict) else None
        if not isinstance(messages, dict) or len(messages) == 0:
            return None

        current_id = history.get("currentId")
        if isinstance(current_id, str) and current_id in messages:
            return current_id

        candidates = []
        for message_id, message in messages.items():
            if not isinstance(message_id, str) or not isinstance(message, dict):
                continue

            child_ids = message.get("childrenIds")
            has_child_in_history = (
                isinstance(child_ids, list)
                and any(
                    isinstance(child_id, str) and child_id in messages
                    for child_id in child_ids
                )
            )
            if not has_child_in_history:
                candidates.append((message_id, message))

        if not candidates:
            candidates = [
                (message_id, message)
                for message_id, message in messages.items()
                if isinstance(message_id, str) and isinstance(message, dict)
            ]

        if not candidates:
            return None

        return max(
            candidates,
            key=lambda item: (
                item[1].get("timestamp", 0)
                if isinstance(item[1].get("timestamp"), (int, float))
                else 0,
                item[0],
            ),
        )[0]

    def _get_history_branch_ids(self, history: dict) -> tuple[list[str], Optional[str], dict]:
        messages = history.get("messages") if isinstance(history, dict) else None
        if not isinstance(messages, dict) or len(messages) == 0:
            return [], None, {}

        current_id = self._resolve_history_current_id(history)
        if current_id is None:
            return [], None, messages

        branch_ids: list[str] = []
        visited: set[str] = set()
        cursor: Optional[str] = current_id

        while isinstance(cursor, str) and cursor in messages and cursor not in visited:
            visited.add(cursor)
            branch_ids.append(cursor)

            parent_id = messages[cursor].get("parentId")
            cursor = parent_id if isinstance(parent_id, str) else None

        branch_ids.reverse()
        return branch_ids, current_id, messages

    def _build_history_tail_window(
        self, history: dict, tail: Optional[int]
    ) -> tuple[dict, dict]:
        """
        Build a truncated history map along the current branch.
        Returns (new_history, meta) without mutating the input.
        """
        if not isinstance(history, dict):
            return history, {
                "truncated": False,
                "can_load_more": False,
                "tail": 0,
                "total": 0,
            }

        messages = history.get("messages")
        if not isinstance(messages, dict) or not messages:
            total = len(messages) if isinstance(messages, dict) else 0
            return history, {
                "truncated": False,
                "can_load_more": False,
                "tail": 0,
                "total": total,
            }

        branch_ids, current_id, messages = self._get_history_branch_ids(history)
        total = len(messages)
        if not branch_ids or current_id is None:
            return history, {
                "truncated": False,
                "can_load_more": False,
                "tail": total,
                "total": total,
            }

        effective_tail: Optional[int] = None
        if tail is not None:
            try:
                tail_value = int(tail)
            except (TypeError, ValueError):
                tail_value = None
            if tail_value is not None and tail_value > 0:
                effective_tail = min(tail_value, MAX_CHAT_TAIL_MESSAGES)

        effective_branch_ids = (
            branch_ids[-effective_tail:] if effective_tail is not None else branch_ids
        )
        truncated = len(effective_branch_ids) < len(branch_ids)

        effective_message_ids = set(effective_branch_ids)
        history_messages: dict = {}
        for message_id in effective_branch_ids:
            message = messages.get(message_id)
            if not isinstance(message, dict):
                continue
            child_ids = message.get("childrenIds")
            if isinstance(child_ids, list):
                filtered_child_ids = [
                    child_id
                    for child_id in child_ids
                    if isinstance(child_id, str) and child_id in effective_message_ids
                ]
                if filtered_child_ids != child_ids:
                    message = {**message, "childrenIds": filtered_child_ids}
            history_messages[message_id] = message

        new_history = {
            **history,
            "messages": history_messages,
            "currentId": current_id,
        }
        meta = {
            "truncated": truncated,
            "can_load_more": truncated,
            "tail": len(effective_branch_ids),
            "tail_start_id": effective_branch_ids[0] if effective_branch_ids else None,
            "total": total,
        }
        return new_history, meta

    def _apply_history_tail_window(self, chat: ChatModel, tail: Optional[int]) -> ChatModel:
        chat_data = chat.model_dump()
        chat_payload = chat_data.get("chat") or {}
        history = chat_payload.get("history")

        new_history, history_meta = self._build_history_tail_window(history, tail)

        if new_history is not history:
            chat_payload = {**chat_payload, "history": new_history}
            chat_data["chat"] = chat_payload

        meta = dict(chat_data.get("meta") or {})
        meta["history"] = history_meta
        chat_data["meta"] = meta

        return ChatModel(**chat_data)

    def build_history_tail(self, history: dict, tail: int) -> tuple[dict, dict]:
        return self._build_history_tail_window(history, tail)

    def insert_new_chat(
        self, user_id: str, form_data: ChatForm, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        with get_db_context(db) as db:
            id = str(uuid.uuid4())
            chat_payload = self._normalize_chat_payload_for_storage(form_data.chat, id)
            chat = ChatModel(
                **{
                    "id": id,
                    "user_id": user_id,
                    "title": self._clean_null_bytes(
                        form_data.chat["title"]
                        if "title" in form_data.chat
                        else "New Chat"
                    ),
                    "chat": self._clean_null_bytes(chat_payload),
                    "meta": self._clean_null_bytes(form_data.meta or {}),
                    "folder_id": form_data.folder_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )

            chat_item = Chat(**chat.model_dump())
            db.add(chat_item)
            db.commit()
            db.refresh(chat_item)

            # Dual-write initial messages to chat_message table
            try:
                history = form_data.chat.get("history", {})
                messages = history.get("messages", {})
                for message_id, message in messages.items():
                    if isinstance(message, dict) and message.get("role"):
                        ChatMessages.upsert_message(
                            message_id=message_id,
                            chat_id=id,
                            user_id=user_id,
                            data=message,
                        )
            except Exception as e:
                log.warning(
                    f"Failed to write initial messages to chat_message table: {e}"
                )

            return ChatModel.model_validate(chat_item) if chat_item else None

    def _chat_import_form_to_chat_model(
        self, user_id: str, form_data: ChatImportForm
    ) -> ChatModel:
        id = str(uuid.uuid4())
        chat_payload = self._normalize_chat_payload_for_storage(form_data.chat, id)
        chat = ChatModel(
            **{
                "id": id,
                "user_id": user_id,
                "title": self._clean_null_bytes(
                    form_data.chat["title"] if "title" in form_data.chat else "New Chat"
                ),
                "chat": self._clean_null_bytes(chat_payload),
                "meta": form_data.meta,
                "pinned": form_data.pinned,
                "folder_id": form_data.folder_id,
                "created_at": (
                    form_data.created_at if form_data.created_at else int(time.time())
                ),
                "updated_at": (
                    form_data.updated_at if form_data.updated_at else int(time.time())
                ),
            }
        )
        return chat

    def import_chats(
        self,
        user_id: str,
        chat_import_forms: list[ChatImportForm],
        db: Optional[Session] = None,
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            chats = []

            for form_data in chat_import_forms:
                chat = self._chat_import_form_to_chat_model(user_id, form_data)
                chats.append(Chat(**chat.model_dump()))

            db.add_all(chats)
            db.commit()

            # Dual-write messages to chat_message table
            try:
                for form_data, chat_obj in zip(chat_import_forms, chats):
                    history = form_data.chat.get("history", {})
                    messages = history.get("messages", {})
                    for message_id, message in messages.items():
                        if isinstance(message, dict) and message.get("role"):
                            ChatMessages.upsert_message(
                                message_id=message_id,
                                chat_id=chat_obj.id,
                                user_id=user_id,
                                data=message,
                            )
            except Exception as e:
                log.warning(
                    f"Failed to write imported messages to chat_message table: {e}"
                )

            return [ChatModel.model_validate(chat) for chat in chats]

    def update_chat_by_id(
        self, id: str, chat: dict, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat_item = db.get(Chat, id)
                chat_item.chat = self._clean_null_bytes(
                    self._normalize_chat_payload_for_storage(chat, id)
                )
                chat_item.title = (
                    self._clean_null_bytes(chat["title"])
                    if "title" in chat
                    else "New Chat"
                )

                chat_item.updated_at = int(time.time())

                db.commit()
                db.refresh(chat_item)

                return ChatModel.model_validate(chat_item)
        except Exception:
            return None

    def update_chat_title_by_id(self, id: str, title: str) -> Optional[ChatModel]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        chat = chat.chat
        chat["title"] = title

        return self.update_chat_by_id(id, chat)

    def update_chat_tags_by_id(
        self, id: str, tags: list[str], user
    ) -> Optional[ChatModel]:
        with get_db_context() as db:
            chat = db.get(Chat, id)
            if chat is None:
                return None

            old_tags = chat.meta.get("tags", [])
            new_tags = [t for t in tags if t.replace(" ", "_").lower() != "none"]
            new_tag_ids = [t.replace(" ", "_").lower() for t in new_tags]

            # Single meta update
            chat.meta = {**chat.meta, "tags": new_tag_ids}
            db.commit()
            db.refresh(chat)

            # Batch-create any missing tag rows
            Tags.ensure_tags_exist(new_tags, user.id, db=db)

            # Clean up orphaned old tags in one query
            removed = set(old_tags) - set(new_tag_ids)
            if removed:
                self.delete_orphan_tags_for_user(list(removed), user.id, db=db)

            return ChatModel.model_validate(chat)

    def update_chat_meta_by_id(
        self, id: str, meta_updates: dict, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, id)
                if chat is None:
                    return None

                existing_meta = chat.meta or {}
                chat.meta = {**existing_meta, **meta_updates}
                chat.updated_at = int(time.time())
                db.commit()
                db.refresh(chat)

                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def get_chat_title_by_id(self, id: str) -> Optional[str]:
        with get_db_context() as db:
            result = db.query(Chat.title).filter_by(id=id).first()
            if result is None:
                return None
            return result[0] or "New Chat"

    def get_messages_map_by_chat_id(self, id: str) -> Optional[dict]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        return chat.chat.get("history", {}).get("messages", {}) or {}

    def get_message_by_id_and_message_id(
        self, id: str, message_id: str
    ) -> Optional[dict]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        return chat.chat.get("history", {}).get("messages", {}).get(message_id, {})

    def upsert_message_to_chat_by_id_and_message_id(
        self, id: str, message_id: str, message: dict
    ) -> Optional[ChatModel]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        # Sanitize message content for null characters before upserting
        if isinstance(message.get("content"), str):
            message["content"] = sanitize_text_for_db(message["content"])

        user_id = chat.user_id
        chat = chat.chat
        history = chat.get("history", {})

        if message_id in history.get("messages", {}):
            history["messages"][message_id] = self._merge_message_payload(
                history["messages"][message_id],
                message,
            )
        else:
            history["messages"][message_id] = message

        history["currentId"] = message_id

        chat["history"] = history

        # Dual-write to chat_message table
        try:
            ChatMessages.upsert_message(
                message_id=message_id,
                chat_id=id,
                user_id=user_id,
                data=history["messages"][message_id],
            )
        except Exception as e:
            log.warning(f"Failed to write to chat_message table: {e}")

        return self.update_chat_by_id(id, chat)

    def add_message_status_to_chat_by_id_and_message_id(
        self, id: str, message_id: str, status: dict
    ) -> Optional[ChatModel]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        chat = chat.chat
        history = chat.get("history", {})

        if message_id in history.get("messages", {}):
            status_history = history["messages"][message_id].get("statusHistory", [])
            status_history.append(status)
            history["messages"][message_id]["statusHistory"] = status_history

        chat["history"] = history
        return self.update_chat_by_id(id, chat)

    def add_message_files_by_id_and_message_id(
        self, id: str, message_id: str, files: list[dict]
    ) -> list[dict]:
        with get_db_context() as db:
            chat = self.get_chat_by_id(id, db=db)
            if chat is None:
                return None

            user_id = chat.user_id
            chat = chat.chat
            history = chat.get("history", {})

            message_files = []

            sanitized_files: list[dict] = []
            for item in files or []:
                if not isinstance(item, dict):
                    continue

                def _normalized_ref(*keys: str) -> str:
                    for key in keys:
                        value = item.get(key)
                        if not isinstance(value, str):
                            continue
                        normalized = value.strip()
                        if normalized and normalized.lower() not in {"null", "undefined"}:
                            return normalized
                    return ""

                url_ref = _normalized_ref(
                    "url",
                    "bridge_url",
                    "generated_file_url",
                    "download_url",
                    "downloadUrl",
                )
                id_ref = _normalized_ref(
                    "id",
                    "bridge_file_id",
                    "file_id",
                    "fileId",
                )
                path_ref = _normalized_ref(
                    "path",
                    "output_path",
                    "target_path",
                    "file_path",
                )

                if not any((url_ref, id_ref, path_ref)):
                    continue

                normalized = {**item}

                if url_ref:
                    normalized["url"] = url_ref
                elif id_ref:
                    normalized["url"] = id_ref
                else:
                    normalized["path"] = path_ref

                if id_ref:
                    normalized["id"] = id_ref
                elif path_ref:
                    normalized["id"] = path_ref

                sanitized_files.append(normalized)

            def _file_key(file_item: dict) -> str:
                if not isinstance(file_item, dict):
                    return ""
                return "||".join(
                    [
                        str(file_item.get("id") or "").strip(),
                        str(file_item.get("url") or "").strip(),
                        str(file_item.get("path") or "").strip(),
                        str(file_item.get("name") or file_item.get("filename") or "").strip(),
                        str(file_item.get("size") or "").strip(),
                    ]
                )

            if message_id in history.get("messages", {}):
                existing_files = history["messages"][message_id].get("files", [])
                message_files = []
                seen_file_keys: set[str] = set()

                for file_item in [*(existing_files or []), *sanitized_files]:
                    if not isinstance(file_item, dict):
                        continue
                    dedupe_key = _file_key(file_item)
                    if dedupe_key and dedupe_key in seen_file_keys:
                        continue
                    if dedupe_key:
                        seen_file_keys.add(dedupe_key)
                    message_files.append(file_item)

                role = str(history["messages"][message_id].get("role") or "").strip().lower()
                if role == "assistant":
                    message_files = _collapse_assistant_generated_file_variants(
                        message_files
                    )

                history["messages"][message_id]["files"] = message_files
            elif sanitized_files:
                message_files = []
                seen_file_keys: set[str] = set()

                for file_item in sanitized_files:
                    if not isinstance(file_item, dict):
                        continue
                    dedupe_key = _file_key(file_item)
                    if dedupe_key and dedupe_key in seen_file_keys:
                        continue
                    if dedupe_key:
                        seen_file_keys.add(dedupe_key)
                    message_files.append(file_item)

                message_files = _collapse_assistant_generated_file_variants(message_files)

                history.setdefault("messages", {})[message_id] = {
                    "role": "assistant",
                    "content": "",
                    "files": message_files,
                    "done": False,
                    "timestamp": int(time.time()),
                }
                if not history.get("currentId"):
                    history["currentId"] = message_id

            chat["history"] = history
            self.update_chat_by_id(id, chat, db=db)

            if message_files:
                try:
                    message_payload = (
                        history.get("messages", {}).get(message_id) or {"files": message_files}
                    )
                    if "files" not in message_payload:
                        message_payload = {**message_payload, "files": message_files}
                    ChatMessages.upsert_message(
                        message_id=message_id,
                        chat_id=id,
                        user_id=user_id,
                        data=message_payload,
                        db=db,
                    )
                except Exception as e:
                    log.warning(f"Failed to write message files to chat_message table: {e}")

            return message_files

    def insert_shared_chat_by_chat_id(
        self, chat_id: str, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        with get_db_context(db) as db:
            # Get the existing chat to share
            chat = db.get(Chat, chat_id)
            # Check if chat exists
            if not chat:
                return None
            # Check if the chat is already shared
            if chat.share_id:
                return self.get_chat_by_id_and_user_id(chat.share_id, "shared", db=db)
            # Create a new chat with the same data, but with a new ID
            shared_chat = ChatModel(
                **{
                    "id": str(uuid.uuid4()),
                    "user_id": f"shared-{chat_id}",
                    "title": chat.title,
                    "chat": chat.chat,
                    "meta": chat.meta,
                    "pinned": chat.pinned,
                    "folder_id": chat.folder_id,
                    "created_at": chat.created_at,
                    "updated_at": int(time.time()),
                }
            )
            shared_result = Chat(**shared_chat.model_dump())
            db.add(shared_result)
            db.commit()
            db.refresh(shared_result)

            # Update the original chat with the share_id
            result = (
                db.query(Chat)
                .filter_by(id=chat_id)
                .update({"share_id": shared_chat.id})
            )
            db.commit()
            return shared_chat if (shared_result and result) else None

    def update_shared_chat_by_chat_id(
        self, chat_id: str, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, chat_id)
                shared_chat = (
                    db.query(Chat).filter_by(user_id=f"shared-{chat_id}").first()
                )

                if shared_chat is None:
                    return self.insert_shared_chat_by_chat_id(chat_id, db=db)

                shared_chat.title = chat.title
                shared_chat.chat = chat.chat
                shared_chat.meta = chat.meta
                shared_chat.pinned = chat.pinned
                shared_chat.folder_id = chat.folder_id
                shared_chat.updated_at = int(time.time())
                db.commit()
                db.refresh(shared_chat)

                return ChatModel.model_validate(shared_chat)
        except Exception:
            return None

    def delete_shared_chat_by_chat_id(
        self, chat_id: str, db: Optional[Session] = None
    ) -> bool:
        try:
            with get_db_context(db) as db:
                # Use subquery to delete chat_messages for shared chats
                shared_chat_id_subquery = (
                    db.query(Chat.id)
                    .filter_by(user_id=f"shared-{chat_id}")
                    .scalar_subquery()
                )
                db.query(ChatMessage).filter(
                    ChatMessage.chat_id.in_(shared_chat_id_subquery)
                ).delete(synchronize_session=False)
                db.query(Chat).filter_by(user_id=f"shared-{chat_id}").delete()
                db.commit()

                return True
        except Exception:
            return False

    def unarchive_all_chats_by_user_id(
        self, user_id: str, db: Optional[Session] = None
    ) -> bool:
        try:
            with get_db_context(db) as db:
                db.query(Chat).filter_by(user_id=user_id).update({"archived": False})
                db.commit()
                return True
        except Exception:
            return False

    def update_chat_share_id_by_id(
        self, id: str, share_id: Optional[str], db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, id)
                chat.share_id = share_id
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def toggle_chat_pinned_by_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, id)
                chat.pinned = not chat.pinned
                chat.updated_at = int(time.time())
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def toggle_chat_archive_by_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, id)
                chat.archived = not chat.archived
                chat.folder_id = None
                chat.updated_at = int(time.time())
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def archive_all_chats_by_user_id(
        self, user_id: str, db: Optional[Session] = None
    ) -> bool:
        try:
            with get_db_context(db) as db:
                db.query(Chat).filter_by(user_id=user_id).update({"archived": True})
                db.commit()
                return True
        except Exception:
            return False

    def get_archived_chat_list_by_user_id(
        self,
        user_id: str,
        filter: Optional[dict] = None,
        skip: int = 0,
        limit: int = 50,
        db: Optional[Session] = None,
    ) -> list[ChatTitleIdResponse]:

        with get_db_context(db) as db:
            query = db.query(Chat).filter_by(user_id=user_id, archived=True)

            if filter:
                query_key = filter.get("query")
                if query_key:
                    query = query.filter(Chat.title.ilike(f"%{query_key}%"))

                order_by = filter.get("order_by")
                direction = filter.get("direction")

                if order_by and direction:
                    if not getattr(Chat, order_by, None):
                        raise ValueError("Invalid order_by field")

                    if direction.lower() == "asc":
                        query = query.order_by(getattr(Chat, order_by).asc(), Chat.id)
                    elif direction.lower() == "desc":
                        query = query.order_by(getattr(Chat, order_by).desc(), Chat.id)
                    else:
                        raise ValueError("Invalid direction for ordering")
            else:
                query = query.order_by(Chat.updated_at.desc(), Chat.id)

            query = query.with_entities(
                Chat.id, Chat.title, Chat.updated_at, Chat.created_at
            )

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()
            return [
                ChatTitleIdResponse.model_validate(
                    {
                        "id": chat[0],
                        "title": chat[1],
                        "updated_at": chat[2],
                        "created_at": chat[3],
                    }
                )
                for chat in all_chats
            ]

    def get_shared_chat_list_by_user_id(
        self,
        user_id: str,
        filter: Optional[dict] = None,
        skip: int = 0,
        limit: int = 50,
        db: Optional[Session] = None,
    ) -> list[SharedChatResponse]:

        with get_db_context(db) as db:
            query = (
                db.query(Chat)
                .filter_by(user_id=user_id)
                .filter(Chat.share_id.isnot(None))
            )

            if filter:
                query_key = filter.get("query")
                if query_key:
                    query = query.filter(Chat.title.ilike(f"%{query_key}%"))

                order_by = filter.get("order_by")
                direction = filter.get("direction")

                if order_by and direction:
                    if not getattr(Chat, order_by, None):
                        raise ValueError("Invalid order_by field")

                    if direction.lower() == "asc":
                        query = query.order_by(getattr(Chat, order_by).asc(), Chat.id)
                    elif direction.lower() == "desc":
                        query = query.order_by(getattr(Chat, order_by).desc(), Chat.id)
                    else:
                        raise ValueError("Invalid direction for ordering")
            else:
                query = query.order_by(Chat.updated_at.desc(), Chat.id)

            # Select only the columns needed for SharedChatResponse
            # to avoid loading the heavy chat JSON blob
            query = query.with_entities(
                Chat.id,
                Chat.title,
                Chat.share_id,
                Chat.updated_at,
                Chat.created_at,
            )

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()
            return [
                SharedChatResponse.model_validate(
                    {
                        "id": chat[0],
                        "title": chat[1],
                        "share_id": chat[2],
                        "updated_at": chat[3],
                        "created_at": chat[4],
                    }
                )
                for chat in all_chats
            ]

    def get_chat_list_by_user_id(
        self,
        user_id: str,
        include_archived: bool = False,
        filter: Optional[dict] = None,
        skip: int = 0,
        limit: int = 50,
        db: Optional[Session] = None,
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            query = db.query(Chat).filter_by(user_id=user_id)
            if not include_archived:
                query = query.filter_by(archived=False)

            if filter:
                query_key = filter.get("query")
                if query_key:
                    query = query.filter(Chat.title.ilike(f"%{query_key}%"))

                order_by = filter.get("order_by")
                direction = filter.get("direction")

                if order_by and direction and getattr(Chat, order_by):
                    if direction.lower() == "asc":
                        query = query.order_by(getattr(Chat, order_by).asc(), Chat.id)
                    elif direction.lower() == "desc":
                        query = query.order_by(getattr(Chat, order_by).desc(), Chat.id)
                    else:
                        raise ValueError("Invalid direction for ordering")
            else:
                query = query.order_by(Chat.updated_at.desc(), Chat.id)

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chat_title_id_list_by_user_id(
        self,
        user_id: str,
        include_archived: bool = False,
        include_folders: bool = False,
        include_pinned: bool = False,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> list[ChatTitleIdResponse]:
        with get_db_context(db) as db:
            query = db.query(Chat).filter_by(user_id=user_id)

            if not include_folders:
                query = query.filter_by(folder_id=None)

            if not include_pinned:
                query = query.filter(or_(Chat.pinned == False, Chat.pinned == None))

            if not include_archived:
                query = query.filter_by(archived=False)

            query = query.order_by(Chat.updated_at.desc(), Chat.id).with_entities(
                Chat.id, Chat.title, Chat.updated_at, Chat.created_at
            )

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()

            # result has to be destructured from sqlalchemy `row` and mapped to a dict since the `ChatModel`is not the returned dataclass.
            return [
                ChatTitleIdResponse.model_validate(
                    {
                        "id": chat[0],
                        "title": chat[1],
                        "updated_at": chat[2],
                        "created_at": chat[3],
                    }
                )
                for chat in all_chats
            ]

    def get_chat_list_by_chat_ids(
        self,
        chat_ids: list[str],
        skip: int = 0,
        limit: int = 50,
        db: Optional[Session] = None,
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            all_chats = (
                db.query(Chat)
                .filter(Chat.id.in_(chat_ids))
                .filter_by(archived=False)
                .order_by(Chat.updated_at.desc())
                .all()
            )
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chat_by_id(
        self,
        id: str,
        history_tail: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None

                chat_item = self._prepare_chat_row_for_read(chat_item, db)

                chat = ChatModel.model_validate(chat_item)
                if history_tail is not None:
                    return self._apply_history_tail_window(chat, history_tail)
                return chat
        except Exception:
            return None

    def get_chat_by_share_id(
        self, id: str, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                # it is possible that the shared link was deleted. hence,
                # we check if the chat is still shared by checking if a chat with the share_id exists
                chat = db.query(Chat).filter_by(share_id=id).first()

                if chat:
                    return self.get_chat_by_id(id, db=db)
                else:
                    return None
        except Exception:
            return None

    def get_chat_by_id_and_user_id(
        self,
        id: str,
        user_id: str,
        history_tail: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat = db.query(Chat).filter_by(id=id, user_id=user_id).first()
                if chat is None:
                    return None

                chat = self._prepare_chat_row_for_read(chat, db)

                chat_model = ChatModel.model_validate(chat)
                if history_tail is not None:
                    return self._apply_history_tail_window(chat_model, history_tail)
                return chat_model
        except Exception:
            return None

    def is_chat_owner(
        self, id: str, user_id: str, db: Optional[Session] = None
    ) -> bool:
        """
        Lightweight ownership check — uses EXISTS subquery instead of loading
        the full Chat row (which includes the potentially large JSON blob).
        """
        try:
            with get_db_context(db) as db:
                return db.query(
                    exists().where(and_(Chat.id == id, Chat.user_id == user_id))
                ).scalar()
        except Exception:
            return False

    def get_chat_folder_id(
        self, id: str, user_id: str, db: Optional[Session] = None
    ) -> Optional[str]:
        """
        Fetch only the folder_id column for a chat, without loading the full
        JSON blob. Returns None if chat doesn't exist or doesn't belong to user.
        """
        try:
            with get_db_context(db) as db:
                result = (
                    db.query(Chat.folder_id).filter_by(id=id, user_id=user_id).first()
                )
                return result[0] if result else None
        except Exception:
            return None

    def get_chats(
        self, skip: int = 0, limit: int = 50, db: Optional[Session] = None
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            all_chats = (
                db.query(Chat)
                # .limit(limit).offset(skip)
                .order_by(Chat.updated_at.desc())
            )
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chats_by_user_id(
        self,
        user_id: str,
        filter: Optional[dict] = None,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> ChatListResponse:
        with get_db_context(db) as db:
            query = db.query(Chat).filter_by(user_id=user_id)

            if filter:
                if filter.get("updated_at"):
                    query = query.filter(Chat.updated_at > filter.get("updated_at"))

                order_by = filter.get("order_by")
                direction = filter.get("direction")

                if order_by and direction:
                    if hasattr(Chat, order_by):
                        if direction.lower() == "asc":
                            query = query.order_by(
                                getattr(Chat, order_by).asc(), Chat.id
                            )
                        elif direction.lower() == "desc":
                            query = query.order_by(
                                getattr(Chat, order_by).desc(), Chat.id
                            )
                else:
                    query = query.order_by(Chat.updated_at.desc(), Chat.id)

            else:
                query = query.order_by(Chat.updated_at.desc(), Chat.id)

            total = query.count()

            if skip is not None:
                query = query.offset(skip)
            if limit is not None:
                query = query.limit(limit)

            all_chats = query.all()

            return ChatListResponse(
                **{
                    "items": [ChatModel.model_validate(chat) for chat in all_chats],
                    "total": total,
                }
            )

    def get_pinned_chats_by_user_id(
        self, user_id: str, db: Optional[Session] = None
    ) -> list[ChatTitleIdResponse]:
        with get_db_context(db) as db:
            all_chats = (
                db.query(Chat)
                .filter_by(user_id=user_id, pinned=True, archived=False)
                .order_by(Chat.updated_at.desc())
                .with_entities(Chat.id, Chat.title, Chat.updated_at, Chat.created_at)
            )
            return [
                ChatTitleIdResponse.model_validate(
                    {
                        "id": chat[0],
                        "title": chat[1],
                        "updated_at": chat[2],
                        "created_at": chat[3],
                    }
                )
                for chat in all_chats
            ]

    def get_archived_chats_by_user_id(
        self, user_id: str, db: Optional[Session] = None
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            all_chats = (
                db.query(Chat)
                .filter_by(user_id=user_id, archived=True)
                .order_by(Chat.updated_at.desc())
            )
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chats_by_user_id_and_search_text(
        self,
        user_id: str,
        search_text: str,
        include_archived: bool = False,
        skip: int = 0,
        limit: int = 60,
        db: Optional[Session] = None,
    ) -> list[ChatModel]:
        """
        Filters chats based on a search query using Python, allowing pagination using skip and limit.
        """
        search_text = sanitize_text_for_db(search_text).lower().strip()

        if not search_text:
            return self.get_chat_list_by_user_id(
                user_id, include_archived, filter={}, skip=skip, limit=limit, db=db
            )

        search_text_words = search_text.split(" ")

        # search_text might contain 'tag:tag_name' format so we need to extract the tag_name, split the search_text and remove the tags
        tag_ids = [
            word.replace("tag:", "").replace(" ", "_").lower()
            for word in search_text_words
            if word.startswith("tag:")
        ]

        # Extract folder names - handle spaces and case insensitivity
        folders = Folders.search_folders_by_names(
            user_id,
            [
                word.replace("folder:", "")
                for word in search_text_words
                if word.startswith("folder:")
            ],
        )
        folder_ids = [folder.id for folder in folders]

        is_pinned = None
        if "pinned:true" in search_text_words:
            is_pinned = True
        elif "pinned:false" in search_text_words:
            is_pinned = False

        is_archived = None
        if "archived:true" in search_text_words:
            is_archived = True
        elif "archived:false" in search_text_words:
            is_archived = False

        is_shared = None
        if "shared:true" in search_text_words:
            is_shared = True
        elif "shared:false" in search_text_words:
            is_shared = False

        search_text_words = [
            word
            for word in search_text_words
            if (
                not word.startswith("tag:")
                and not word.startswith("folder:")
                and not word.startswith("pinned:")
                and not word.startswith("archived:")
                and not word.startswith("shared:")
            )
        ]

        search_text = " ".join(search_text_words)

        with get_db_context(db) as db:
            query = db.query(Chat).filter(Chat.user_id == user_id)

            if is_archived is not None:
                query = query.filter(Chat.archived == is_archived)
            elif not include_archived:
                query = query.filter(Chat.archived == False)

            if is_pinned is not None:
                query = query.filter(Chat.pinned == is_pinned)

            if is_shared is not None:
                if is_shared:
                    query = query.filter(Chat.share_id.isnot(None))
                else:
                    query = query.filter(Chat.share_id.is_(None))

            if folder_ids:
                query = query.filter(Chat.folder_id.in_(folder_ids))

            query = query.order_by(Chat.updated_at.desc(), Chat.id)

            # Check if the database dialect is either 'sqlite' or 'postgresql'
            dialect_name = db.bind.dialect.name
            if dialect_name == "sqlite":
                # SQLite case: using JSON1 extension for JSON searching
                sqlite_content_sql = (
                    "EXISTS ("
                    "    SELECT 1 "
                    "    FROM json_each(Chat.chat, '$.messages') AS message "
                    "    WHERE LOWER(message.value->>'content') LIKE '%' || :content_key || '%'"
                    ")"
                )
                sqlite_content_clause = text(sqlite_content_sql)
                query = query.filter(
                    or_(
                        Chat.title.ilike(bindparam("title_key")), sqlite_content_clause
                    ).params(title_key=f"%{search_text}%", content_key=search_text)
                )

                # Check if there are any tags to filter, it should have all the tags
                if "none" in tag_ids:
                    query = query.filter(text("""
                            NOT EXISTS (
                                SELECT 1
                                FROM json_each(Chat.meta, '$.tags') AS tag
                            )
                            """))
                elif tag_ids:
                    query = query.filter(
                        and_(
                            *[
                                text(f"""
                                    EXISTS (
                                        SELECT 1
                                        FROM json_each(Chat.meta, '$.tags') AS tag
                                        WHERE tag.value = :tag_id_{tag_idx}
                                    )
                                    """).params(**{f"tag_id_{tag_idx}": tag_id})
                                for tag_idx, tag_id in enumerate(tag_ids)
                            ]
                        )
                    )

            elif dialect_name == "postgresql":
                # PostgreSQL doesn't allow null bytes in text. We filter those out by checking
                # the JSON representation for \u0000 before attempting text extraction

                # Safety filter: JSON field must not contain \u0000
                query = query.filter(text("Chat.chat::text NOT LIKE '%\\\\u0000%'"))

                # Safety filter: title must not contain actual null bytes
                query = query.filter(text("Chat.title::text NOT LIKE '%\\x00%'"))

                postgres_content_sql = """
                EXISTS (
                    SELECT 1
                    FROM json_array_elements(Chat.chat->'messages') AS message
                    WHERE json_typeof(message->'content') = 'string'
                    AND LOWER(message->>'content') LIKE '%' || :content_key || '%'
                )
                """

                postgres_content_clause = text(postgres_content_sql)

                query = query.filter(
                    or_(
                        Chat.title.ilike(bindparam("title_key")),
                        postgres_content_clause,
                    )
                ).params(title_key=f"%{search_text}%", content_key=search_text.lower())

                # Check if there are any tags to filter, it should have all the tags
                if "none" in tag_ids:
                    query = query.filter(text("""
                            NOT EXISTS (
                                SELECT 1
                                FROM json_array_elements_text(Chat.meta->'tags') AS tag
                            )
                            """))
                elif tag_ids:
                    query = query.filter(
                        and_(
                            *[
                                text(f"""
                                    EXISTS (
                                        SELECT 1
                                        FROM json_array_elements_text(Chat.meta->'tags') AS tag
                                        WHERE tag = :tag_id_{tag_idx}
                                    )
                                    """).params(**{f"tag_id_{tag_idx}": tag_id})
                                for tag_idx, tag_id in enumerate(tag_ids)
                            ]
                        )
                    )
            else:
                raise NotImplementedError(
                    f"Unsupported dialect: {db.bind.dialect.name}"
                )

            # Perform pagination at the SQL level
            all_chats = query.offset(skip).limit(limit).all()

            log.info(f"The number of chats: {len(all_chats)}")

            # Validate and return chats
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chats_by_folder_id_and_user_id(
        self,
        folder_id: str,
        user_id: str,
        skip: int = 0,
        limit: int = 60,
        db: Optional[Session] = None,
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            query = db.query(Chat).filter_by(folder_id=folder_id, user_id=user_id)
            query = query.filter(or_(Chat.pinned == False, Chat.pinned == None))
            query = query.filter_by(archived=False)

            query = query.order_by(Chat.updated_at.desc(), Chat.id)

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chats_by_folder_ids_and_user_id(
        self, folder_ids: list[str], user_id: str, db: Optional[Session] = None
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            query = db.query(Chat).filter(
                Chat.folder_id.in_(folder_ids), Chat.user_id == user_id
            )
            query = query.filter(or_(Chat.pinned == False, Chat.pinned == None))
            query = query.filter_by(archived=False)

            query = query.order_by(Chat.updated_at.desc())

            all_chats = query.all()
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def update_chat_folder_id_by_id_and_user_id(
        self, id: str, user_id: str, folder_id: str, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, id)
                chat.folder_id = folder_id
                chat.updated_at = int(time.time())
                chat.pinned = False
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def get_chat_tags_by_id_and_user_id(
        self, id: str, user_id: str, db: Optional[Session] = None
    ) -> list[TagModel]:
        with get_db_context(db) as db:
            chat = db.get(Chat, id)
            tag_ids = chat.meta.get("tags", [])
            return Tags.get_tags_by_ids_and_user_id(tag_ids, user_id, db=db)

    def get_chat_list_by_user_id_and_tag_name(
        self,
        user_id: str,
        tag_name: str,
        skip: int = 0,
        limit: int = 50,
        db: Optional[Session] = None,
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            query = db.query(Chat).filter_by(user_id=user_id)
            tag_id = tag_name.replace(" ", "_").lower()

            log.info(f"DB dialect name: {db.bind.dialect.name}")
            if db.bind.dialect.name == "sqlite":
                # SQLite JSON1 querying for tags within the meta JSON field
                query = query.filter(
                    text(
                        f"EXISTS (SELECT 1 FROM json_each(Chat.meta, '$.tags') WHERE json_each.value = :tag_id)"
                    )
                ).params(tag_id=tag_id)
            elif db.bind.dialect.name == "postgresql":
                # PostgreSQL JSON query for tags within the meta JSON field (for `json` type)
                query = query.filter(
                    text(
                        "EXISTS (SELECT 1 FROM json_array_elements_text(Chat.meta->'tags') elem WHERE elem = :tag_id)"
                    )
                ).params(tag_id=tag_id)
            else:
                raise NotImplementedError(
                    f"Unsupported dialect: {db.bind.dialect.name}"
                )

            all_chats = query.all()
            log.debug(f"all_chats: {all_chats}")
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def add_chat_tag_by_id_and_user_id_and_tag_name(
        self, id: str, user_id: str, tag_name: str, db: Optional[Session] = None
    ) -> Optional[ChatModel]:
        tag_id = tag_name.replace(" ", "_").lower()
        Tags.ensure_tags_exist([tag_name], user_id, db=db)
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, id)
                if tag_id not in chat.meta.get("tags", []):
                    chat.meta = {
                        **chat.meta,
                        "tags": list(set(chat.meta.get("tags", []) + [tag_id])),
                    }
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def count_chats_by_tag_name_and_user_id(
        self, tag_name: str, user_id: str, db: Optional[Session] = None
    ) -> int:
        with get_db_context(db) as db:
            query = db.query(Chat).filter_by(user_id=user_id, archived=False)
            tag_id = tag_name.replace(" ", "_").lower()

            if db.bind.dialect.name == "sqlite":
                query = query.filter(
                    text(
                        "EXISTS (SELECT 1 FROM json_each(Chat.meta, '$.tags') WHERE json_each.value = :tag_id)"
                    )
                ).params(tag_id=tag_id)
            elif db.bind.dialect.name == "postgresql":
                query = query.filter(
                    text(
                        "EXISTS (SELECT 1 FROM json_array_elements_text(Chat.meta->'tags') elem WHERE elem = :tag_id)"
                    )
                ).params(tag_id=tag_id)
            else:
                raise NotImplementedError(
                    f"Unsupported dialect: {db.bind.dialect.name}"
                )

            return query.count()

    def delete_orphan_tags_for_user(
        self,
        tag_ids: list[str],
        user_id: str,
        threshold: int = 0,
        db: Optional[Session] = None,
    ) -> None:
        """Delete tag rows from *tag_ids* that appear in at most *threshold*
        non-archived chats for *user_id*.  One query to find orphans, one to
        delete them.

        Use threshold=0 after a tag is already removed from a chat's meta.
        Use threshold=1 when the chat itself is about to be deleted (the
        referencing chat still exists at query time).
        """
        if not tag_ids:
            return
        with get_db_context(db) as db:
            orphans = []
            for tag_id in tag_ids:
                count = self.count_chats_by_tag_name_and_user_id(tag_id, user_id, db=db)
                if count <= threshold:
                    orphans.append(tag_id)
            Tags.delete_tags_by_ids_and_user_id(orphans, user_id, db=db)

    def count_chats_by_folder_id_and_user_id(
        self, folder_id: str, user_id: str, db: Optional[Session] = None
    ) -> int:
        with get_db_context(db) as db:
            query = db.query(Chat).filter_by(user_id=user_id)

            query = query.filter_by(folder_id=folder_id)
            count = query.count()

            log.info(f"Count of chats for folder '{folder_id}': {count}")
            return count

    def delete_tag_by_id_and_user_id_and_tag_name(
        self, id: str, user_id: str, tag_name: str, db: Optional[Session] = None
    ) -> bool:
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, id)
                tags = chat.meta.get("tags", [])
                tag_id = tag_name.replace(" ", "_").lower()

                tags = [tag for tag in tags if tag != tag_id]
                chat.meta = {
                    **chat.meta,
                    "tags": list(set(tags)),
                }
                db.commit()
                return True
        except Exception:
            return False

    def delete_all_tags_by_id_and_user_id(
        self, id: str, user_id: str, db: Optional[Session] = None
    ) -> bool:
        try:
            with get_db_context(db) as db:
                chat = db.get(Chat, id)
                chat.meta = {
                    **chat.meta,
                    "tags": [],
                }
                db.commit()

                return True
        except Exception:
            return False

    def delete_chat_by_id(self, id: str, db: Optional[Session] = None) -> bool:
        try:
            with get_db_context(db) as db:
                db.query(ChatMessage).filter_by(chat_id=id).delete()
                db.query(Chat).filter_by(id=id).delete()
                db.commit()

                return True and self.delete_shared_chat_by_chat_id(id, db=db)
        except Exception:
            return False

    def delete_chat_by_id_and_user_id(
        self, id: str, user_id: str, db: Optional[Session] = None
    ) -> bool:
        try:
            with get_db_context(db) as db:
                db.query(ChatMessage).filter_by(chat_id=id).delete()
                db.query(Chat).filter_by(id=id, user_id=user_id).delete()
                db.commit()

                return True and self.delete_shared_chat_by_chat_id(id, db=db)
        except Exception:
            return False

    def delete_chats_by_user_id(
        self, user_id: str, db: Optional[Session] = None, commit: bool = True
    ) -> bool:
        try:
            with get_db_context(db) as db:
                self.delete_shared_chats_by_user_id(user_id, db=db, commit=False)

                chat_id_subquery = select(Chat.id).where(Chat.user_id == user_id)
                db.query(ChatMessage).filter(
                    ChatMessage.chat_id.in_(chat_id_subquery)
                ).delete(synchronize_session=False)
                db.query(Chat).filter_by(user_id=user_id).delete()
                if commit:
                    db.commit()
                else:
                    db.flush()

                return True
        except Exception:
            return False

    def delete_chats_by_user_id_and_folder_id(
        self, user_id: str, folder_id: str, db: Optional[Session] = None
    ) -> bool:
        try:
            with get_db_context(db) as db:
                chat_id_subquery = select(Chat.id).where(
                    Chat.user_id == user_id, Chat.folder_id == folder_id
                )
                db.query(ChatMessage).filter(
                    ChatMessage.chat_id.in_(chat_id_subquery)
                ).delete(synchronize_session=False)
                db.query(Chat).filter_by(user_id=user_id, folder_id=folder_id).delete()
                db.commit()

                return True
        except Exception:
            return False

    def move_chats_by_user_id_and_folder_id(
        self,
        user_id: str,
        folder_id: str,
        new_folder_id: Optional[str],
        db: Optional[Session] = None,
    ) -> bool:
        try:
            with get_db_context(db) as db:
                db.query(Chat).filter_by(user_id=user_id, folder_id=folder_id).update(
                    {"folder_id": new_folder_id}
                )
                db.commit()

                return True
        except Exception:
            return False

    def delete_shared_chats_by_user_id(
        self, user_id: str, db: Optional[Session] = None, commit: bool = True
    ) -> bool:
        try:
            with get_db_context(db) as db:
                chats_by_user = db.query(Chat).filter_by(user_id=user_id).all()
                shared_chat_ids = [f"shared-{chat.id}" for chat in chats_by_user]

                # Use subquery to delete chat_messages for shared chats
                shared_id_subq = select(Chat.id).where(Chat.user_id.in_(shared_chat_ids))
                db.query(ChatMessage).filter(
                    ChatMessage.chat_id.in_(shared_id_subq)
                ).delete(synchronize_session=False)
                db.query(Chat).filter(Chat.user_id.in_(shared_chat_ids)).delete()
                if commit:
                    db.commit()
                else:
                    db.flush()

                return True
        except Exception:
            return False

    def insert_chat_files(
        self,
        chat_id: str,
        message_id: str,
        file_ids: list[str],
        user_id: str,
        db: Optional[Session] = None,
    ) -> Optional[list[ChatFileModel]]:
        if not file_ids:
            return None

        chat_message_file_ids = [
            item.id
            for item in self.get_chat_files_by_chat_id_and_message_id(
                chat_id, message_id, db=db
            )
        ]
        # Remove duplicates and existing file_ids
        file_ids = list(
            set(
                [
                    file_id
                    for file_id in file_ids
                    if file_id and file_id not in chat_message_file_ids
                ]
            )
        )
        if not file_ids:
            return None

        try:
            with get_db_context(db) as db:
                now = int(time.time())

                chat_files = [
                    ChatFileModel(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        file_id=file_id,
                        created_at=now,
                        updated_at=now,
                    )
                    for file_id in file_ids
                ]

                results = [
                    ChatFile(**chat_file.model_dump()) for chat_file in chat_files
                ]

                db.add_all(results)
                db.commit()

                return chat_files
        except Exception:
            return None

    def get_chat_files_by_chat_id_and_message_id(
        self, chat_id: str, message_id: str, db: Optional[Session] = None
    ) -> list[ChatFileModel]:
        with get_db_context(db) as db:
            all_chat_files = (
                db.query(ChatFile)
                .filter_by(chat_id=chat_id, message_id=message_id)
                .order_by(ChatFile.created_at.asc())
                .all()
            )
            return [
                ChatFileModel.model_validate(chat_file) for chat_file in all_chat_files
            ]

    def delete_chat_file(
        self, chat_id: str, file_id: str, db: Optional[Session] = None
    ) -> bool:
        try:
            with get_db_context(db) as db:
                db.query(ChatFile).filter_by(chat_id=chat_id, file_id=file_id).delete()
                db.commit()
                return True
        except Exception:
            return False

    def get_shared_chats_by_file_id(
        self, file_id: str, db: Optional[Session] = None
    ) -> list[ChatModel]:
        with get_db_context(db) as db:
            # Join Chat and ChatFile tables to get shared chats associated with the file_id
            all_chats = (
                db.query(Chat)
                .join(ChatFile, Chat.id == ChatFile.chat_id)
                .filter(ChatFile.file_id == file_id, Chat.share_id.isnot(None))
                .all()
            )

            return [ChatModel.model_validate(chat) for chat in all_chats]


Chats = ChatTable()
