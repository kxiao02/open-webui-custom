import ast
import hashlib
import logging
import json
import re
import time
import uuid
from typing import Optional
from urllib.parse import urlparse, urlunparse

from sqlalchemy.orm import Session
from open_webui.internal.db import Base, JSONField, get_db, get_db_context
from open_webui.models.tags import TagModel, Tag, Tags
from open_webui.models.folders import Folders
from open_webui.models.chat_messages import ChatMessage, ChatMessages
from open_webui.models.files import Files
from open_webui.utils.misc import sanitize_data_for_db, sanitize_text_for_db
from open_webui.utils.knowflow import get_knowflow_asset_ref_key
from open_webui.utils.telemetry.llm_observability import (
    diagnostic_classification_counts,
    diagnostic_reason_codes,
    observe_llm_event,
)

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
SECOND_PASS_RETRIEVAL_TOOL_NAMES = {
    "query_selected_knowledge_files",
    "read_selected_file",
}
WEBPAGE_TOOL_NAMES = {
    "visit_webpage",
    "fetch_url",
}
_WEB_REFERENCE_TEXT_LIMIT = 1600
_SAFE_QUERY_TEXT_LIMIT = 240
_OFFICIAL_WEB_HOST_SUFFIXES = (
    ".gov",
    ".gov.cn",
    ".gov.hk",
    ".gov.mo",
    ".gov.uk",
    ".go.jp",
    ".gc.ca",
)
_SUPPRESSED_REFERENCE_CITATION_PATTERN = re.compile(
    r"\s*\[(?:\d+\s*(?:,\s*\d+\s*)*)\]"
)
_SUPPRESSED_REFERENCE_PUNCTUATION_PATTERN = re.compile(r"[ \t]+([,.;:!?，。！？；：])")
_SUPPRESSED_REFERENCE_DOUBLE_SPACE_PATTERN = re.compile(r"[ \t]{2,}")


def _normalize_message_file_ref(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    if lowered in {"null", "undefined"}:
        return ""
    knowflow_asset_ref = get_knowflow_asset_ref_key(normalized)
    if knowflow_asset_ref:
        return knowflow_asset_ref
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


def _message_file_url_score(file_item: dict) -> int:
    normalized = _normalize_message_file_ref(
        file_item.get("download_url")
        or file_item.get("downloadUrl")
        or file_item.get("url")
    )
    if not normalized:
        return 0

    lowered = normalized.lower()
    if "/api/v1/files/" in lowered and "/content" in lowered:
        return 5
    if "/api/v1/files/" in lowered:
        return 4
    if get_knowflow_asset_ref_key(normalized):
        return 3
    if lowered.startswith(("https://", "http://")):
        return 2
    return 0


def _message_file_label_score(file_item: dict) -> int:
    label = _infer_message_file_name(file_item)
    if not label:
        return 0

    lowered = label.strip().lower()
    if lowered in {"knowflow", "image", "file", "generated-file"}:
        return 1
    return len(label.strip())


def _merge_message_file_variants(existing: dict, incoming: dict) -> dict:
    merged = {
        **existing,
        **{
            key: value
            for key, value in incoming.items()
            if value not in (None, "", [], {})
        },
    }

    if _message_file_url_score(existing) >= _message_file_url_score(incoming):
        for key in ("url", "download_url", "downloadUrl"):
            value = _normalize_message_file_ref(existing.get(key))
            if value:
                merged[key] = value
    else:
        for key in ("url", "download_url", "downloadUrl"):
            value = _normalize_message_file_ref(incoming.get(key))
            if value:
                merged[key] = value

    if _message_file_label_score(existing) >= _message_file_label_score(incoming):
        for key in ("name", "filename", "fileName"):
            value = _normalize_message_file_ref(existing.get(key))
            if value:
                merged[key] = value
    else:
        for key in ("name", "filename", "fileName"):
            value = _normalize_message_file_ref(incoming.get(key))
            if value:
                merged[key] = value

    existing_id = _normalize_message_file_ref(existing.get("id"))
    incoming_id = _normalize_message_file_ref(incoming.get("id"))
    if len(existing_id) >= len(incoming_id):
        if existing_id:
            merged["id"] = existing_id
    elif incoming_id:
        merged["id"] = incoming_id

    return merged


def _collapse_assistant_generated_file_variants(files: list[dict]) -> list[dict]:
    collapsed: list[dict] = []
    index_by_key: dict[str, int] = {}

    for file_item in files:
        key = ""
        for candidate in (
            file_item.get("download_url"),
            file_item.get("downloadUrl"),
            file_item.get("url"),
            file_item.get("id"),
        ):
            asset_ref = get_knowflow_asset_ref_key(candidate)
            if asset_ref:
                key = f"knowflow::{asset_ref}"
                break

        if not key:
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
        if key.startswith("knowflow::"):
            collapsed[existing_index] = _merge_message_file_variants(existing, file_item)
            continue

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


_HISTORICAL_TOOL_REPLAY_NOTE_START = (
    "Historical tool attempt retained as plain context only."
)
_HISTORICAL_TOOL_REPLAY_NOTE_SECOND_SENTENCE = (
    "Do not replay it as a new tool call."
)
_HISTORICAL_TOOL_REPLAY_NOTE_LINE_PREFIXES = (
    "Tool:",
    "Status:",
    "Arguments:",
    "Replay was skipped because",
    "Reported output:",
)


def _is_historical_tool_replay_note_line(line: str) -> bool:
    trimmed = line.strip()
    return any(
        trimmed.startswith(prefix)
        for prefix in _HISTORICAL_TOOL_REPLAY_NOTE_LINE_PREFIXES
    )


def _extract_historical_tool_replay_note_remainder(line: str) -> Optional[str]:
    trimmed = line.strip()
    if not trimmed:
        return None

    if trimmed.startswith(_HISTORICAL_TOOL_REPLAY_NOTE_START):
        remainder = trimmed[len(_HISTORICAL_TOOL_REPLAY_NOTE_START) :].lstrip()
        if remainder.startswith(_HISTORICAL_TOOL_REPLAY_NOTE_SECOND_SENTENCE):
            remainder = remainder[
                len(_HISTORICAL_TOOL_REPLAY_NOTE_SECOND_SENTENCE) :
            ].lstrip()
        if remainder and not _is_historical_tool_replay_note_line(remainder):
            return remainder
        return None

    if not trimmed.startswith("Reported output:"):
        return None

    remainder = trimmed[len("Reported output:") :].strip()
    if not remainder:
        return None

    parts = re.split(r"(?:\s{2,}|\t+)", remainder, maxsplit=1)
    if len(parts) < 2:
        return None

    candidate = parts[1].strip()
    if not candidate or _is_historical_tool_replay_note_line(candidate):
        return None

    return candidate


def _strip_historical_tool_replay_note_text(text: object) -> object:
    if not isinstance(text, str):
        return text
    if _HISTORICAL_TOOL_REPLAY_NOTE_START not in text:
        return text

    kept: list[str] = []
    in_block = False

    for line in text.splitlines():
        trimmed = line.strip()

        if not in_block:
            if trimmed.startswith(_HISTORICAL_TOOL_REPLAY_NOTE_START):
                in_block = True
                remainder = _extract_historical_tool_replay_note_remainder(line)
                if remainder:
                    kept.append(remainder)
                    in_block = False
                continue
            kept.append(line)
            continue

        if not trimmed:
            continue

        remainder = _extract_historical_tool_replay_note_remainder(line)
        if remainder:
            kept.append(remainder)
            in_block = False
            continue

        if _is_historical_tool_replay_note_line(trimmed):
            if trimmed.startswith("Reported output:"):
                in_block = False
            continue

        in_block = False
        kept.append(line)

    sanitized = "\n".join(kept)
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


def _sanitize_assistant_text_content(text: object) -> object:
    sanitized = _strip_leaked_vision_specialist_prefix(text)
    return _strip_historical_tool_replay_note_text(sanitized)


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


def _is_webpage_tool_name(tool_name: str) -> bool:
    return str(tool_name or "").strip().lower() in WEBPAGE_TOOL_NAMES


def _parse_function_call_arguments(value: object) -> dict:
    parsed = _parse_tool_output_payload(value)
    return parsed if isinstance(parsed, dict) else {}


def _function_call_context_by_call_id(output: object) -> dict[str, dict[str, object]]:
    context_by_call_id: dict[str, dict[str, object]] = {}
    if not isinstance(output, list):
        return context_by_call_id

    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = str(item.get("call_id") or item.get("id") or "").strip()
        if not call_id:
            continue
        context_by_call_id[call_id] = {
            "name": str(item.get("name") or "").strip(),
            "arguments": _parse_function_call_arguments(item.get("arguments")),
        }

    return context_by_call_id


def _coalesce_mapping_value(*mappings: object, keys: tuple[str, ...]) -> object:
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        for key in keys:
            value = mapping.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _collapse_reference_text(value: object, *, limit: int = _WEB_REFERENCE_TEXT_LIMIT) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) <= limit:
        return text
    return f"{text[: max(limit - 1, 0)].rstrip()}…"


def _web_query_digest(value: object) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip())
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _safe_query_provenance_fields(value: object) -> dict[str, str]:
    normalized = re.sub(r"\s+", " ", str(value or "").strip())
    if not normalized:
        return {}

    fields = {"query_digest": _web_query_digest(normalized)}
    if len(normalized) <= _SAFE_QUERY_TEXT_LIMIT:
        fields["query"] = normalized
    return fields


def _looks_like_official_web_url(value: object) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False

    host = urlparse(normalized).netloc.strip().lower()
    if not host:
        return False
    if host.startswith("www."):
        host = host[4:]

    return host.endswith(_OFFICIAL_WEB_HOST_SUFFIXES) or host in {
        "gov.cn",
        "www.gov.cn",
    }


def _looks_like_http_web_url(value: object) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False

    parsed = urlparse(normalized)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _normalize_web_source_class(value: object, *, url: object = "", authority: object = "") -> str:
    normalized = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if normalized in {
        "official",
        "official_web",
        "government",
        "gov",
        "primary_source",
        "official_primary",
    }:
        return "official_web"
    if normalized in {"generic", "generic_web", "web", "online", "website", "webpage"}:
        return "generic_web"

    normalized_authority = (
        str(authority or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if normalized_authority in {"official", "government", "gov", "primary", "authoritative"}:
        return "official_web"

    if _looks_like_official_web_url(url):
        return "official_web"
    if _looks_like_http_web_url(url):
        return "generic_web"
    return ""


def _normalize_web_authority(value: object, *, source_class: str = "") -> str:
    normalized = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if normalized in {"official", "government", "gov", "primary", "authoritative"}:
        return "official"
    if normalized in {"generic", "public", "web"}:
        return "generic"
    if source_class == "official_web":
        return "official"
    if source_class == "generic_web":
        return "generic"
    return ""


def _web_reference_domain(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    host = urlparse(normalized).netloc.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _normalize_web_reference_url(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        return normalized

    path = parsed.path.rstrip("/")
    if path == "/":
        path = ""

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            parsed.query,
            "",
        )
    )


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


def _merge_message_file_entries(existing: list[dict], incoming: list[dict]) -> list[dict]:
    merged = [item for item in existing if isinstance(item, dict)]
    index_by_key: dict[str, int] = {}

    for index, item in enumerate(merged):
        key = str(
            item.get("id")
            or item.get("url")
            or item.get("path")
            or item.get("output_path")
            or item.get("name")
            or item.get("filename")
            or ""
        ).strip()
        if key:
            index_by_key[key] = index

    for item in incoming:
        if not isinstance(item, dict):
            continue
        key = str(
            item.get("id")
            or item.get("url")
            or item.get("path")
            or item.get("output_path")
            or item.get("name")
            or item.get("filename")
            or ""
        ).strip()
        if not key or key not in index_by_key:
            index_by_key[key] = len(merged)
            merged.append(dict(item))
            continue

        existing_item = merged[index_by_key[key]]
        merged[index_by_key[key]] = {
            **existing_item,
            **{k: v for k, v in item.items() if v not in (None, "", [], {})},
        }

    return merged


def _strip_misbinding_tool_output_files(
    output: list[dict],
    known_call_ids: set[str],
) -> bool:
    changed = False

    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue

        item_call_id = str(item.get("call_id") or item.get("id") or "").strip()
        existing_files = item.get("files")
        if not item_call_id or not isinstance(existing_files, list):
            continue

        cleaned_files = []
        removed_misbound_files = False
        for file_item in existing_files:
            if not isinstance(file_item, dict):
                cleaned_files.append(file_item)
                continue

            file_call_id = str(file_item.get("call_id") or "").strip()
            if (
                file_call_id
                and file_call_id != item_call_id
                and file_call_id in known_call_ids
            ):
                removed_misbound_files = True
                continue

            cleaned_files.append(file_item)

        if removed_misbound_files:
            item["files"] = cleaned_files
            changed = True

    return changed


def _repair_message_tool_outputs_from_files(
    output: object,
    files: object,
) -> tuple[object, bool]:
    if not isinstance(output, list) or not isinstance(files, list):
        return output, False

    changed = False
    files_by_call_id: dict[str, list[dict]] = {}
    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        call_id = str(file_item.get("call_id") or "").strip()
        if not call_id:
            continue
        files_by_call_id.setdefault(call_id, []).append(dict(file_item))

    if not files_by_call_id:
        return output, False

    function_call_by_id: dict[str, dict] = {}
    function_call_output_by_id: dict[str, dict] = {}

    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if call_id:
                function_call_by_id[call_id] = item
        elif item_type == "function_call_output":
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if call_id:
                function_call_output_by_id[call_id] = item

    known_call_ids = (
        set(files_by_call_id.keys())
        | set(function_call_by_id.keys())
        | set(function_call_output_by_id.keys())
    )
    if _strip_misbinding_tool_output_files(output, known_call_ids):
        changed = True

    for call_id, call_files in files_by_call_id.items():
        if call_id in function_call_output_by_id:
            output_item = function_call_output_by_id[call_id]
            existing_files = output_item.get("files")
            merged_files = _merge_message_file_entries(
                existing_files if isinstance(existing_files, list) else [],
                call_files,
            )
            if merged_files != existing_files:
                output_item["files"] = merged_files
                changed = True
            if str(output_item.get("status") or "").strip().lower() in {"", "running", "in_progress"}:
                output_item["status"] = "success"
                changed = True
            function_call_item = function_call_by_id.get(call_id)
            if function_call_item is not None and str(
                function_call_item.get("status") or ""
            ).strip().lower() in {"", "running", "in_progress"}:
                function_call_item["status"] = "completed"
                changed = True
            continue

        function_call_item = function_call_by_id.get(call_id)
        if function_call_item is None:
            continue

        tool_name = str(function_call_item.get("name") or "tool").strip() or "tool"
        output.append(
            {
                "type": "function_call_output",
                "id": f"fco_{call_id}",
                "call_id": call_id,
                "name": tool_name,
                "status": "success",
                "files": call_files,
                "output": [
                    {
                        "type": "input_text",
                        "text": "工具已完成并生成文件，可在界面中预览或下载。",
                    }
                ],
            }
        )
        function_call_item["status"] = "completed"
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
                sanitized_text = _sanitize_assistant_text_content(original_text)
                if sanitized_text != original_text:
                    updated_content.append({**part, "text": sanitized_text})
                    item_changed = True
                    continue
            updated_content.append(part)

        summary = item.get("summary")
        if isinstance(summary, str):
            sanitized_summary = _sanitize_assistant_text_content(summary)
            if sanitized_summary != summary:
                item["summary"] = sanitized_summary
                item_changed = True

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
        content = _sanitize_assistant_text_content(content)
        if content:
            return content
    if isinstance(fallback_content, str):
        sanitized = _sanitize_assistant_text_content(fallback_content)
        return sanitized.strip() if isinstance(sanitized, str) else ""
    return ""


def _sanitize_assistant_message(message: object) -> tuple[object, bool]:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return message, False

    changed = False
    output = message.get("output")
    if isinstance(output, list):
        _, repaired_output_changed = _repair_message_tool_outputs_from_files(
            output, message.get("files")
        )
        if repaired_output_changed:
            message["output"] = output
            changed = True
        _, output_changed = _sanitize_message_output(output)
        if output_changed:
            message["output"] = output
            message["content"] = _serialize_message_output_content(
                output, message.get("content", "")
            )
            changed = True
        elif repaired_output_changed:
            message["content"] = _serialize_message_output_content(
                output, message.get("content", "")
            )

    content = message.get("content")
    if isinstance(content, str):
        sanitized_content = _sanitize_assistant_text_content(content)
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

    def _build_history_branch_messages(self, history: object) -> Optional[list[dict]]:
        if not isinstance(history, dict):
            return None

        branch_ids, current_id, messages = self._get_history_branch_ids(history)
        if current_id is None:
            return []

        branch_messages: list[dict] = []
        for message_id in branch_ids:
            message = messages.get(message_id)
            if isinstance(message, dict):
                branch_messages.append(message)

        return branch_messages

    def _normalize_chat_history_for_storage(
        self, chat_payload: object
    ) -> tuple[object, bool]:
        if not isinstance(chat_payload, dict):
            return chat_payload, False

        history = chat_payload.get("history")
        if not isinstance(history, dict):
            return chat_payload, False

        messages = history.get("messages")
        if not isinstance(messages, dict):
            return chat_payload, False

        normalized_messages = messages
        changed = False

        for message_id, message in messages.items():
            if not isinstance(message_id, str) or not isinstance(message, dict):
                continue
            normalized_message = self._normalize_message_reference_sidecar(message)
            if not isinstance(normalized_message, dict):
                normalized_message = message
            if normalized_message.get("id") == message_id and normalized_message == message:
                continue
            if normalized_messages is messages:
                normalized_messages = dict(messages)
            normalized_messages[message_id] = {**normalized_message, "id": message_id}
            changed = True

        normalized_history = history
        if normalized_messages is not messages:
            normalized_history = {**normalized_history, "messages": normalized_messages}

        resolved_current_id = self._resolve_history_current_id(normalized_history)
        if normalized_history.get("currentId") != resolved_current_id:
            normalized_history = {
                **normalized_history,
                "currentId": resolved_current_id,
            }
            changed = True

        branch_messages = self._build_history_branch_messages(normalized_history)
        normalized_chat_payload = chat_payload

        if normalized_history is not history:
            normalized_chat_payload = {
                **normalized_chat_payload,
                "history": normalized_history,
            }

        if branch_messages is not None and normalized_chat_payload.get("messages") != branch_messages:
            normalized_chat_payload = {
                **normalized_chat_payload,
                "messages": branch_messages,
            }
            changed = True

        return normalized_chat_payload, changed

    def _normalize_chat_payload_for_storage(
        self, chat_payload: object, chat_id: str
    ) -> object:
        normalized = self._ensure_chat_payload_identity(chat_payload, chat_id)
        normalized, _ = _normalize_chat_history_tool_outputs(normalized)
        normalized, _ = self._normalize_chat_history_for_storage(normalized)
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

    def _is_retrieval_diagnostic_item(self, value: object) -> bool:
        if not isinstance(value, dict):
            return False
        return str(value.get("kind") or "").strip().lower() == "retrieval_quality"

    def _is_second_pass_retrieval_tool_name(self, value: object) -> bool:
        return (
            str(value or "").strip().lower()
            in SECOND_PASS_RETRIEVAL_TOOL_NAMES
        )

    def _retrieval_tool_name_from_payload(self, value: object) -> str:
        if not isinstance(value, dict):
            return ""

        for key in ("tool_name", "function_name", "registered_name", "name"):
            candidate = str(value.get(key) or "").strip()
            if self._is_second_pass_retrieval_tool_name(candidate):
                return candidate

        for item in value.get("retrieval_diagnostics") or []:
            if not isinstance(item, dict):
                continue
            candidate = str(item.get("tool_name") or "").strip()
            if self._is_second_pass_retrieval_tool_name(candidate):
                return candidate

        for reference in value.get("canonical_references") or []:
            if not isinstance(reference, dict):
                continue
            provenance = reference.get("provenance")
            if isinstance(provenance, dict):
                candidate = str(provenance.get("tool_name") or "").strip()
                if self._is_second_pass_retrieval_tool_name(candidate):
                    return candidate
            for metadata in reference.get("metadata") or []:
                if not isinstance(metadata, dict):
                    continue
                candidate = str(
                    metadata.get("retrieval_tool_name")
                    or metadata.get("tool_name")
                    or ""
                ).strip()
                if self._is_second_pass_retrieval_tool_name(candidate):
                    return candidate

        return ""

    def _looks_like_retrieval_tool_payload(
        self, value: object, fallback_tool_name: str = ""
    ) -> bool:
        if not isinstance(value, dict):
            return False
        if self._is_second_pass_retrieval_tool_name(fallback_tool_name):
            return True
        if self._retrieval_tool_name_from_payload(value):
            return True
        if value.get("tool_id") == "builtin:retrieval":
            return True
        return False

    def _diagnostic_from_retrieval_tool_payload(
        self, payload: dict, fallback_tool_name: str = ""
    ) -> Optional[dict]:
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("retrieval_diagnostics"), list) and payload.get(
            "retrieval_diagnostics"
        ):
            return None

        status = str(payload.get("status") or "").strip().lower()
        if not status and any(
            payload.get(key) not in (None, "", [], {})
            for key in ("error", "code", "message", "reason")
        ):
            status = "error"
        if status in {"", "success", "evidence"}:
            return None

        tool_name = (
            self._retrieval_tool_name_from_payload(payload)
            or str(fallback_tool_name or "").strip()
        )
        if not self._is_second_pass_retrieval_tool_name(tool_name):
            return None

        reason = str(
            payload.get("reason")
            or payload.get("code")
            or payload.get("error")
            or payload.get("message")
            or f"retrieval_{status}"
        ).strip()
        if not reason:
            reason = f"retrieval_{status}"

        classification = "no_evidence" if status in {"empty", "weak", "denied"} else "diagnostics"
        diagnostic = {
            "kind": "retrieval_quality",
            "classification": classification,
            "reason": reason,
            "tool_name": tool_name,
        }
        query = str(payload.get("query") or "").strip()
        if query:
            diagnostic["query"] = query
        retrieval_round = payload.get("retrieval_round")
        if retrieval_round not in (None, "", [], {}):
            diagnostic["retrieval_round"] = retrieval_round
        detail = payload.get("detail")
        if detail not in (None, "", [], {}):
            diagnostic["detail"] = detail
        return diagnostic

    def _iter_retrieval_tool_output_payloads(self, output: object) -> list[dict]:
        if not isinstance(output, list):
            return []

        function_context_by_call_id = _function_call_context_by_call_id(output)

        payloads: list[dict] = []
        dropped_canonical_references_count = 0
        malformed_payload_count = 0
        non_success_payload_count = 0
        status_counts: dict[str, int] = {}
        synthesized_diagnostic_count = 0
        tool_names: set[str] = set()
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            function_context = function_context_by_call_id.get(call_id) or {}
            fallback_tool_name = str(
                item.get("name")
                or function_context.get("name")
                or ""
            ).strip()
            parsed_payload = _extract_function_call_output_payload(item)
            if not isinstance(parsed_payload, dict):
                malformed_payload_count += 1
                continue
            if not self._looks_like_retrieval_tool_payload(
                parsed_payload, fallback_tool_name
            ):
                continue

            payload = json.loads(
                json.dumps(parsed_payload, ensure_ascii=False, default=str)
            )
            for alias in ("sources", "citations", "references", "documents"):
                payload.pop(alias, None)
            status = str(payload.get("status") or "").strip().lower()
            status_counts[status or "unknown"] = status_counts.get(status or "unknown", 0) + 1
            if status and status not in {"success", "evidence"}:
                non_success_payload_count += 1
                if payload.get("canonical_references") not in (None, "", [], {}):
                    dropped_canonical_references_count += 1
                payload.pop("canonical_references", None)
            if fallback_tool_name and not payload.get("tool_name"):
                payload["tool_name"] = fallback_tool_name
            tool_name = self._retrieval_tool_name_from_payload(payload)
            if tool_name:
                tool_names.add(tool_name)
            diagnostic = self._diagnostic_from_retrieval_tool_payload(
                payload, fallback_tool_name
            )
            if diagnostic:
                synthesized_diagnostic_count += 1
                payload["retrieval_diagnostics"] = [
                    *(payload.get("retrieval_diagnostics") or []),
                    diagnostic,
                ]
            payloads.append(payload)

        if payloads or malformed_payload_count:
            observe_llm_event(
                "retrieval.second_pass_payload.normalize",
                {
                    "payload_count": len(payloads),
                    "malformed_payload_count": malformed_payload_count,
                    "non_success_payload_count": non_success_payload_count,
                    "dropped_canonical_references_count": (
                        dropped_canonical_references_count
                    ),
                    "synthesized_diagnostic_count": synthesized_diagnostic_count,
                    "status_counts": status_counts,
                    "tool_names": sorted(tool_names),
                },
            )

        return payloads

    def _web_reference_diagnostic(
        self,
        *,
        tool_name: str,
        status: str,
        reason: str,
        query: str = "",
        query_digest: str = "",
        url: str = "",
        retrieval_round: object = None,
        provider: str = "",
        source_class: str = "",
        authority: str = "",
        detail: object = None,
    ) -> dict:
        classification = (
            "no_evidence" if status in {"empty", "weak", "denied"} else "diagnostics"
        )
        diagnostic = {
            "kind": "retrieval_quality",
            "classification": classification,
            "reason": reason,
            "tool_name": tool_name,
        }
        if query:
            diagnostic["query"] = query
        elif query_digest:
            diagnostic["query_digest"] = query_digest
        if url:
            diagnostic["url"] = url
        if retrieval_round not in (None, "", [], {}):
            diagnostic["retrieval_round"] = retrieval_round
        if provider:
            diagnostic["provider"] = provider
        if source_class:
            diagnostic["source_class"] = source_class
        if authority:
            diagnostic["authority"] = authority
        if detail not in (None, "", [], {}):
            diagnostic["detail"] = detail
        return diagnostic

    def _normalize_web_reference(
        self,
        *,
        tool_name: str,
        url: object,
        title: object,
        content: object,
        query: object = "",
        retrieval_round: object = None,
        provider: object = "",
        source_class: object = "",
        authority: object = "",
        as_of: object = None,
        freshness: object = None,
    ) -> Optional[dict]:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            return None

        normalized_source_class = _normalize_web_source_class(
            source_class,
            url=normalized_url,
            authority=authority,
        )
        normalized_authority = _normalize_web_authority(
            authority,
            source_class=normalized_source_class,
        )
        normalized_title = str(title or normalized_url).strip() or normalized_url
        normalized_content = _collapse_reference_text(
            content or title or normalized_url
        )
        if not normalized_content:
            return None

        metadata = {
            "source": normalized_url,
            "name": normalized_title,
            "url": normalized_url,
            "tool_name": tool_name,
            "retrieval_tool_name": tool_name,
        }
        metadata.update(_safe_query_provenance_fields(query))

        normalized_provider = str(provider or "").strip()
        if normalized_provider:
            metadata["provider"] = normalized_provider

        if retrieval_round not in (None, "", [], {}):
            metadata["retrieval_round"] = retrieval_round

        normalized_as_of = str(as_of or "").strip()
        if normalized_as_of:
            metadata["as_of"] = normalized_as_of

        normalized_freshness = str(freshness or "").strip()
        if normalized_freshness:
            metadata["freshness"] = normalized_freshness

        if normalized_source_class:
            metadata["source_class"] = normalized_source_class

        if normalized_authority:
            metadata["authority"] = normalized_authority

        domain = _web_reference_domain(normalized_url)
        if domain:
            metadata["domain"] = domain

        reference: dict[str, object] = {
            "source": {
                "id": normalized_url,
                "name": normalized_title,
                "url": normalized_url,
                "type": normalized_source_class or "web",
            },
            "document": [normalized_content],
            "metadata": [metadata],
        }

        if normalized_source_class:
            reference["source_class"] = normalized_source_class
        if normalized_authority:
            reference["authority"] = normalized_authority
            reference["source"]["authority"] = normalized_authority
        if normalized_as_of:
            reference["as_of"] = normalized_as_of
        if normalized_freshness:
            reference["freshness"] = normalized_freshness

        return reference

    def _web_reference_has_acceptance_markers(
        self,
        reference: dict,
        *,
        source_info: dict,
        provenance: dict,
    ) -> bool:
        if reference.get("type") == "retrieval_reference":
            return True

        provenance_source_class = (
            str(provenance.get("source_class") or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        if provenance_source_class in {
            "official",
            "official_web",
            "government",
            "gov",
            "primary_source",
            "official_primary",
            "generic",
            "generic_web",
        }:
            return True

        provenance_authority = (
            str(provenance.get("authority") or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        if provenance_authority in {
            "official",
            "government",
            "gov",
            "primary",
            "authoritative",
            "generic",
            "public",
        }:
            return True

        if any(
            provenance.get(key) not in (None, "", [], {})
            for key in (
                "tool_name",
                "provider",
                "query",
                "query_digest",
                "retrieval_round",
                "domain",
                "as_of",
                "freshness",
                "published_at",
                "page",
                "section",
                "chunk_id",
            )
        ):
            return True

        source_type = str(source_info.get("type") or "").strip().lower()
        if source_type in {"official_web", "generic_web"}:
            return True

        explicit_source_class = (
            str(reference.get("source_class") or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        if explicit_source_class in {
            "official",
            "official_web",
            "government",
            "gov",
            "primary_source",
            "official_primary",
            "generic",
            "generic_web",
        }:
            return True

        explicit_authority = (
            str(reference.get("authority") or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        if explicit_authority in {"official", "government", "gov", "primary", "authoritative", "generic", "public"}:
            return True

        if any(reference.get(key) not in (None, "", [], {}) for key in ("as_of", "freshness")):
            return True

        for metadata in reference.get("metadata") or []:
            if not isinstance(metadata, dict):
                continue
            if any(
                metadata.get(key) not in (None, "", [], {})
                for key in (
                    "tool_name",
                    "retrieval_tool_name",
                    "provider",
                    "query",
                    "query_digest",
                    "retrieval_round",
                    "source_class",
                    "authority",
                    "domain",
                    "as_of",
                    "freshness",
                    "published_at",
                    "page",
                    "section",
                    "chunk_id",
                )
            ):
                return True

        return False

    def _enrich_normalized_web_reference(
        self,
        reference: dict,
        *,
        tool_name: str,
        query: str = "",
        retrieval_round: object = None,
        provider: object = "",
        as_of: object = None,
        freshness: object = None,
    ) -> Optional[dict]:
        if not isinstance(reference, dict):
            return None

        source_info = reference.get("source")
        source_info = dict(source_info) if isinstance(source_info, dict) else {}
        metadata_items = [
            dict(item)
            for item in (reference.get("metadata") or [])
            if isinstance(item, dict)
        ]
        provenance = (
            dict(reference.get("provenance"))
            if isinstance(reference.get("provenance"), dict)
            else {}
        )

        metadata_url = ""
        metadata_name = ""
        metadata_source_class = ""
        metadata_authority = ""
        metadata_domain = ""
        for item in metadata_items:
            if not metadata_url and item.get("url") not in (None, "", [], {}):
                metadata_url = str(item.get("url") or "").strip()
            if not metadata_name and item.get("name") not in (None, "", [], {}):
                metadata_name = str(item.get("name") or "").strip()
            if (
                not metadata_source_class
                and item.get("source_class") not in (None, "", [], {})
            ):
                metadata_source_class = str(item.get("source_class") or "").strip()
            if not metadata_authority and item.get("authority") not in (None, "", [], {}):
                metadata_authority = str(item.get("authority") or "").strip()
            if not metadata_domain and item.get("domain") not in (None, "", [], {}):
                metadata_domain = str(item.get("domain") or "").strip()

        normalized_url = _normalize_web_reference_url(
            source_info.get("url")
            or reference.get("url")
            or metadata_url
            or (
                source_info.get("id")
                if str(source_info.get("id") or "").strip().startswith(("http://", "https://"))
                else ""
            )
            or (
                provenance.get("source_id")
                if str(provenance.get("source_id") or "").strip().startswith(("http://", "https://"))
                else ""
            )
        )
        normalized_title = str(
            source_info.get("name")
            or reference.get("title")
            or metadata_name
            or normalized_url
        ).strip() or normalized_url
        normalized_source_class = _normalize_web_source_class(
            reference.get("source_class")
            or provenance.get("source_class")
            or metadata_source_class
            or source_info.get("type"),
            url=normalized_url,
            authority=reference.get("authority")
            or provenance.get("authority")
            or metadata_authority
            or source_info.get("authority"),
        )
        normalized_authority = _normalize_web_authority(
            reference.get("authority")
            or provenance.get("authority")
            or metadata_authority
            or source_info.get("authority"),
            source_class=normalized_source_class,
        )
        is_web_reference = bool(normalized_url) and bool(normalized_source_class)

        if not is_web_reference:
            return reference

        if not self._web_reference_has_acceptance_markers(
            reference,
            source_info=source_info,
            provenance=provenance,
        ):
            return None

        source_info["id"] = str(source_info.get("id") or normalized_url).strip() or normalized_url
        source_info["url"] = normalized_url
        if normalized_title:
            source_info["name"] = normalized_title
        if normalized_source_class:
            source_info["type"] = normalized_source_class
            reference["source_class"] = normalized_source_class
            provenance["source_class"] = normalized_source_class
            provenance["source_type"] = normalized_source_class
        if normalized_authority:
            source_info["authority"] = normalized_authority
            reference["authority"] = normalized_authority
            provenance["authority"] = normalized_authority

        normalized_provider = str(provider or "").strip()
        normalized_as_of = str(as_of or "").strip()
        normalized_freshness = str(freshness or "").strip()
        domain = metadata_domain or _web_reference_domain(normalized_url)

        if not metadata_items:
            metadata_items = [
                {
                    "source": normalized_url,
                    "name": normalized_title,
                    "url": normalized_url,
                }
            ]

        primary_metadata = metadata_items[0]
        primary_metadata.setdefault("source", normalized_url)
        primary_metadata.setdefault("name", normalized_title)
        primary_metadata.setdefault("url", normalized_url)
        if tool_name:
            primary_metadata.setdefault("tool_name", tool_name)
            primary_metadata.setdefault("retrieval_tool_name", tool_name)
        if normalized_provider:
            primary_metadata.setdefault("provider", normalized_provider)
        if retrieval_round not in (None, "", [], {}):
            primary_metadata.setdefault("retrieval_round", retrieval_round)
        if normalized_source_class:
            primary_metadata.setdefault("source_class", normalized_source_class)
        if normalized_authority:
            primary_metadata.setdefault("authority", normalized_authority)
        if normalized_as_of:
            primary_metadata.setdefault("as_of", normalized_as_of)
        if normalized_freshness:
            primary_metadata.setdefault("freshness", normalized_freshness)
        if domain:
            primary_metadata.setdefault("domain", domain)
        for key, value in _safe_query_provenance_fields(query).items():
            primary_metadata.setdefault(key, value)

        if tool_name:
            provenance.setdefault("tool_name", tool_name)
        if normalized_provider:
            provenance.setdefault("provider", normalized_provider)
        if retrieval_round not in (None, "", [], {}):
            provenance.setdefault("retrieval_round", retrieval_round)
        if normalized_as_of:
            provenance.setdefault("as_of", normalized_as_of)
        if normalized_freshness:
            provenance.setdefault("freshness", normalized_freshness)
        if domain:
            provenance.setdefault("domain", domain)
        provenance.setdefault("source_id", source_info["id"])
        for key, value in _safe_query_provenance_fields(query).items():
            provenance.setdefault(key, value)

        reference["source"] = source_info
        reference["metadata"] = metadata_items
        reference["provenance"] = provenance
        if normalized_as_of:
            reference["as_of"] = normalized_as_of
        if normalized_freshness:
            reference["freshness"] = normalized_freshness

        return reference

    def _normalize_explicit_web_reference_candidate(
        self,
        candidate: object,
        *,
        tool_name: str,
        query: str = "",
        retrieval_round: object = None,
        provider: object = "",
        as_of: object = None,
        freshness: object = None,
    ) -> Optional[dict]:
        if not isinstance(candidate, dict):
            return None

        if any(
            key in candidate
            for key in ("source", "document", "documents", "metadata", "metadatas")
        ):
            reference = self._normalize_canonical_reference(candidate)
            if not reference:
                return None
            return self._enrich_normalized_web_reference(
                reference,
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
                provider=provider,
                as_of=as_of,
                freshness=freshness,
            )

        return self._normalize_web_reference(
            tool_name=tool_name,
            url=_coalesce_mapping_value(
                candidate,
                keys=("url", "link", "source_url", "sourceUrl"),
            ),
            title=_coalesce_mapping_value(
                candidate,
                keys=("title", "name", "source_name", "sourceName", "url", "link"),
            ),
            content=_coalesce_mapping_value(
                candidate,
                keys=("content", "snippet", "summary", "text", "title"),
            ),
            query=query,
            retrieval_round=retrieval_round,
            provider=_coalesce_mapping_value(
                candidate,
                keys=("provider", "search_provider", "engine"),
            )
            or provider,
            source_class=_coalesce_mapping_value(
                candidate,
                keys=("source_class", "sourceClass", "source_type", "sourceType", "type"),
            ),
            authority=_coalesce_mapping_value(
                candidate,
                keys=("authority", "authority_level", "source_authority"),
            ),
            as_of=_coalesce_mapping_value(
                candidate,
                keys=("as_of", "asOf", "retrieved_at", "retrievedAt", "timestamp"),
            )
            or as_of,
            freshness=_coalesce_mapping_value(
                candidate,
                keys=("freshness", "age", "published_at", "publishedAt", "date"),
            )
            or freshness,
        )

    def _filter_retrieval_diagnostics_for_accepted_references(
        self, diagnostics: list[dict], references: list[dict]
    ) -> list[dict]:
        if not references or not diagnostics:
            return diagnostics

        accepted_web_references = 0
        for reference in references:
            if not isinstance(reference, dict):
                continue
            source_info = reference.get("source")
            source_info = source_info if isinstance(source_info, dict) else {}
            normalized_source_class = _normalize_web_source_class(
                reference.get("source_class") or source_info.get("type"),
                url=source_info.get("url") or source_info.get("id") or "",
                authority=reference.get("authority") or source_info.get("authority") or "",
            )
            if normalized_source_class in {"official_web", "generic_web"}:
                accepted_web_references += 1

        if not accepted_web_references:
            return diagnostics

        return [
            item
            for item in diagnostics
            if not (
                isinstance(item, dict)
                and str(item.get("classification") or "").strip().lower()
                == "no_evidence"
            )
        ]

    def _normalize_web_search_payload(
        self,
        *,
        tool_name: str,
        payload: object,
        tool_args: dict,
    ) -> dict:
        payload_dict = payload if isinstance(payload, dict) else {}
        results = []
        if isinstance(payload_dict.get("results"), list):
            results = payload_dict.get("results") or []
        elif isinstance(payload_dict.get("data"), list):
            results = payload_dict.get("data") or []
        elif isinstance(payload, list):
            results = payload

        query = str(
            tool_args.get("query")
            or payload_dict.get("query")
            or payload_dict.get("q")
            or ""
        ).strip()
        query_fields = _safe_query_provenance_fields(query)
        query_digest = query_fields.get("query_digest", "")
        status = str(payload_dict.get("status") or "").strip().lower()
        provider = str(
            payload_dict.get("provider")
            or payload_dict.get("search_provider")
            or payload_dict.get("engine")
            or ""
        ).strip()
        retrieval_round = _coalesce_mapping_value(
            payload_dict,
            keys=("retrieval_round", "retrievalRound", "attempt"),
        )
        as_of = _coalesce_mapping_value(
            payload_dict,
            keys=("as_of", "asOf", "retrieved_at", "retrievedAt", "timestamp"),
        )
        freshness = _coalesce_mapping_value(
            payload_dict,
            keys=("freshness", "age", "published_at", "publishedAt"),
        )

        explicit_reason = str(
            _coalesce_mapping_value(
                payload_dict,
                keys=("reason", "code", "error", "message", "detail"),
            )
            or ""
        ).strip()
        if explicit_reason and not status:
            status = "error"

        if status in {"error", "failed", "failure", "timeout", "timed_out", "blocked", "denied", "weak", "empty"}:
            reason = explicit_reason or (
                "empty_search_results"
                if status == "empty"
                else "weak_search_results"
                if status == "weak"
                else "search_result_denied"
                if status == "denied"
                else "search_result_blocked"
                if status == "blocked"
                else "web_search_timeout"
                if status in {"timeout", "timed_out"}
                else "web_search_error"
            )
            return {
                "tool_name": tool_name,
                "retrieval_diagnostics": [
                    self._web_reference_diagnostic(
                        tool_name=tool_name,
                        status=status,
                        reason=reason,
                        query=query,
                        query_digest=query_digest,
                        retrieval_round=retrieval_round,
                        provider=provider,
                        detail=payload_dict.get("detail") or explicit_reason,
                    )
                ],
            }

        explicit_reference_groups = [
            payload_dict.get("canonical_references"),
            payload_dict.get("accepted_references"),
            payload_dict.get("accepted_results"),
            payload_dict.get("selected_results"),
            payload_dict.get("used_results"),
        ]
        explicit_candidates = []
        for group in explicit_reference_groups:
            explicit_candidates.extend(self._iter_source_items(group))

        if explicit_candidates:
            references: list[dict] = []
            for candidate in explicit_candidates:
                reference = self._normalize_explicit_web_reference_candidate(
                    candidate,
                    tool_name=tool_name,
                    query=query,
                    retrieval_round=retrieval_round,
                    provider=provider,
                    as_of=as_of,
                    freshness=freshness,
                )
                if reference:
                    references.append(reference)

            if references:
                return {
                    "tool_name": tool_name,
                    "canonical_references": references,
                }

            return {
                "tool_name": tool_name,
                "retrieval_diagnostics": [
                    self._web_reference_diagnostic(
                        tool_name=tool_name,
                        status="malformed",
                        reason="malformed_search_results",
                        query=query,
                        query_digest=query_digest,
                        retrieval_round=retrieval_round,
                        provider=provider,
                    )
                ],
            }

        if results:
            return {"tool_name": tool_name}

        return {
            "tool_name": tool_name,
            "retrieval_diagnostics": [
                self._web_reference_diagnostic(
                    tool_name=tool_name,
                    status="empty",
                    reason="empty_search_results",
                    query=query,
                    query_digest=query_digest,
                    retrieval_round=retrieval_round,
                    provider=provider,
                )
            ],
        }

    def _normalize_webpage_payload(
        self,
        *,
        tool_name: str,
        payload: object,
        tool_args: dict,
    ) -> dict:
        payload_dict = payload if isinstance(payload, dict) else {}
        url = str(
            tool_args.get("url")
            or payload_dict.get("url")
            or payload_dict.get("source_url")
            or ""
        ).strip()
        status = str(payload_dict.get("status") or "").strip().lower()
        provider = str(payload_dict.get("provider") or "").strip()
        retrieval_round = _coalesce_mapping_value(
            payload_dict,
            keys=("retrieval_round", "retrievalRound", "attempt"),
        )
        as_of = _coalesce_mapping_value(
            payload_dict,
            keys=("as_of", "asOf", "retrieved_at", "retrievedAt", "timestamp"),
        )
        freshness = _coalesce_mapping_value(
            payload_dict,
            keys=("freshness", "age", "published_at", "publishedAt", "date"),
        )
        source_class = _coalesce_mapping_value(
            payload_dict,
            keys=("source_class", "sourceClass", "source_type", "sourceType", "type"),
        )
        authority = _coalesce_mapping_value(
            payload_dict,
            keys=("authority", "authority_level", "source_authority"),
        )
        explicit_reason = str(
            _coalesce_mapping_value(
                payload_dict,
                keys=("reason", "code", "error", "message", "detail"),
            )
            or ""
        ).strip()
        if explicit_reason and not status:
            status = "error"

        if status in {"error", "failed", "failure", "timeout", "timed_out", "blocked", "denied", "weak", "empty"}:
            reason = explicit_reason or (
                "empty_webpage_result"
                if status == "empty"
                else "weak_webpage_result"
                if status == "weak"
                else "webpage_result_denied"
                if status == "denied"
                else "webpage_result_blocked"
                if status == "blocked"
                else "webpage_read_timeout"
                if status in {"timeout", "timed_out"}
                else "webpage_read_error"
            )
            return {
                "tool_name": tool_name,
                "retrieval_diagnostics": [
                    self._web_reference_diagnostic(
                        tool_name=tool_name,
                        status=status,
                        reason=reason,
                        url=url,
                        retrieval_round=retrieval_round,
                        provider=provider,
                        source_class=str(source_class or ""),
                        authority=str(authority or ""),
                        detail=payload_dict.get("detail") or explicit_reason,
                    )
                ],
            }

        content = payload if isinstance(payload, str) else _coalesce_mapping_value(
            payload_dict,
            keys=("content", "text", "summary", "snippet", "message", "detail"),
        )
        title = _coalesce_mapping_value(
            payload_dict,
            keys=("title", "name", "url"),
        ) or url
        reference = self._normalize_web_reference(
            tool_name=tool_name,
            url=url,
            title=title,
            content=content,
            retrieval_round=retrieval_round,
            provider=provider,
            source_class=source_class,
            authority=authority,
            as_of=as_of,
            freshness=freshness,
        )
        if reference:
            return {
                "tool_name": tool_name,
                "canonical_references": [reference],
            }

        return {
            "tool_name": tool_name,
            "retrieval_diagnostics": [
                self._web_reference_diagnostic(
                    tool_name=tool_name,
                    status="empty" if not str(content or "").strip() else "malformed",
                    reason="empty_webpage_result"
                    if not str(content or "").strip()
                    else "malformed_webpage_result",
                    url=url,
                    retrieval_round=retrieval_round,
                    provider=provider,
                    source_class=str(source_class or ""),
                    authority=str(authority or ""),
                )
            ],
        }

    def _iter_web_tool_output_payloads(self, output: object) -> list[dict]:
        if not isinstance(output, list):
            return []

        function_context_by_call_id = _function_call_context_by_call_id(output)
        payloads: list[dict] = []
        malformed_payload_count = 0

        for item in output:
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue

            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            function_context = function_context_by_call_id.get(call_id) or {}
            tool_name = str(
                item.get("name")
                or function_context.get("name")
                or ""
            ).strip().lower()
            if not tool_name or (
                not _is_search_tool_name(tool_name)
                and not _is_webpage_tool_name(tool_name)
            ):
                continue

            parsed_payload = _extract_function_call_output_payload(item)
            if parsed_payload in (None, "", []):
                malformed_payload_count += 1
                continue

            tool_args = function_context.get("arguments")
            tool_args = tool_args if isinstance(tool_args, dict) else {}
            if _is_search_tool_name(tool_name):
                payloads.append(
                    self._normalize_web_search_payload(
                        tool_name=tool_name,
                        payload=parsed_payload,
                        tool_args=tool_args,
                    )
                )
            else:
                payloads.append(
                    self._normalize_webpage_payload(
                        tool_name=tool_name,
                        payload=parsed_payload,
                        tool_args=tool_args,
                    )
                )

        if payloads or malformed_payload_count:
            observe_llm_event(
                "reference.web_tool_payload.normalize",
                {
                    "payload_count": len(payloads),
                    "malformed_payload_count": malformed_payload_count,
                },
            )

        return payloads

    def _is_chat_completion_source_wrapper(self, value: object) -> bool:
        if not isinstance(value, dict):
            return False

        # Realtime completion payloads can be echoed back through legacy
        # source lanes. Their nested metadata may be useful, but the wrapper
        # is the assistant response, not citeable retrieval evidence.
        if not isinstance(value.get("output"), list):
            return False
        if value.get("done") is not True and "content" not in value:
            return False

        has_retrieval_payload = any(
            value.get(key) not in (None, "", [], {})
            for key in ("document", "documents")
        )
        metadata_payload = value.get("metadata")
        metadatas_payload = value.get("metadatas")
        if isinstance(metadata_payload, list) and metadata_payload:
            has_retrieval_payload = True
        if isinstance(metadatas_payload, list) and metadatas_payload:
            has_retrieval_payload = True

        return not has_retrieval_payload

    def _iter_source_items(self, value: object) -> list[dict]:
        if isinstance(value, list):
            items: list[dict] = []
            for item in value:
                items.extend(self._iter_source_items(item))
            return items

        if not isinstance(value, dict):
            return []

        nested_items: list[dict] = []
        for key in (
            "canonical_references",
            "sources",
            "citations",
            "references",
            "retrieval_diagnostics",
        ):
            nested_items.extend(self._iter_source_items(value.get(key)))
        if nested_items:
            return nested_items

        if self._is_chat_completion_source_wrapper(value):
            nested_items.extend(self._iter_source_items(value.get("metadata")))
            nested_items.extend(self._iter_source_items(value.get("data")))
            return nested_items

        if not any(
            key in value for key in ("source", "document", "metadata", "metadatas")
        ):
            nested_items.extend(self._iter_source_items(value.get("documents")))
            if nested_items:
                return nested_items

        data = value.get("data")
        if isinstance(data, (dict, list)):
            data_items = self._iter_source_items(data)
            if data_items:
                return data_items

        return [value]

    def _normalize_canonical_reference(self, source: object) -> Optional[dict]:
        if not isinstance(source, dict):
            return None
        if self._is_retrieval_diagnostic_item(source):
            return None
        if self._is_chat_completion_source_wrapper(source):
            return None
        if source.get("type") == "code_execution":
            return None

        reference = json.loads(json.dumps(source, ensure_ascii=False, default=str))
        if isinstance(reference.get("data"), dict):
            reference = reference["data"]
        if not isinstance(reference, dict):
            return None

        if self._is_retrieval_diagnostic_item(reference):
            return None
        if self._is_chat_completion_source_wrapper(reference):
            return None
        if reference.get("type") == "code_execution":
            return None

        if "document" not in reference and isinstance(reference.get("documents"), list):
            reference["document"] = reference.get("documents")
        if "metadata" not in reference and isinstance(reference.get("metadatas"), list):
            reference["metadata"] = reference.get("metadatas")

        source_info = reference.get("source")
        if isinstance(source_info, str):
            source_info = {"name": source_info}
        elif not isinstance(source_info, dict):
            source_info = {}

        for source_key, target_key in (
            ("source_id", "id"),
            ("id", "id"),
            ("source_name", "name"),
            ("name", "name"),
            ("title", "name"),
            ("file_name", "name"),
            ("filename", "name"),
            ("url", "url"),
            ("source_url", "url"),
            ("source_type", "type"),
            ("type", "type"),
            ("source_class", "type"),
            ("authority", "authority"),
        ):
            value = reference.get(source_key)
            if value not in (None, "", [], {}) and not source_info.get(target_key):
                source_info[target_key] = value

        if source_info:
            reference["source"] = source_info

        source_class = str(
            reference.get("source_class")
            or source_info.get("type")
            or ""
        ).strip()
        if source_class:
            reference["source_class"] = source_class

        authority = str(
            reference.get("authority")
            or source_info.get("authority")
            or ""
        ).strip()
        if authority:
            reference["authority"] = authority

        provenance = reference.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        for metadata in reference.get("metadata") or []:
            if not isinstance(metadata, dict):
                continue
            for source_key, target_key in (
                ("retrieval_tool_name", "tool_name"),
                ("tool_name", "tool_name"),
                ("retrieval_round", "retrieval_round"),
                ("query", "query"),
                ("file_id", "file_id"),
                ("knowledge_id", "knowledge_id"),
                ("dataset_id", "dataset_id"),
                ("document_id", "document_id"),
                ("chunk_id", "chunk_id"),
                ("page", "page"),
                ("section", "section"),
                ("query_digest", "query_digest"),
                ("provider", "provider"),
                ("domain", "domain"),
                ("source_class", "source_class"),
                ("authority", "authority"),
                ("as_of", "as_of"),
                ("freshness", "freshness"),
                ("published_at", "published_at"),
            ):
                value = metadata.get(source_key)
                if value not in (None, "", [], {}) and not provenance.get(target_key):
                    provenance[target_key] = value

        if isinstance(source_info, dict):
            for source_key, target_key in (
                ("id", "source_id"),
                ("file_id", "file_id"),
                ("knowledge_id", "knowledge_id"),
                ("note_id", "note_id"),
                ("type", "source_type"),
                ("authority", "authority"),
            ):
                value = source_info.get(source_key)
                if value not in (None, "", [], {}) and not provenance.get(target_key):
                    provenance[target_key] = value

        for source_key, target_key in (
            ("source_class", "source_class"),
            ("authority", "authority"),
            ("as_of", "as_of"),
            ("freshness", "freshness"),
        ):
            value = reference.get(source_key)
            if value not in (None, "", [], {}) and not provenance.get(target_key):
                provenance[target_key] = value

        if provenance:
            reference["provenance"] = provenance

        reference = self._enrich_normalized_web_reference(
            reference,
            tool_name=str(provenance.get("tool_name") or "").strip(),
            query=str(provenance.get("query") or "").strip(),
            retrieval_round=provenance.get("retrieval_round"),
            provider=provenance.get("provider") or "",
            as_of=provenance.get("as_of"),
            freshness=provenance.get("freshness"),
        )
        if not reference:
            return None

        has_identity = any(
            value not in (None, "", [], {})
            for value in (
                reference.get("source"),
                reference.get("document"),
                reference.get("metadata"),
                reference.get("id"),
            )
        )
        if not has_identity:
            return None

        reference["type"] = "retrieval_reference"
        return reference

    def build_canonical_references(self, *reference_groups: object) -> list[dict]:
        references: list[dict] = []
        seen: dict[str, int] = {}
        candidate_count = 0
        dedupe_count = 0
        malformed_or_empty_input_count = 0

        for group in reference_groups:
            for source in self._iter_source_items(group):
                candidate_count += 1
                reference = self._normalize_canonical_reference(source)
                if not reference:
                    malformed_or_empty_input_count += 1
                    continue
                signature = self._source_signature(reference)
                if not signature:
                    malformed_or_empty_input_count += 1
                    continue
                if signature in seen:
                    dedupe_count += 1
                    self._merge_reference_provenance(
                        references[seen[signature]],
                        reference,
                    )
                    continue
                seen[signature] = len(references)
                references.append(reference)

        if candidate_count or references:
            observe_llm_event(
                "reference.normalize",
                {
                    "input_group_count": len(reference_groups),
                    "candidate_count": candidate_count,
                    "canonical_reference_count": len(references),
                    "dedupe_count": dedupe_count,
                    "malformed_or_empty_input_count": malformed_or_empty_input_count,
                },
            )

        return references

    def _merge_reference_provenance(
        self, existing_reference: dict, incoming_reference: dict
    ) -> None:
        if not isinstance(existing_reference, dict) or not isinstance(
            incoming_reference, dict
        ):
            return

        incoming = incoming_reference.get("provenance")
        if not isinstance(incoming, dict) or not incoming:
            return

        existing = existing_reference.get("provenance")
        if not isinstance(existing, dict):
            existing_reference["provenance"] = dict(incoming)
            return

        for key, value in incoming.items():
            if value in (None, "", [], {}):
                continue
            if existing.get(key) in (None, "", [], {}):
                existing[key] = value
            elif existing.get(key) != value:
                additional = existing.setdefault("additional_provenance", [])
                if isinstance(additional, list) and incoming not in additional:
                    additional.append(dict(incoming))

    def _merge_retrieval_diagnostics(self, *diagnostic_groups: object) -> list[dict]:
        diagnostics: list[dict] = []
        seen: set[str] = set()
        candidate_count = 0
        dedupe_count = 0
        ignored_input_count = 0

        for group in diagnostic_groups:
            if not isinstance(group, (dict, list)):
                continue
            items = self._iter_source_items(group)

            for item in items:
                candidate_count += 1
                if not self._is_retrieval_diagnostic_item(item):
                    ignored_input_count += 1
                    continue
                try:
                    diagnostic = json.loads(
                        json.dumps(item, ensure_ascii=False, default=str)
                    )
                    signature = json.dumps(
                        diagnostic, ensure_ascii=False, sort_keys=True, default=str
                    )
                except (TypeError, ValueError):
                    ignored_input_count += 1
                    continue
                if signature in seen:
                    dedupe_count += 1
                    continue
                seen.add(signature)
                diagnostics.append(diagnostic)

        if candidate_count or diagnostics:
            observe_llm_event(
                "reference.diagnostics.normalize",
                {
                    "candidate_count": candidate_count,
                    "diagnostic_count": len(diagnostics),
                    "dedupe_count": dedupe_count,
                    "ignored_input_count": ignored_input_count,
                    "reason_codes": diagnostic_reason_codes(diagnostics),
                    "classification_counts": diagnostic_classification_counts(
                        diagnostics
                    ),
                },
            )

        return diagnostics

    def _normalize_active_source_scope_authority(
        self, scope: dict, reason: str, sources: list[dict]
    ) -> str:
        authority = str(
            scope.get("authority")
            or scope.get("source_authority")
            or scope.get("sourceAuthority")
            or ""
        ).strip()
        if authority:
            return authority

        reason_key = reason.strip().lower()
        if reason_key in {"current_turn_upload", "current_preview"}:
            return "current_upload"
        if reason_key in {"explicit_anchor", "selected_source"}:
            return "selected_source"
        if reason_key == "user_clarified_deictic_reference":
            return "user_clarification"
        if reason_key == "previous_single_canonical_reference":
            return "canonical_reference"
        if reason_key in {"ambiguous_retrieval_scope", "expired_or_conflicting"}:
            return "diagnostic"
        return "selected_source" if sources else "none"

    def _derive_active_source_focus_state(
        self,
        *,
        requested_focus_state: str,
        status: str,
        source_set_mode: str,
        source_ids: list[str],
        reason: str,
    ) -> str:
        if requested_focus_state in {"none", "single", "multi", "ambiguous"}:
            return requested_focus_state
        if status == "ambiguous" or reason == "ambiguous_retrieval_scope":
            return "ambiguous"
        if status == "resolved":
            if source_set_mode == "multi" or len(source_ids) > 1:
                return "multi"
            if source_set_mode == "single" or len(source_ids) == 1:
                return "single"
        return "none"

    def _normalize_active_source_scope_source(
        self, source: object, fallback_authority: str
    ) -> Optional[dict]:
        if not isinstance(source, dict):
            return None

        source_id = str(
            source.get("id")
            or source.get("file_id")
            or source.get("knowledge_id")
            or source.get("url")
            or ""
        ).strip()
        name = str(
            source.get("name")
            or source.get("filename")
            or source.get("title")
            or source_id
            or ""
        ).strip()
        if not source_id and not name:
            return None

        normalized = {
            "id": source_id or name,
            "name": name or source_id,
            "type": str(source.get("type") or source.get("source_class") or "unknown").strip()
            or "unknown",
        }
        authority = str(
            source.get("authority")
            or source.get("source_authority")
            or fallback_authority
            or ""
        ).strip()
        if authority:
            normalized["authority"] = authority
        return normalized

    def normalize_active_source_scope(self, scope: object) -> Optional[dict]:
        if not isinstance(scope, dict) or not scope:
            return None

        status = str(scope.get("status") or "").strip().lower()
        source_set_mode = str(scope.get("source_set_mode") or "").strip().lower()
        reason = str(
            scope.get("validity_reason")
            or scope.get("reason")
            or scope.get("ambiguity_reason")
            or scope.get("expiration_reason")
            or ""
        ).strip()

        source_ids: list[str] = []
        for value in scope.get("source_ids") or []:
            normalized = str(value or "").strip()
            if normalized and normalized not in source_ids:
                source_ids.append(normalized)

        preliminary_authority = self._normalize_active_source_scope_authority(
            scope, reason, []
        )
        sources: list[dict] = []
        seen_sources: set[str] = set()
        for source in scope.get("sources") or []:
            normalized_source = self._normalize_active_source_scope_source(
                source,
                preliminary_authority,
            )
            if not normalized_source:
                continue
            signature = json.dumps(
                normalized_source,
                ensure_ascii=False,
                sort_keys=True,
            )
            if signature in seen_sources:
                continue
            seen_sources.add(signature)
            sources.append(normalized_source)
            source_id = str(normalized_source.get("id") or "").strip()
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)

        authority = self._normalize_active_source_scope_authority(
            scope, reason, sources
        )
        focus_state = self._derive_active_source_focus_state(
            requested_focus_state=str(scope.get("focus_state") or "").strip().lower(),
            status=status,
            source_set_mode=source_set_mode,
            source_ids=source_ids,
            reason=reason,
        )

        normalized = dict(scope)
        normalized["focus_state"] = focus_state
        normalized["source_ids"] = source_ids
        normalized["sources"] = sources
        normalized["authority"] = authority

        if not status:
            normalized["status"] = (
                "resolved"
                if focus_state in {"single", "multi"}
                else "ambiguous"
                if focus_state == "ambiguous"
                else "none"
            )
        if not source_set_mode:
            normalized["source_set_mode"] = (
                "single"
                if focus_state == "single"
                else "multi"
                if focus_state == "multi"
                else "none"
            )

        if focus_state in {"single", "multi"}:
            normalized["validity_reason"] = (
                str(scope.get("validity_reason") or reason or "resolved_source_scope")
                .strip()
            )
            normalized.pop("ambiguity_reason", None)
            normalized.pop("expiration_reason", None)
        elif focus_state == "ambiguous":
            normalized["ambiguity_reason"] = (
                str(scope.get("ambiguity_reason") or reason or "ambiguous_retrieval_scope")
                .strip()
            )
            normalized.pop("validity_reason", None)
        else:
            expiration_reason = str(
                scope.get("expiration_reason")
                or (
                    reason
                    if status in {"expired", "none"}
                    or source_set_mode == "none"
                    else ""
                )
            ).strip()
            if expiration_reason:
                normalized["expiration_reason"] = expiration_reason
            normalized.pop("validity_reason", None)

        if not str(normalized.get("expires_on") or "").strip():
            normalized["expires_on"] = "new_upload_or_explicit_change"

        follow_up = (
            dict(scope.get("follow_up"))
            if isinstance(scope.get("follow_up"), dict)
            else {}
        )
        follow_up["reuse"] = focus_state in {"single", "multi"}
        if focus_state in {"single", "multi"}:
            follow_up.setdefault("reuse_hint", "reuse_if_authorized_and_not_conflicting")
            follow_up.setdefault(
                "reject_hints",
                [
                    "new_upload",
                    "explicit_source_change",
                    "conflicting_anchor",
                    "permission_invalidation",
                ],
            )
        else:
            follow_up.setdefault("reuse_hint", "do_not_reuse_without_clarification")
            follow_up.setdefault(
                "reject_hints",
                [
                    "ambiguous_scope",
                    "expired_focus",
                    "unauthorized_source",
                    "no_evidence",
                ],
            )
        normalized["follow_up"] = follow_up

        return normalized

    def _active_source_scope_suppresses_references(self, scope: object) -> bool:
        if not isinstance(scope, dict):
            return False

        focus_state = str(scope.get("focus_state") or "").strip().lower()
        if focus_state == "ambiguous":
            return True

        status = str(scope.get("status") or "").strip().lower()
        source_set_mode = str(scope.get("source_set_mode") or "").strip().lower()
        reason = str(
            scope.get("reason")
            or scope.get("ambiguity_reason")
            or scope.get("expiration_reason")
            or ""
        ).strip().lower()
        return status in {"ambiguous", "expired"} or (
            source_set_mode == "none"
            and reason in {"ambiguous_retrieval_scope", "expired_or_conflicting"}
        )

    def _diagnostics_suppress_references(self, diagnostics: object) -> bool:
        if not isinstance(diagnostics, list):
            return False

        return any(
            isinstance(item, dict)
            and str(item.get("reason") or "").strip().lower()
            == "ambiguous_retrieval_scope"
            for item in diagnostics
        )

    def _diagnostics_indicate_no_evidence(self, diagnostics: object) -> bool:
        if not isinstance(diagnostics, list):
            return False

        return any(
            isinstance(item, dict)
            and str(item.get("classification") or "").strip().lower()
            == "no_evidence"
            for item in diagnostics
        )

    def _should_suppress_canonical_references(
        self, *, metadata: object = None, diagnostics: object = None
    ) -> bool:
        metadata = metadata if isinstance(metadata, dict) else {}
        merged_diagnostics = self._merge_retrieval_diagnostics(
            metadata.get("retrieval_diagnostics"),
            diagnostics,
        )
        return self._active_source_scope_suppresses_references(
            metadata.get("active_source_scope")
        ) or self._diagnostics_suppress_references(merged_diagnostics)

    def _should_clear_reference_aliases(
        self,
        *,
        metadata: object = None,
        diagnostics: object = None,
        accepted_references: object = None,
    ) -> bool:
        metadata = metadata if isinstance(metadata, dict) else {}
        merged_diagnostics = self._merge_retrieval_diagnostics(
            metadata.get("retrieval_diagnostics"),
            diagnostics,
        )

        if self._active_source_scope_suppresses_references(
            metadata.get("active_source_scope")
        ) or self._diagnostics_suppress_references(merged_diagnostics):
            return True

        accepted_references = (
            accepted_references if isinstance(accepted_references, list) else []
        )
        return (
            not accepted_references
            and self._diagnostics_indicate_no_evidence(merged_diagnostics)
        )

    def _should_hide_retrieval_status_history(
        self,
        *,
        message: dict,
        canonical_references: list[dict],
        retrieval_diagnostics: list[dict],
        metadata: dict,
    ) -> bool:
        if not isinstance(message, dict) or message.get("done") is not True:
            return False

        status_history = message.get("statusHistory")
        if not isinstance(status_history, list):
            status_history = message.get("status_history")
        if not isinstance(status_history, list) or not status_history:
            return False

        retrieval_status_actions = {
            "knowledge_search",
            "queries_generated",
            "sources_retrieved",
        }
        visible_statuses = [
            status
            for status in status_history
            if isinstance(status, dict) and status.get("hidden") is not True
        ]
        if not visible_statuses or canonical_references:
            return False

        visible_actions = {
            str(status.get("action") or "").strip()
            for status in visible_statuses
            if str(status.get("action") or "").strip()
        }
        if not visible_actions.intersection(retrieval_status_actions):
            return False
        if any(
            action not in retrieval_status_actions and action != "chat"
            for action in visible_actions
        ):
            return False

        if any(
            str(status.get("action") or "").strip() == "sources_retrieved"
            and isinstance(status.get("count"), (int, float))
            and status.get("count", 0) > 0
            for status in status_history
            if isinstance(status, dict)
        ):
            return False

        if self._should_suppress_canonical_references(
            metadata=metadata,
            diagnostics=retrieval_diagnostics,
        ):
            return True

        if any(
            isinstance(item, dict)
            and str(item.get("classification") or "").strip().lower()
            == "no_evidence"
            for item in retrieval_diagnostics
        ):
            return True

        if any(
            str(status.get("action") or "").strip() == "sources_retrieved"
            and status.get("count") == 0
            for status in status_history
            if isinstance(status, dict)
        ):
            return True

        content = message.get("content")
        return isinstance(content, str) and content.strip() == "NO_FILE_ACCESS"

    def _strip_suppressed_reference_citation_markers(
        self, content: object
    ) -> object:
        if not isinstance(content, str) or "[" not in content:
            return content

        cleaned = _SUPPRESSED_REFERENCE_CITATION_PATTERN.sub("", content)
        cleaned = _SUPPRESSED_REFERENCE_PUNCTUATION_PATTERN.sub(r"\1", cleaned)
        cleaned = _SUPPRESSED_REFERENCE_DOUBLE_SPACE_PATTERN.sub(" ", cleaned)
        return cleaned.strip()

    def _strip_suppressed_reference_citation_markers_from_output(
        self, output: object
    ) -> object:
        if not isinstance(output, list):
            return output

        changed = False
        cleaned_output = []

        for item in output:
            if not isinstance(item, dict):
                cleaned_output.append(item)
                continue

            cleaned_item = dict(item)
            item_changed = False

            if isinstance(cleaned_item.get("text"), str):
                cleaned_text = self._strip_suppressed_reference_citation_markers(
                    cleaned_item.get("text")
                )
                if cleaned_text != cleaned_item.get("text"):
                    cleaned_item["text"] = cleaned_text
                    item_changed = True

            content = cleaned_item.get("content")
            if isinstance(content, str):
                cleaned_content = self._strip_suppressed_reference_citation_markers(
                    content
                )
                if cleaned_content != content:
                    cleaned_item["content"] = cleaned_content
                    item_changed = True
            elif isinstance(content, list):
                cleaned_content = []
                content_changed = False
                for content_item in content:
                    if not isinstance(content_item, dict):
                        cleaned_content.append(content_item)
                        continue

                    cleaned_content_item = dict(content_item)
                    if isinstance(cleaned_content_item.get("text"), str):
                        cleaned_text = self._strip_suppressed_reference_citation_markers(
                            cleaned_content_item.get("text")
                        )
                        if cleaned_text != cleaned_content_item.get("text"):
                            cleaned_content_item["text"] = cleaned_text
                            content_changed = True
                    cleaned_content.append(cleaned_content_item)

                if content_changed:
                    cleaned_item["content"] = cleaned_content
                    item_changed = True

            cleaned_output.append(cleaned_item if item_changed else item)
            changed = changed or item_changed

        return cleaned_output if changed else output

    def _hide_retrieval_status_history(self, message: dict) -> tuple[dict, bool]:
        if not isinstance(message, dict):
            return message, False

        status_history_key = None
        status_history = None
        for key in ("statusHistory", "status_history"):
            if isinstance(message.get(key), list):
                status_history_key = key
                status_history = message.get(key)
                break

        if not status_history_key or not isinstance(status_history, list):
            return message, False

        retrieval_status_actions = {
            "knowledge_search",
            "queries_generated",
            "sources_retrieved",
        }
        changed = False
        updated_status_history = []

        for status in status_history:
            if not isinstance(status, dict):
                updated_status_history.append(status)
                continue

            action = str(status.get("action") or "").strip()
            if action not in retrieval_status_actions or status.get("hidden") is True:
                updated_status_history.append(status)
                continue

            updated_status_history.append({**status, "hidden": True})
            changed = True

        normalized_message = message
        if changed:
            normalized_message = {**normalized_message, status_history_key: updated_status_history}

        status = normalized_message.get("status")
        if isinstance(status, dict):
            action = str(status.get("action") or "").strip()
            if action in retrieval_status_actions and status.get("hidden") is not True:
                if normalized_message is message:
                    normalized_message = dict(normalized_message)
                normalized_message["status"] = {**status, "hidden": True}
                changed = True

        return normalized_message, changed

    def build_reference_metadata_sidecar(
        self,
        *,
        metadata: object = None,
        sources: object = None,
        diagnostics: object = None,
        tool_outputs: object = None,
    ) -> dict:
        metadata = metadata if isinstance(metadata, dict) else {}
        active_source_scope = self.normalize_active_source_scope(
            metadata.get("active_source_scope")
        )
        tool_reference_payloads = [
            *self._iter_retrieval_tool_output_payloads(tool_outputs),
            *self._iter_web_tool_output_payloads(tool_outputs),
        ]
        source_groups = [
            metadata.get("canonical_references"),
            metadata.get("references"),
            metadata.get("sources"),
            metadata.get("citations"),
            metadata.get("documents"),
            sources,
            tool_reference_payloads,
        ]
        explicit_accepted_references = self.build_canonical_references(
            metadata.get("canonical_references"),
            tool_reference_payloads,
        )
        references = self.build_canonical_references(
            *source_groups,
        )
        retrieval_diagnostics = self._merge_retrieval_diagnostics(
            metadata.get("retrieval_diagnostics"),
            diagnostics,
            *source_groups,
        )
        retrieval_diagnostics = (
            self._filter_retrieval_diagnostics_for_accepted_references(
                retrieval_diagnostics,
                explicit_accepted_references,
            )
        )
        if self._diagnostics_indicate_no_evidence(retrieval_diagnostics):
            references = []
        if self._should_suppress_canonical_references(
            metadata=metadata,
            diagnostics=retrieval_diagnostics,
        ):
            references = []

        sidecar: dict = {}
        if active_source_scope:
            sidecar["active_source_scope"] = active_source_scope
        if references:
            sidecar["canonical_references"] = references
        if retrieval_diagnostics:
            sidecar["retrieval_diagnostics"] = retrieval_diagnostics
        observe_llm_event(
            "reference.sidecar.build",
            {
                "metadata_present": bool(metadata),
                "source_group_count": len(source_groups),
                "retrieval_tool_payload_count": len(tool_reference_payloads),
                "active_source_focus_state": (
                    active_source_scope.get("focus_state")
                    if isinstance(active_source_scope, dict)
                    else None
                ),
                "canonical_reference_count": len(references),
                "diagnostic_count": len(retrieval_diagnostics),
                "result_status": (
                    "mixed"
                    if references and retrieval_diagnostics
                    else "references"
                    if references
                    else "diagnostics"
                    if retrieval_diagnostics
                    else "empty"
                ),
            },
        )
        return sidecar

    def _merge_message_metadata(
        self, existing_metadata: object, incoming_metadata: object
    ) -> dict:
        metadata: dict = {}
        if isinstance(existing_metadata, dict):
            metadata.update(existing_metadata)
        if isinstance(incoming_metadata, dict):
            metadata.update(incoming_metadata)
        return metadata

    def _normalize_message_reference_sidecar(self, message: object) -> object:
        if not isinstance(message, dict):
            return message

        normalized_message = dict(message)
        changed = False

        for key in ("sources", "citations", "references", "documents"):
            if key not in normalized_message:
                continue

            value = normalized_message.get(key)
            if not isinstance(value, (list, dict)):
                continue

            clean_sources = self._merge_message_sources(None, value)
            if clean_sources:
                if clean_sources != value:
                    normalized_message[key] = clean_sources
                    changed = True
            else:
                normalized_message.pop(key, None)
                changed = True

        existing_metadata = normalized_message.get("metadata")
        metadata = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        sidecar = self.build_reference_metadata_sidecar(
            metadata=metadata,
            sources=[
                normalized_message.get("canonical_references"),
                normalized_message.get("sources"),
                normalized_message.get("citations"),
                normalized_message.get("references"),
                normalized_message.get("documents"),
            ],
            diagnostics=normalized_message.get("retrieval_diagnostics"),
            tool_outputs=normalized_message.get("output"),
        )
        if sidecar or metadata:
            merged_metadata = dict(metadata)
            if sidecar.get("canonical_references"):
                merged_metadata["canonical_references"] = sidecar["canonical_references"]
            else:
                merged_metadata.pop("canonical_references", None)
            if sidecar.get("active_source_scope"):
                merged_metadata["active_source_scope"] = sidecar["active_source_scope"]
            else:
                merged_metadata.pop("active_source_scope", None)
            if sidecar.get("retrieval_diagnostics"):
                merged_metadata["retrieval_diagnostics"] = sidecar["retrieval_diagnostics"]
            else:
                merged_metadata.pop("retrieval_diagnostics", None)
            if merged_metadata != existing_metadata:
                normalized_message["metadata"] = merged_metadata
                changed = True

        accepted_references = self.build_canonical_references(
            metadata.get("canonical_references"),
            normalized_message.get("canonical_references"),
        )

        if self._should_clear_reference_aliases(
            metadata=normalized_message.get("metadata"),
            diagnostics=sidecar.get("retrieval_diagnostics"),
            accepted_references=accepted_references,
        ):
            message_metadata = normalized_message.get("metadata")
            if (
                isinstance(message_metadata, dict)
                and "canonical_references" in message_metadata
            ):
                normalized_message["metadata"] = {
                    key: value
                    for key, value in message_metadata.items()
                    if key != "canonical_references"
                }
                changed = True
            for key in (
                "canonical_references",
                "sources",
                "citations",
                "references",
                "documents",
            ):
                if key in normalized_message:
                    normalized_message.pop(key, None)
                    changed = True
            cleaned_content = self._strip_suppressed_reference_citation_markers(
                normalized_message.get("content")
            )
            if cleaned_content != normalized_message.get("content"):
                normalized_message["content"] = cleaned_content
                changed = True
            cleaned_output = (
                self._strip_suppressed_reference_citation_markers_from_output(
                    normalized_message.get("output")
                )
            )
            if cleaned_output != normalized_message.get("output"):
                normalized_message["output"] = cleaned_output
                changed = True

        if self._should_hide_retrieval_status_history(
            message=normalized_message,
            canonical_references=sidecar.get("canonical_references") or [],
            retrieval_diagnostics=sidecar.get("retrieval_diagnostics") or [],
            metadata=normalized_message.get("metadata")
            if isinstance(normalized_message.get("metadata"), dict)
            else {},
        ):
            normalized_message, status_changed = self._hide_retrieval_status_history(
                normalized_message
            )
            changed = changed or status_changed

        if not changed:
            if sidecar:
                observe_llm_event(
                    "reference.message_hydrate",
                    {
                        "role": str(normalized_message.get("role") or ""),
                        "changed": False,
                        "metadata_present": bool(metadata),
                        "canonical_reference_count": len(
                            sidecar.get("canonical_references") or []
                        ),
                        "diagnostic_count": len(
                            sidecar.get("retrieval_diagnostics") or []
                        ),
                    },
                )
            return message

        observe_llm_event(
            "reference.message_hydrate",
            {
                "role": str(normalized_message.get("role") or ""),
                "changed": True,
                "metadata_present": bool(metadata),
                "canonical_reference_count": len(
                    sidecar.get("canonical_references") or []
                ),
                "diagnostic_count": len(sidecar.get("retrieval_diagnostics") or []),
            },
        )

        return normalized_message

    def _source_signature(self, source: object) -> str:
        if not isinstance(source, dict):
            return ""

        if isinstance(source.get("data"), dict):
            source = source["data"]
        elif source.get("type") == "retrieval_reference":
            source = {key: value for key, value in source.items() if key != "type"}

        source_info = source.get("source")
        source_identity: dict[str, object] = {}
        if isinstance(source_info, dict):
            for key in (
                "id",
                "file_id",
                "note_id",
                "knowledge_id",
                "name",
                "url",
                "type",
                "authority",
            ):
                value = source_info.get(key)
                if value not in (None, "", [], {}):
                    source_identity[key] = value
        elif isinstance(source_info, str) and source_info.strip():
            source_identity["name"] = source_info.strip()

        metadata_identity: dict[str, object] = {}
        metadatas = source.get("metadata")
        if isinstance(metadatas, list):
            for metadata in metadatas:
                if not isinstance(metadata, dict):
                    continue
                for key in (
                    "file_id",
                    "note_id",
                    "knowledge_id",
                    "dataset_id",
                    "source",
                    "name",
                    "url",
                    "source_class",
                    "authority",
                ):
                    value = metadata.get(key)
                    if value not in (None, "", [], {}) and key not in metadata_identity:
                        metadata_identity[key] = value

        top_level_identity: dict[str, object] = {}
        for key in ("source_class", "authority", "as_of", "freshness"):
            value = source.get(key)
            if value not in (None, "", [], {}):
                top_level_identity[key] = value

        provenance = source.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}

        normalized_url = _normalize_web_reference_url(
            source_identity.get("url")
            or metadata_identity.get("url")
            or (
                source_identity.get("id")
                if str(source_identity.get("id") or "").strip().startswith(("http://", "https://"))
                else ""
            )
            or (
                metadata_identity.get("source")
                if str(metadata_identity.get("source") or "").strip().startswith(("http://", "https://"))
                else ""
            )
            or (
                provenance.get("source_id")
                if str(provenance.get("source_id") or "").strip().startswith(("http://", "https://"))
                else ""
            )
        )
        normalized_source_class = _normalize_web_source_class(
            source.get("source_class")
            or provenance.get("source_class")
            or metadata_identity.get("source_class")
            or source_identity.get("type"),
            url=normalized_url,
            authority=source.get("authority")
            or provenance.get("authority")
            or metadata_identity.get("authority")
            or source_identity.get("authority"),
        )
        normalized_authority = _normalize_web_authority(
            source.get("authority")
            or provenance.get("authority")
            or metadata_identity.get("authority")
            or source_identity.get("authority"),
            source_class=normalized_source_class,
        )
        normalized_domain = (
            str(provenance.get("domain") or "").strip()
            or str(metadata_identity.get("domain") or "").strip()
            or _web_reference_domain(normalized_url)
        )

        if normalized_url and normalized_source_class in {"official_web", "generic_web"}:
            try:
                return json.dumps(
                    {
                        "kind": "web",
                        "source_class": normalized_source_class,
                        "authority": normalized_authority,
                        "url": normalized_url,
                        "domain": normalized_domain,
                        "chunk_id": provenance.get("chunk_id"),
                        "page": provenance.get("page"),
                        "section": provenance.get("section"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            except (TypeError, ValueError):
                pass

        non_web_source_type = str(
            source_identity.get("type")
            or provenance.get("source_type")
            or source.get("source_class")
            or ""
        ).strip()
        stable_source_id = (
            source_identity.get("id")
            or metadata_identity.get("file_id")
            or metadata_identity.get("note_id")
            or metadata_identity.get("knowledge_id")
            or provenance.get("source_id")
            or provenance.get("file_id")
            or provenance.get("note_id")
            or provenance.get("knowledge_id")
            or provenance.get("document_id")
        )
        if stable_source_id or non_web_source_type:
            try:
                return json.dumps(
                    {
                        "kind": non_web_source_type or "source",
                        "source_id": stable_source_id,
                        "dataset_id": metadata_identity.get("dataset_id")
                        or provenance.get("dataset_id"),
                        "document_id": provenance.get("document_id"),
                        "chunk_id": provenance.get("chunk_id"),
                        "page": provenance.get("page"),
                        "section": provenance.get("section"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            except (TypeError, ValueError):
                pass

        if source_identity or metadata_identity or top_level_identity:
            try:
                return json.dumps(
                    {
                        "source": source_identity,
                        "metadata": metadata_identity,
                        "top_level": top_level_identity,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            except (TypeError, ValueError):
                pass

        try:
            return json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            source_meta = source.get("source") if isinstance(source.get("source"), dict) else {}
            metadata = (
                source.get("metadata")
                if isinstance(source.get("metadata"), list)
                else source.get("metadatas")
                if isinstance(source.get("metadatas"), list)
                else []
            )
            document = (
                source.get("document")
                if isinstance(source.get("document"), list)
                else source.get("documents")
                if isinstance(source.get("documents"), list)
                else []
            )
            return json.dumps(
                {
                    "id": source.get("id"),
                    "source_id": source_meta.get("id"),
                    "source_name": source_meta.get("name"),
                    "source_url": source_meta.get("url"),
                    "metadata": metadata,
                    "document": document,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )

    def _merge_message_sources(
        self, existing_sources: object, incoming_sources: object
    ) -> list:
        merged: list[dict] = []
        seen: set[str] = set()

        for group in (existing_sources, incoming_sources):
            for source in self._iter_source_items(group):
                if not isinstance(source, dict):
                    continue
                if self._is_retrieval_diagnostic_item(source):
                    continue
                if source.get("type") == "code_execution":
                    continue
                signature = self._source_signature(source)
                if not signature or signature in seen:
                    continue
                seen.add(signature)
                merged.append(source)

        return merged

    def _merge_runtime_message_references(
        self, existing_message: object, incoming_message: object
    ) -> object:
        if not isinstance(existing_message, dict) or not isinstance(incoming_message, dict):
            return incoming_message

        merged = incoming_message
        for key in ("sources", "citations", "references", "documents"):
            existing_sources = existing_message.get(key)
            incoming_sources = incoming_message.get(key)
            if not isinstance(existing_sources, (list, dict)) and not isinstance(
                incoming_sources, (list, dict)
            ):
                continue

            merged_sources = self._merge_message_sources(existing_sources, incoming_sources)
            if not merged_sources:
                continue

            if merged is incoming_message:
                merged = dict(incoming_message)
            merged[key] = merged_sources

        existing_metadata = existing_message.get("metadata")
        incoming_metadata = incoming_message.get("metadata")
        if isinstance(existing_metadata, dict) or isinstance(incoming_metadata, dict):
            if merged is incoming_message:
                merged = dict(incoming_message)
            merged["metadata"] = self._merge_message_metadata(
                existing_metadata, incoming_metadata
            )

        return self._normalize_message_reference_sidecar(merged)

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
            return self._normalize_message_reference_sidecar(incoming_message)
        if not isinstance(incoming_message, dict):
            return self._normalize_message_reference_sidecar(existing_message)

        merged_message = {**existing_message, **incoming_message}
        role = incoming_message.get("role", existing_message.get("role"))

        if isinstance(existing_message.get("metadata"), dict) or isinstance(
            incoming_message.get("metadata"), dict
        ):
            merged_message["metadata"] = self._merge_message_metadata(
                existing_message.get("metadata"), incoming_message.get("metadata")
            )

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

        for key in ("sources", "citations", "references", "documents"):
            if isinstance(existing_message.get(key), (list, dict)) or isinstance(
                incoming_message.get(key), (list, dict)
            ):
                merged_sources = self._merge_message_sources(
                    existing_message.get(key), incoming_message.get(key)
                )
                if merged_sources:
                    merged_message[key] = merged_sources

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

        return self._normalize_message_reference_sidecar(merged_message)

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

    def _hydrate_chat_message_sources(
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

        message_sources: dict[str, list[dict]] = {}

        with get_db_context(db) as db:
            try:
                chat_messages = ChatMessages.get_messages_by_chat_id(chat_id, db=db)
                for chat_message in chat_messages:
                    message_id = self._extract_chat_message_id(chat_id, chat_message.id)
                    if not message_id or not isinstance(chat_message.sources, list):
                        continue
                    if not chat_message.sources:
                        continue
                    message_sources.setdefault(message_id, []).extend(chat_message.sources)
            except Exception as e:
                log.warning(
                    "Failed to load chat_message sources for chat %s: %s", chat_id, e
                )

        if not message_sources:
            return chat_payload, False

        hydrated_messages: Optional[dict] = None
        changed = False

        for message_id, supplemental_sources in message_sources.items():
            message = messages.get(message_id)
            if not isinstance(message, dict) or not supplemental_sources:
                continue

            merged_sources = self._merge_message_sources(
                message.get("sources"), supplemental_sources
            )
            hydrated_message = message
            if merged_sources != message.get("sources"):
                hydrated_message = {**message, "sources": merged_sources}

            hydrated_message = self._normalize_message_reference_sidecar(hydrated_message)
            if hydrated_message == message:
                continue

            if hydrated_messages is None:
                hydrated_messages = dict(messages)

            hydrated_messages[message_id] = hydrated_message
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

    def _preserve_runtime_message_references(
        self, existing_chat_payload: object, incoming_chat_payload: object
    ) -> object:
        if not isinstance(existing_chat_payload, dict) or not isinstance(
            incoming_chat_payload, dict
        ):
            return incoming_chat_payload

        existing_history = existing_chat_payload.get("history")
        incoming_history = incoming_chat_payload.get("history")
        if not isinstance(existing_history, dict) or not isinstance(incoming_history, dict):
            return incoming_chat_payload

        existing_messages = existing_history.get("messages")
        incoming_messages = incoming_history.get("messages")
        if not isinstance(existing_messages, dict) or not isinstance(incoming_messages, dict):
            return incoming_chat_payload

        merged_messages = incoming_messages
        changed = False

        for message_id, incoming_message in incoming_messages.items():
            existing_message = existing_messages.get(message_id)
            merged_message = self._merge_runtime_message_references(
                existing_message, incoming_message
            )
            if merged_message is incoming_message:
                continue
            if merged_messages is incoming_messages:
                merged_messages = dict(incoming_messages)
            merged_messages[message_id] = merged_message
            changed = True

        if not changed:
            return incoming_chat_payload

        return {
            **incoming_chat_payload,
            "history": {
                **incoming_history,
                "messages": merged_messages,
            },
        }

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

        hydrated_chat, hydrated_changed = self._hydrate_chat_message_sources(
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
                existing_chat_payload, _ = self._hydrate_chat_message_sources(
                    chat_item.chat, id, db=db
                )
                chat = self._preserve_runtime_message_references(
                    existing_chat_payload, chat
                )
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

        message = {**message, "id": message_id}

        user_id = chat.user_id
        chat = chat.chat
        history = chat.get("history", {})

        if message_id in history.get("messages", {}):
            history["messages"][message_id] = self._merge_message_payload(
                history["messages"][message_id],
                message,
            )
        else:
            history["messages"][message_id] = self._normalize_message_reference_sidecar(
                message
            )

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

                history["messages"][message_id] = {
                    **history["messages"][message_id],
                    "id": message_id,
                    "role": "assistant",
                    "files": message_files,
                }
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
                    "id": message_id,
                    "role": "assistant",
                    "content": "",
                    "files": message_files,
                    "done": False,
                    "timestamp": int(time.time()),
                }

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

    def get_orphan_message_files_by_chat_id(
        self,
        chat_id: str,
        before_timestamp: Optional[int] = None,
        db: Optional[Session] = None,
    ) -> list[dict]:
        with get_db_context(db) as db:
            chat_item = db.get(Chat, chat_id)
            if chat_item is None or not isinstance(chat_item.chat, dict):
                return []

            known_message_ids = set(
                (
                    chat_item.chat.get("history", {}) or {}
                ).get("messages", {}).keys()
            )

            query = (
                db.query(ChatFile)
                .filter_by(chat_id=chat_id)
                .filter(ChatFile.message_id.isnot(None))
                .order_by(ChatFile.created_at.asc())
            )
            if before_timestamp is not None:
                query = query.filter(ChatFile.created_at <= before_timestamp)

            orphan_rows = [
                row
                for row in query.all()
                if str(row.message_id or "").strip()
                and str(row.message_id or "").strip() not in known_message_ids
            ]
            if not orphan_rows:
                return []

            file_ids = list(
                {
                    str(row.file_id or "").strip()
                    for row in orphan_rows
                    if str(row.file_id or "").strip()
                }
            )
            if not file_ids:
                return []

            file_models_by_id = {
                file_model.id: file_model
                for file_model in Files.get_files_by_ids(file_ids, db=db)
            }

            orphan_files: list[dict] = []
            for row in orphan_rows:
                file_model = file_models_by_id.get(str(row.file_id or "").strip())
                if file_model is None:
                    continue
                orphan_files.append(self._message_file_from_file_model(file_model))

            return self._merge_message_files([], orphan_files, "user")

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
