import copy
import time
import logging
import sys
import os
import base64
import textwrap
import io
import mimetypes

import asyncio
from aiocache import cached
from typing import Any, Optional
import random
import json
import html
import inspect
import re
import ast
from urllib.parse import urlparse

from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import requests

from fastapi import Request, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from starlette.responses import Response, StreamingResponse, JSONResponse


from open_webui.utils.misc import is_string_allowed
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.chats import Chats
from open_webui.models.folders import Folders
from open_webui.models.knowledge import Knowledges
from open_webui.models.users import Users
from open_webui.socket.main import (
    get_event_call,
    get_event_emitter,
)
from open_webui.routers.tasks import (
    build_fallback_chat_title,
    generate_queries,
    generate_title,
    generate_follow_ups,
    generate_image_prompt,
    generate_chat_tags,
    resolve_generated_chat_title,
)
from open_webui.routers.retrieval import (
    process_web_search,
    SearchForm,
    ProcessFileForm,
    ensure_retrieval_runtime,
    process_file,
)
from open_webui.internal.db import SessionLocal
from open_webui.models.files import Files, File
from open_webui.utils.tools import get_builtin_tools
from open_webui.routers.images import (
    image_generations,
    CreateImageForm,
    image_edits,
    EditImageForm,
)
from open_webui.routers.pipelines import (
    process_pipeline_inlet_filter,
    process_pipeline_outlet_filter,
)
from open_webui.routers.memories import query_memory, QueryMemoryForm

from open_webui.utils.webhook import post_webhook
from open_webui.utils.files import (
    convert_markdown_base64_images,
    get_file_url_from_base64,
    get_image_base64_from_url,
    get_image_url_from_base64,
)
from open_webui.routers.files import upload_file_handler


from open_webui.models.users import UserModel
from open_webui.models.functions import Functions
from open_webui.models.models import Models

from open_webui.retrieval.engine_contract import (
    _retrieval_engine_authority_state,
    _retrieval_engine_bypass_reason,
    _retrieval_engine_observe_safe_mapping,
    _retrieval_engine_observe_safe_value,
    _retrieval_engine_result_first_pass_contract,
    _retrieval_engine_runtime_mode,
)
from open_webui.retrieval.utils import get_sources_from_items


from open_webui.utils.sanitize import sanitize_code
from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.telemetry.llm_observability import (
    diagnostic_classification_counts,
    diagnostic_reason_codes,
    observe_llm_event,
    summarize_source_scope,
)
from open_webui.utils.task import (
    build_follow_up_context_guidance,
    build_session_user_memory_prompt,
    extract_session_user_facts,
    get_task_model_id,
    normalize_session_user_facts,
    rag_template,
    tools_function_calling_generation_template,
)
from open_webui.utils.misc import (
    deep_update,
    extract_urls,
    get_message_list,
    add_or_update_system_message,
    add_or_update_user_message,
    set_last_user_message_content,
    get_last_user_message,
    get_last_user_message_item,
    get_last_assistant_message,
    get_system_message,
    replace_system_message_content,
    prepend_to_first_user_message_content,
    convert_logit_bias_input_to_json,
    get_content_from_message,
    convert_output_to_messages,
)
from open_webui.utils.tools import (
    DEEPAGENT_RUNTIME_SKILL_IDS_METADATA_KEY,
    build_deepagent_runtime_tool_snapshot,
    get_tools,
    get_updated_tool_function,
    get_terminal_tools,
)
from open_webui.utils.access_control import get_permissions, has_connection_access
from open_webui.utils.knowflow import (
    get_knowflow_asset_ref_key,
    is_knowflow_image_ref,
    list_knowledge_documents,
    resolve_knowflow_asset_url,
    resolve_knowflow_html_content,
)
from open_webui.utils.plugin import load_function_module_by_id
from open_webui.utils.skill_import import PHASE_NOTES as DOCUMENT_SKILL_PHASE_NOTES
from open_webui.utils.filter import (
    get_sorted_filter_ids,
    process_filter_functions,
)
from open_webui.utils.catalog import (
    filter_hidden_skill_ids,
    filter_hidden_tool_ids,
    filter_visible_skills,
    is_catalog_runtime_activatable,
)
from open_webui.utils.code_interpreter import execute_code_jupyter
from open_webui.utils.payload import apply_system_prompt_to_body
from open_webui.utils.response import normalize_usage
from open_webui.utils.mcp.client import MCPClient


from open_webui.config import (
    CACHE_DIR,
    DEFAULT_VOICE_MODE_PROMPT_TEMPLATE,
    DEFAULT_TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE,
    DEFAULT_CODE_INTERPRETER_PROMPT,
    CODE_INTERPRETER_PYODIDE_PROMPT,
    CODE_INTERPRETER_BLOCKED_MODULES,
)
from open_webui.env import (
    GLOBAL_LOG_LEVEL,
    ENABLE_CHAT_RESPONSE_BASE64_IMAGE_URL_CONVERSION,
    CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE,
    CHAT_RESPONSE_MAX_TOOL_CALL_RETRIES,
    BYPASS_MODEL_ACCESS_CONTROL,
    ENABLE_REALTIME_CHAT_SAVE,
    ENABLE_QUERIES_CACHE,
    RAG_SYSTEM_CONTEXT,
    ENABLE_FORWARD_USER_INFO_HEADERS,
    FORWARD_SESSION_INFO_HEADER_CHAT_ID,
    FORWARD_SESSION_INFO_HEADER_MESSAGE_ID,
)
from open_webui.utils.headers import include_user_info_headers
from open_webui.constants import TASKS


logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)

INLINE_FILE_SOURCE_MAX_CHARS = 12000


DEFAULT_REASONING_TAGS = [
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<reason>", "</reason>"),
    ("<reasoning>", "</reasoning>"),
    ("<thought>", "</thought>"),
    ("<Thought>", "</Thought>"),
    ("<|begin_of_thought|>", "<|end_of_thought|>"),
    ("◁think▷", "◁/think▷"),
]
DEFAULT_SOLUTION_TAGS = [("<|begin_of_solution|>", "<|end_of_solution|>")]
DEFAULT_CODE_INTERPRETER_TAGS = [("<code_interpreter>", "</code_interpreter>")]


def _iter_balanced_json_candidates(text: str):
    if not text:
        return

    length = len(text)
    for start in range(length):
        if text[start] not in "{[":
            continue

        stack = []
        in_string = False
        escaped = False

        for idx in range(start, length):
            ch = text[idx]

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue

            if ch in "{[":
                stack.append(ch)
                continue

            if ch in "}]":
                if not stack:
                    break
                opener = stack.pop()
                if (opener == "{" and ch != "}") or (opener == "[" and ch != "]"):
                    break
                if not stack:
                    yield text[start : idx + 1]
                    break


def _extract_json_from_task_response(text: str):
    if not isinstance(text, str):
        return None

    normalized = text.strip()
    if not normalized:
        return None

    candidates: list[str] = [normalized]
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", normalized, re.I):
        fenced = (match.group(1) or "").strip()
        if fenced:
            candidates.append(fenced)

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except Exception:
            pass

    for candidate in candidates:
        for snippet in _iter_balanced_json_candidates(candidate):
            try:
                return json.loads(snippet)
            except Exception:
                continue

    return None


def _message_has_meaningful_content(message: dict) -> bool:
    if not isinstance(message, dict):
        return False
    content = message.get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return True
    return False


def _extract_follow_up_focus_messages(messages: list[dict]) -> list[dict]:
    if not isinstance(messages, list) or not messages:
        return []

    def _role_at(index: int) -> str:
        role = messages[index].get("role", "")
        return str(role or "").strip().lower()

    last_assistant_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        role = _role_at(idx)
        if role == "assistant" and _message_has_meaningful_content(messages[idx]):
            last_assistant_idx = idx
            break

    user_search_end = last_assistant_idx if last_assistant_idx >= 0 else len(messages) - 1
    last_user_idx = -1
    for idx in range(user_search_end, -1, -1):
        role = _role_at(idx)
        if role in {"user", "human"} and _message_has_meaningful_content(messages[idx]):
            last_user_idx = idx
            break

    focused: list[dict] = []
    if last_user_idx >= 0:
        focused.append(messages[last_user_idx])
    if last_assistant_idx >= 0 and last_assistant_idx >= last_user_idx:
        focused.append(messages[last_assistant_idx])

    if focused:
        return focused

    fallback: list[dict] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).strip().lower()
        if role not in {"user", "human", "assistant"}:
            continue
        if not _message_has_meaningful_content(msg):
            continue
        fallback.append(msg)
        if len(fallback) >= 4:
            break
    fallback.reverse()
    return fallback


def output_id(prefix: str) -> str:
    """Generate OR-style ID: prefix + 24-char hex UUID."""
    return f"{prefix}_{uuid4().hex[:24]}"


def _split_tool_calls(
    tool_calls: list[dict],
) -> list[dict]:
    """Expand tool calls whose arguments contain multiple back-to-back JSON objects.

    Some models (e.g. GPT-5.4) send multiple complete JSON argument objects
    under the same tool call index, producing concatenated invalid JSON like:
        '{"query":"A","count":5}{"query":"B","count":5}'

    Each such tool call is split into separate entries so each gets executed
    independently. Single-object arguments pass through unchanged.
    """

    def split_json_objects(raw: str) -> list[str]:
        decoder = json.JSONDecoder()
        results = []
        position = 0

        while position < len(raw):
            while position < len(raw) and raw[position].isspace():
                position += 1
            if position >= len(raw):
                break
            try:
                _, end = decoder.raw_decode(raw, position)
                results.append(raw[position:end].strip())
                position = end
            except json.JSONDecodeError:
                return [raw]

        return results or [raw]

    expanded = []
    for tool_call in tool_calls:
        arguments = tool_call.get("function", {}).get("arguments", "")
        split_arguments = split_json_objects(arguments)

        if len(split_arguments) <= 1:
            expanded.append(tool_call)
        else:
            for argument in split_arguments:
                cloned = copy.deepcopy(tool_call)
                cloned["id"] = f"call_{uuid4().hex[:24]}"
                cloned["function"]["arguments"] = argument
                expanded.append(cloned)

    return expanded


def get_citation_source_from_tool_result(
    tool_name: str, tool_params: dict, tool_result: str, tool_id: str = ""
) -> list[dict]:
    """
    Parse a tool's result and convert it to source dicts for citation display.

    Follows the source format conventions from get_sources_from_items:
    - source: file/item info object with id, name, type
    - document: list of document contents
    - metadata: list of metadata objects with source, file_id, name fields

    Returns a list of sources (usually one, but query_knowledge_files may return multiple).
    """
    _EXPECTS_LIST = {"search_web", "query_knowledge_files"}
    _EXPECTS_DICT = {
        "view_knowledge_file",
        "query_selected_knowledge_files",
        "read_selected_file",
    }

    try:
        try:
            tool_result = json.loads(tool_result)
        except (json.JSONDecodeError, TypeError):
            pass  # keep tool_result as-is (e.g. fetch_url returns plain text)
        if isinstance(tool_result, dict) and "error" in tool_result:
            return []

        # Validate tool_result type based on what the branch expects
        if tool_name in _EXPECTS_LIST and not isinstance(tool_result, list):
            return []
        elif tool_name in _EXPECTS_DICT and not isinstance(tool_result, dict):
            return []

        if tool_name == "search_web":
            # Parse JSON array: [{"title": "...", "link": "...", "snippet": "..."}]
            results = tool_result
            documents = []
            metadata = []

            for result in results:
                title = result.get("title", "")
                link = result.get("link", "")
                snippet = result.get("snippet", "")

                documents.append(f"{title}\n{snippet}")
                metadata.append(
                    {
                        "source": link,
                        "name": title,
                        "url": link,
                    }
                )

            return [
                {
                    "source": {"name": "search_web", "id": "search_web"},
                    "document": documents,
                    "metadata": metadata,
                }
            ]

        elif tool_name == "view_knowledge_file":
            file_data = tool_result
            filename = file_data.get("filename", "Unknown File")
            file_id = file_data.get("id", "")
            knowledge_name = file_data.get("knowledge_name", "")

            return [
                {
                    "source": {
                        "id": file_id,
                        "name": filename,
                        "type": "file",
                    },
                    "document": [file_data.get("content", "")],
                    "metadata": [
                        {
                            "file_id": file_id,
                            "name": filename,
                            "source": filename,
                            **(
                                {"knowledge_name": knowledge_name}
                                if knowledge_name
                                else {}
                            ),
                        }
                    ],
                }
            ]

        elif tool_name == "fetch_url":
            url = tool_params.get("url", "")
            content = tool_result if isinstance(tool_result, str) else str(tool_result)
            snippet = content[:500] + ("..." if len(content) > 500 else "")

            return [
                {
                    "source": {"name": url or "fetch_url", "id": url or "fetch_url"},
                    "document": [snippet],
                    "metadata": [
                        {
                            "source": url,
                            "name": url,
                            "url": url,
                        }
                    ],
                }
            ]

        elif tool_name in {
            "query_selected_knowledge_files",
            "read_selected_file",
        }:
            sidecar = Chats.build_reference_metadata_sidecar(metadata=tool_result)
            return sidecar.get("canonical_references", [])

        elif tool_name == "query_knowledge_files":
            chunks = tool_result

            # Group chunks by source for better citation display
            # Each unique source becomes a separate source entry
            sources_by_file = {}

            for chunk in chunks:
                source_name = chunk.get("source", "Unknown")
                file_id = chunk.get("file_id", "")
                note_id = chunk.get("note_id", "")
                chunk_type = chunk.get("type", "file")
                content = chunk.get("content", "")

                # Use file_id or note_id as the key
                key = file_id or note_id or source_name

                if key not in sources_by_file:
                    sources_by_file[key] = {
                        "source": {
                            "id": file_id or note_id,
                            "name": source_name,
                            "type": chunk_type,
                        },
                        "document": [],
                        "metadata": [],
                        "distances": [],
                    }

                sources_by_file[key]["document"].append(content)
                metadata = {
                    "file_id": file_id,
                    "name": source_name,
                    "source": source_name,
                    "title": source_name,
                    "file_name": source_name,
                    **({"note_id": note_id} if note_id else {}),
                }

                for field in (
                    "url",
                    "embed_url",
                    "image_id",
                    "chunk_type",
                    "render_markdown",
                    "page",
                    "title",
                    "file_name",
                    "filename",
                    "document_name",
                    "provider",
                    "external_provider",
                ):
                    value = chunk.get(field)
                    if value not in (None, "", "null", "undefined"):
                        metadata[field] = value

                html_content = chunk.get("html_content")
                if isinstance(html_content, str) and html_content.strip():
                    metadata["html"] = True
                    metadata["html_content"] = html_content.strip()
                elif chunk.get("html") is True:
                    metadata["html"] = True

                sources_by_file[key]["metadata"].append(metadata)

                distance = chunk.get("distance")
                sources_by_file[key]["distances"].append(distance)

            # Return all grouped sources as a list
            if sources_by_file:
                return list(sources_by_file.values())

            # Empty result fallback
            return []

        else:
            # Fallback for other tools
            return [
                {
                    "source": {
                        "name": tool_name,
                        "type": "tool",
                        "id": tool_id or tool_name,
                    },
                    "document": [str(tool_result)],
                    "metadata": [{"source": tool_name, "name": tool_name}],
                }
            ]
    except Exception as e:
        log.exception(f"Error parsing tool result for {tool_name}: {e}")
        return [
            {
                "source": {"name": tool_name, "type": "tool"},
                "document": [str(tool_result)],
                "metadata": [{"source": tool_name}],
            }
        ]


def split_content_and_whitespace(content):
    content_stripped = content.rstrip()
    original_whitespace = (
        content[len(content_stripped) :] if len(content) > len(content_stripped) else ""
    )
    return content_stripped, original_whitespace


def is_opening_code_block(content):
    backtick_segments = content.split("```")
    # Even number of segments means the last backticks are opening a new block
    return len(backtick_segments) > 1 and len(backtick_segments) % 2 == 0


TOOL_RESULT_PREVIEW_LIMIT = 900
TOOL_RESULT_ERROR_LIMIT = 600
TOOL_RESULT_WEBPAGE_LIMIT = 280
TOOL_RESULT_SEARCH_ITEMS_LIMIT = 5
TOOL_RESULT_BASE64ISH_CHUNK_RE = re.compile(r"[A-Za-z0-9+/=_-]{160,}")


def _collapse_preview_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _truncate_preview_text(value: str, limit: int) -> str:
    text = _collapse_preview_whitespace(value)
    if len(text) <= limit:
        return text
    return f"{text[: max(limit - 1, 0)].rstrip()}…"


def _extract_tool_result_message(parsed_result: Any) -> str:
    if not isinstance(parsed_result, dict):
        return ""

    for key in ("message", "detail", "error", "content"):
        value = parsed_result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _infer_tool_result_status(result_text: str, parsed_result: Any) -> str:
    if isinstance(parsed_result, dict):
        status = str(parsed_result.get("status", "") or "").strip().lower()
        if status in {"error", "failed"}:
            return "error"
        if status == "timeout":
            return "timeout"
        if parsed_result.get("success") is False or parsed_result.get("ok") is False:
            return "error"

        error_text = _extract_tool_result_message(parsed_result)
        if error_text:
            lowered_error = error_text.lower()
            if "timeout" in lowered_error:
                return "timeout"
            if any(
                marker in lowered_error
                for marker in (
                    "error",
                    "exception",
                    "traceback",
                    "failed",
                    "connection refused",
                )
            ):
                return "error"

    lowered = str(result_text or "").strip().lower()
    if not lowered:
        return "success"
    if "timeout" in lowered:
        return "timeout"
    if lowered.startswith("error"):
        return "error"
    if any(
        marker in lowered
        for marker in (
            "error fetching",
            "exception",
            "traceback",
            "failed to ",
            "connection refused",
        )
    ):
        return "error"
    return "success"


def _build_tool_result_preview(
    tool_name: str,
    result_text: str,
    parsed_result: Any,
    result_status: str,
) -> str:
    message = _extract_tool_result_message(parsed_result)
    if result_status in {"error", "timeout"}:
        return _truncate_preview_text(message or result_text, TOOL_RESULT_ERROR_LIMIT)

    normalized_tool_name = str(tool_name or "").strip().lower()

    if normalized_tool_name in {"internet_search", "search_web"} and isinstance(
        parsed_result, dict
    ):
        results = parsed_result.get("results")
        if isinstance(results, list):
            preview: dict[str, Any] = {
                "query": str(parsed_result.get("query", "") or "").strip(),
                "count": len(results),
            }
            items = []
            for result in results[:TOOL_RESULT_SEARCH_ITEMS_LIMIT]:
                if not isinstance(result, dict):
                    continue
                title = str(result.get("title", "") or result.get("url", "")).strip()
                url = str(result.get("url", "") or "").strip()
                if title and url:
                    items.append({"title": title, "url": url})
                elif title:
                    items.append({"title": title})
            if items:
                preview["items"] = items
            return json.dumps(preview, ensure_ascii=False)

    if normalized_tool_name in {"visit_webpage", "fetch_url"}:
        if isinstance(parsed_result, dict):
            url = str(parsed_result.get("url", "") or "").strip()
            snippet = _extract_tool_result_message(parsed_result)
            if snippet and not TOOL_RESULT_BASE64ISH_CHUNK_RE.search(snippet):
                preview = _truncate_preview_text(snippet, TOOL_RESULT_WEBPAGE_LIMIT)
                if url:
                    return json.dumps(
                        {"url": url, "snippet": preview}, ensure_ascii=False
                    )
                return preview
            if url:
                return json.dumps(
                    {"url": url, "message": "网页内容已读取并用于后续回答。"},
                    ensure_ascii=False,
                )
            return "网页内容已读取并用于后续回答。"

        collapsed = _collapse_preview_whitespace(result_text)
        if collapsed and not TOOL_RESULT_BASE64ISH_CHUNK_RE.search(collapsed):
            return _truncate_preview_text(collapsed, TOOL_RESULT_WEBPAGE_LIMIT)
        return "网页内容已读取并用于后续回答。"

    if isinstance(parsed_result, (dict, list)):
        serialized = json.dumps(parsed_result, ensure_ascii=False)
        return _truncate_preview_text(serialized, TOOL_RESULT_PREVIEW_LIMIT)

    return _truncate_preview_text(result_text, TOOL_RESULT_PREVIEW_LIMIT)


def _tool_call_summary_label(result_status: str) -> str:
    if result_status == "error":
        return "Tool Failed"
    if result_status == "timeout":
        return "Tool Timed Out"
    return "Tool Executed"


def serialize_output(output: list) -> str:
    """
    Convert OR-aligned output items to HTML for display.
    For LLM consumption, use convert_output_to_messages() instead.
    """
    content = ""

    # First pass: collect function_call_output items by call_id for lookup
    tool_outputs = {}
    for item in output:
        if item.get("type") == "function_call_output":
            tool_outputs[item.get("call_id")] = item

    # Second pass: render items in order
    for idx, item in enumerate(output):
        item_type = item.get("type", "")

        if item_type == "message":
            for content_part in item.get("content", []):
                if "text" in content_part:
                    text = content_part.get("text", "").strip()
                    if text:
                        content = f"{content}{text}\n"

        elif item_type == "function_call":
            # Render tool call inline with its result (if available)
            if content and not content.endswith("\n"):
                content += "\n"

            call_id = item.get("call_id", "")
            name = item.get("name", "")
            arguments = item.get("arguments", "")

            result_item = tool_outputs.get(call_id)
            if result_item:
                result_text = ""
                for result_output in result_item.get("output", []):
                    if "text" in result_output:
                        output_text = result_output.get("text", "")
                        result_text += (
                            str(output_text)
                            if not isinstance(output_text, str)
                            else output_text
                        )
                parsed_result = _parse_nested_json_value(result_text)
                result_status = _infer_tool_result_status(result_text, parsed_result)
                preview_result = _build_tool_result_preview(
                    name,
                    result_text,
                    parsed_result,
                    result_status,
                )
                files = result_item.get("files")
                embeds = result_item.get("embeds", "")

                content += f'<details type="tool_calls" done="true" status="{result_status}" id="{call_id}" name="{name}" arguments="{html.escape(json.dumps(arguments))}" result="{html.escape(json.dumps(preview_result, ensure_ascii=False))}" files="{html.escape(json.dumps(files)) if files else ""}" embeds="{html.escape(json.dumps(embeds))}">\n<summary>{_tool_call_summary_label(result_status)}</summary>\n</details>\n'
            else:
                content += f'<details type="tool_calls" done="false" status="running" id="{call_id}" name="{name}" arguments="{html.escape(json.dumps(arguments))}">\n<summary>Executing...</summary>\n</details>\n'

        elif item_type == "function_call_output":
            # Already handled inline with function_call above
            pass

        elif item_type == "reasoning":
            reasoning_content = ""
            # Check for 'summary' (new structure) or 'content' (legacy/fallback)
            source_list = item.get("summary", []) or item.get("content", [])
            for content_part in source_list:
                if "text" in content_part:
                    reasoning_content += content_part.get("text", "")
                elif "summary" in content_part:  # Handle potential nested logic if any
                    pass

            reasoning_content = reasoning_content.strip()

            duration = item.get("duration")
            status = item.get("status", "in_progress")

            # Infer completion: if this reasoning item is NOT the last item,
            # render as done (a subsequent item means reasoning is complete)
            is_last_item = idx == len(output) - 1

            if content and not content.endswith("\n"):
                content += "\n"

            display = html.escape(
                "\n".join(
                    (f"> {line}" if not line.startswith(">") else line)
                    for line in reasoning_content.splitlines()
                )
            )

            if status == "completed" or duration is not None or not is_last_item:
                content = f'{content}<details type="reasoning" done="true" duration="{duration or 0}">\n<summary>Thought for {duration or 0} seconds</summary>\n{display}\n</details>\n'
            else:
                content = f'{content}<details type="reasoning" done="false">\n<summary>Thinking…</summary>\n{display}\n</details>\n'

        elif item_type == "open_webui:code_interpreter":
            content_stripped, original_whitespace = split_content_and_whitespace(
                content
            )
            if is_opening_code_block(content_stripped):
                content = content_stripped.rstrip("`").rstrip() + original_whitespace
            else:
                content = content_stripped + original_whitespace

            if content and not content.endswith("\n"):
                content += "\n"

            # Render the code_interpreter item as a <details> block
            # so the frontend Collapsible renders "Analyzing..."/"Analyzed".
            code = item.get("code", "").strip()
            lang = item.get("lang", "python")
            status = item.get("status", "in_progress")
            duration = item.get("duration")
            is_last_item = idx == len(output) - 1

            # Build inner content: code block
            display = ""
            if code:
                display = f"```{lang}\n{code}\n```"

            # Build output attribute as HTML-escaped JSON for CodeBlock.svelte
            ci_output = item.get("output")
            output_attr = ""
            if ci_output:
                if isinstance(ci_output, dict):
                    output_json = json.dumps(ci_output, ensure_ascii=False)
                else:
                    output_json = json.dumps(
                        {"result": str(ci_output)}, ensure_ascii=False
                    )
                output_attr = f' output="{html.escape(output_json)}"'

            if status == "completed" or duration is not None or not is_last_item:
                content += f'<details type="code_interpreter" done="true" duration="{duration or 0}"{output_attr}>\n<summary>Analyzed</summary>\n{display}\n</details>\n'
            else:
                content += f'<details type="code_interpreter" done="false"{output_attr}>\n<summary>Analyzing…</summary>\n{display}\n</details>\n'

    return content.strip()


def _extract_text_from_output_parts(parts: Any) -> str:
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


_LEAKED_VISION_SPECIALIST_KEYS = {"observations", "uncertainties", "summary"}


def _extract_leading_json_block(text: str) -> tuple[Any, int]:
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


def _looks_like_leaked_vision_specialist_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    keys = {str(key).strip() for key in payload.keys()}
    if not keys or not keys.issubset(_LEAKED_VISION_SPECIALIST_KEYS):
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


def _strip_leaked_vision_specialist_prefix(text: Any) -> Any:
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
        trimmed.startswith(prefix) for prefix in _HISTORICAL_TOOL_REPLAY_NOTE_LINE_PREFIXES
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


def _strip_historical_tool_replay_note_text(text: Any) -> Any:
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


def _sanitize_assistant_text_content(text: Any) -> Any:
    sanitized = _strip_leaked_vision_specialist_prefix(text)
    return _strip_historical_tool_replay_note_text(sanitized)


def _sanitize_assistant_message_content(content: Any) -> Any:
    if isinstance(content, list):
        updated_content = []
        changed = False
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") in {"text", "input_text", "output_text"}
            ):
                original_text = part.get("text")
                sanitized_text = _sanitize_assistant_text_content(original_text)
                if sanitized_text != original_text:
                    updated_content.append({**part, "text": sanitized_text})
                    changed = True
                    continue
            updated_content.append(part)
        return updated_content if changed else content

    if isinstance(content, str):
        return _sanitize_assistant_text_content(content)

    return content


def _normalize_output_for_chat(output: list) -> list:
    if not isinstance(output, list):
        return []

    normalized_output = copy.deepcopy(output)
    terminal_status_by_call_id: dict[str, str] = {}

    for item in normalized_output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call_output":
            continue

        call_id = str(item.get("call_id") or "").strip()
        output_text = _extract_text_from_output_parts(item.get("output"))
        parsed_output = _parse_nested_json_value(output_text)
        inferred_status = _infer_tool_result_status(output_text, parsed_output)
        output_status = str(item.get("status") or "").strip().lower()

        if output_status not in {"success", "completed", "error", "timeout"}:
            if inferred_status in {"success", "error", "timeout"}:
                item["status"] = inferred_status
                output_status = inferred_status

        terminal_status = None
        if output_status in {"success", "completed"}:
            terminal_status = "completed"
        elif output_status in {"error", "timeout"}:
            terminal_status = output_status
        elif inferred_status in {"success", "error", "timeout"}:
            terminal_status = "completed" if inferred_status == "success" else inferred_status

        if call_id and terminal_status:
            terminal_status_by_call_id[call_id] = terminal_status

    for item in normalized_output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call":
            continue

        call_id = str(item.get("call_id") or item.get("id") or "").strip()
        terminal_status = terminal_status_by_call_id.get(call_id)
        if not terminal_status:
            continue

        current_status = str(item.get("status") or "").strip().lower()
        if current_status in {"completed", "error", "timeout"}:
            continue

        item["status"] = terminal_status

    for item in normalized_output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue

        content = item.get("content")
        sanitized_content = _sanitize_assistant_message_content(content)
        if sanitized_content != content:
            item["content"] = sanitized_content

    return normalized_output


def _serialize_output_for_chat_content(
    output: list,
    *,
    fallback_content: str = "",
) -> str:
    message_blocks: list[str] = []
    has_structured_items = False
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            has_structured_items = True
            continue

        message_text = _extract_text_from_output_parts(item.get("content"))
        message_text = _strip_bridge_details_blocks(message_text).strip()
        message_text = _sanitize_assistant_text_content(message_text).strip()
        if message_text:
            message_blocks.append(message_text)

    content = "\n".join(message_blocks).strip()
    if content:
        return content

    # When the response already carries structured tool/process items but no
    # assistant message item, raw fallback text is usually transient pre-tool
    # scaffolding rather than the real final answer.
    if has_structured_items:
        return ""

    if isinstance(fallback_content, str):
        content = _strip_bridge_details_blocks(fallback_content).strip()

    return content


def _build_chat_completion_payload(
    output: list,
    *,
    fallback_content: str = "",
) -> tuple[list, dict]:
    normalized_output = _normalize_output_for_chat(output)
    return normalized_output, {
        "content": _serialize_output_for_chat_content(
            normalized_output,
            fallback_content=fallback_content,
        ),
        "output": normalized_output,
    }


def _fallback_answer_for_completed_tool_only_output(output: object) -> str:
    if not isinstance(output, list):
        return ""

    saw_web_search = False
    saw_time = False
    current_date = ""
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            name = str(item.get("name") or "").strip()
            saw_web_search = saw_web_search or name in {"internet_search", "search_web"}
            saw_time = saw_time or name == "current_server_time"
            continue
        if item.get("type") != "function_call_output":
            continue
        parsed = _extract_function_call_output_payload(item)
        if not isinstance(parsed, dict):
            continue
        if not current_date and isinstance(parsed.get("date"), str):
            current_date = parsed["date"]
        if isinstance(parsed.get("results"), list):
            saw_web_search = True

    if saw_web_search:
        prefix = f"今天是 {current_date}。" if current_date else ""
        return (
            f"{prefix}已完成当前网络检索，但本轮未能形成进一步的可靠综合结论；"
            "请参考引用来源，或指定要核验的具体来源继续深入。"
        )
    if saw_time and current_date:
        return f"今天是 {current_date}。"
    return ""


def _merge_reference_sidecar_into_metadata(
    metadata: dict,
    *,
    sources: Any = None,
    diagnostics: Any = None,
    tool_outputs: Any = None,
    legacy_tool_sources: bool = False,
) -> dict:
    if not isinstance(metadata, dict):
        return {}

    tool_sidecar = Chats.build_reference_metadata_sidecar(tool_outputs=tool_outputs)
    if legacy_tool_sources and tool_sidecar.get("canonical_references"):
        metadata["sources"] = Chats._merge_message_sources(
            tool_sidecar["canonical_references"],
            metadata.get("sources"),
        )

    sidecar = Chats.build_reference_metadata_sidecar(
        metadata=metadata,
        sources=sources,
        diagnostics=diagnostics,
        tool_outputs=tool_outputs,
    )
    if sidecar.get("canonical_references"):
        metadata["canonical_references"] = sidecar["canonical_references"]
    else:
        metadata.pop("canonical_references", None)
    if sidecar.get("reference_cards"):
        metadata["reference_cards"] = sidecar["reference_cards"]
    else:
        metadata.pop("reference_cards", None)
    if sidecar.get("active_source_scope"):
        metadata["active_source_scope"] = sidecar["active_source_scope"]
    elif metadata.get("active_source_scope"):
        normalized_scope = Chats.normalize_active_source_scope(
            metadata.get("active_source_scope")
        )
        if normalized_scope:
            metadata["active_source_scope"] = normalized_scope
        else:
            metadata.pop("active_source_scope", None)
    if sidecar.get("retrieval_diagnostics"):
        metadata["retrieval_diagnostics"] = sidecar["retrieval_diagnostics"]
    elif sidecar.get("canonical_references"):
        metadata.pop("retrieval_diagnostics", None)
    return sidecar


def _completion_sources_for_persistence(metadata: dict) -> list[dict]:
    if not isinstance(metadata, dict):
        return []

    references = metadata.get("canonical_references")
    if not isinstance(references, list):
        return []

    return [item for item in references if isinstance(item, dict)]


def _is_persistable_chat_target(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False

    chat_id = metadata.get("chat_id")
    message_id = metadata.get("message_id")
    if not isinstance(chat_id, str) or not chat_id or chat_id.startswith("local:"):
        return False
    if not isinstance(message_id, str) or not message_id:
        return False
    return True


def _persist_assistant_completion_message(
    *,
    metadata: dict,
    content: str,
    output: object,
    sources: object = None,
    usage: object = None,
    completion_metadata: object = None,
) -> bool:
    if not _is_persistable_chat_target(metadata):
        return False

    message_payload: dict[str, Any] = {
        "role": "assistant",
        "content": str(content or ""),
        "output": output if isinstance(output, list) else [],
    }
    if isinstance(sources, list) and sources:
        message_payload["sources"] = sources
    if isinstance(usage, dict) and usage:
        message_payload["usage"] = usage
    if isinstance(completion_metadata, dict) and completion_metadata:
        message_payload["metadata"] = completion_metadata

    Chats.upsert_message_to_chat_by_id_and_message_id(
        metadata["chat_id"],
        metadata["message_id"],
        message_payload,
    )
    return True


_SELECTED_SOURCE_RETRIEVAL_TOOL_NAMES = {
    "query_selected_knowledge_files",
    "read_selected_file",
}


def _iter_output_items(output: Any):
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                yield item
                content = item.get("content")
                if isinstance(content, list):
                    for child in content:
                        if isinstance(child, dict):
                            yield child
    elif isinstance(output, dict):
        yield output
        for key in ("output", "content"):
            value = output.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item


def _parse_tool_output_payload(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    result = item.get("result")
    if isinstance(result, dict):
        return result
    output = item.get("output")
    if isinstance(output, dict):
        return output
    if isinstance(output, list):
        for block in output:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                decoded = json.loads(text)
            except Exception:
                continue
            if isinstance(decoded, dict):
                return decoded
    return {}


def _normalize_retrieval_tool_status(status: str, code: str) -> str:
    normalized = str(status or "").strip().lower()
    normalized_code = str(code or "").strip().lower()
    if normalized in {"success", "completed"}:
        return "success"
    if "timeout" in normalized or "timeout" in normalized_code:
        return "timeout"
    if normalized in {"blocked", "denied", "permission_denied"} or "denied" in normalized_code:
        return "error"
    if normalized in {"no_evidence", "malformed", "error", "failed", "failure"}:
        return "error"
    return "error" if normalized_code else normalized or "error"


def _retrieval_tool_status_for_persistence(tool_outputs: Any) -> list[dict]:
    statuses: list[dict] = []
    seen: set[str] = set()
    calls_by_id: dict[str, str] = {}

    for item in _iter_output_items(tool_outputs):
        item_type = str(item.get("type") or "").strip()
        if item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            tool_name = str(item.get("name") or "").strip()
            if call_id and tool_name:
                calls_by_id[call_id] = tool_name
            continue

        if item_type != "function_call_output":
            continue

        call_id = str(item.get("call_id") or "").strip()
        payload = _parse_tool_output_payload(item)
        tool_name = str(
            payload.get("tool_name") or payload.get("function_name") or calls_by_id.get(call_id) or ""
        ).strip()
        if tool_name not in _SELECTED_SOURCE_RETRIEVAL_TOOL_NAMES:
            continue

        raw_status = str(payload.get("status") or item.get("status") or "").strip()
        code = str(payload.get("code") or "").strip()
        normalized_status = _normalize_retrieval_tool_status(raw_status, code)
        status_item = {
            "tool_name": tool_name,
            "status": normalized_status,
            "code": code or raw_status or normalized_status,
        }
        query = str(payload.get("query") or "").strip()
        source_id = str(payload.get("source_id") or "").strip()
        if query:
            status_item["query"] = query
        if source_id:
            status_item["source_id"] = source_id
        if payload.get("retrieval_round") not in (None, ""):
            status_item["retrieval_round"] = payload.get("retrieval_round")

        signature = json.dumps(status_item, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        statuses.append(status_item)

    return statuses


_NO_SELECTED_SOURCE_EVIDENCE_PATTERN = re.compile(
    r"(未在[^。；\n]{0,120}(?:找到|检索到)|"
    r"(?:未|没有)[^。；\n]{0,40}(?:找到|检索到)[^。；\n]{0,220}(?:证据|依据|相关)|"
    r"(?:未|没有)(?:找到|检索到)[^。；\n]{0,220}(?:原文|内容|结果|条款|信息)|"
    r"无[^。；\n]{0,40}(?:证据|依据)|"
    r"no\s+(?:usable\s+)?(?:evidence|results?)|"
    r"not\s+found)",
    re.IGNORECASE,
)
_NO_EVIDENCE_DIAGNOSTIC_CLASSIFICATIONS = {
    "no_evidence",
}
_NO_EVIDENCE_DIAGNOSTIC_OUTCOMES = {
    "blocked",
    "denied",
    "error",
    "low_relevance",
    "malformed",
    "no_evidence",
    "permission_denied",
    "timeout",
    "unauthorized",
    "weak_evidence",
}
_NO_EVIDENCE_TOOL_STATUS_VALUES = {
    "blocked",
    "denied",
    "error",
    "failed",
    "failure",
    "malformed",
    "no_evidence",
    "permission_denied",
    "timeout",
}

_EXPLICIT_INCOMPATIBLE_PROFILE_LOCK_REASONS = {
    "explicit_incompatible_profile",
    "chat_profile_zero_tool_lane",
}


def _normalized_match_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _trim_reference_by_accepted_snippets(
    reference: dict[str, Any],
    accepted_snippets: list[str],
) -> dict[str, Any]:
    if not isinstance(reference, dict):
        return {}
    if not accepted_snippets:
        return copy.deepcopy(reference)

    documents = reference.get("document") if isinstance(reference.get("document"), list) else []
    if not documents:
        return copy.deepcopy(reference)

    normalized_snippets = [
        _normalized_match_text(snippet)
        for snippet in accepted_snippets
        if _normalized_match_text(snippet)
    ]
    if not normalized_snippets:
        return copy.deepcopy(reference)

    accepted_indexes: list[int] = []
    for index, document in enumerate(documents):
        normalized_document = _normalized_match_text(document)
        if not normalized_document:
            continue
        if any(
            snippet in normalized_document or normalized_document in snippet
            for snippet in normalized_snippets
        ):
            accepted_indexes.append(index)

    if not accepted_indexes:
        # Keep the first document as a conservative fallback when snippets are present
        # but normalisation cannot map them exactly (e.g., formatting artifacts).
        accepted_indexes = [0]

    trimmed = copy.deepcopy(reference)
    for key, value in reference.items():
        if isinstance(value, list) and len(value) == len(documents):
            trimmed[key] = [value[index] for index in accepted_indexes]
    return trimmed


def _selected_source_references_for_persistence(
    references: object,
    *,
    accepted_outputs: object,
    strategy: object,
    active_source_scope: object,
) -> list[dict]:
    if not isinstance(references, list):
        return []

    clean_references = [item for item in references if isinstance(item, dict)]
    if not clean_references:
        return []

    strategy = strategy if isinstance(strategy, dict) else {}
    accepted_outputs = accepted_outputs if isinstance(accepted_outputs, list) else []
    metadata_first_intent = bool(strategy.get("metadata_first_intent"))
    targeted_context_bounded = bool(strategy.get("targeted_context_bounded"))
    if (
        metadata_first_intent
        and not targeted_context_bounded
        and not any(isinstance(item, dict) for item in accepted_outputs)
    ):
        return []
    snippet_map: dict[str, list[str]] = {}
    for item in accepted_outputs:
        if not isinstance(item, dict):
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        source_id = str(source.get("id") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if not source_id or not snippet:
            continue
        snippet_map.setdefault(source_id, [])
        if snippet not in snippet_map[source_id]:
            snippet_map[source_id].append(snippet)

    if not snippet_map:
        return copy.deepcopy(clean_references)

    allowed_scope_ids = set()
    if isinstance(active_source_scope, dict):
        for value in active_source_scope.get("source_ids") or []:
            normalized = str(value or "").strip()
            if normalized:
                allowed_scope_ids.add(normalized)

    trimmed_references: list[dict] = []
    for reference in clean_references:
        source = reference.get("source") if isinstance(reference.get("source"), dict) else {}
        source_id = str(source.get("id") or "").strip()
        if allowed_scope_ids and source_id and source_id not in allowed_scope_ids:
            continue

        snippets = snippet_map.get(source_id, [])
        if metadata_first_intent and not snippets:
            continue

        trimmed_reference = _trim_reference_by_accepted_snippets(reference, snippets)
        if trimmed_reference:
            trimmed_references.append(trimmed_reference)

    return trimmed_references


_SOURCE_SCOPE_CLARIFICATION_PATTERN = re.compile(
    r"(?:指代不清|请(?:先)?(?:明确|确认|告诉我).{0,40}(?:哪(?:一)?(?:份|个)|哪个).{0,30}(?:文件|知识库|来源)|"
    r"(?:which|what)\s+(?:file|document|knowledge\s*base|source).{0,80}(?:mean|refer|use|search))",
    re.IGNORECASE | re.DOTALL,
)


def _content_is_source_scope_clarification(content: str) -> bool:
    return bool(_SOURCE_SCOPE_CLARIFICATION_PATTERN.search(str(content or "")))


def _source_reference_is_web(source: dict) -> bool:
    if not isinstance(source, dict):
        return False
    source_info = source.get("source")
    source_info = source_info if isinstance(source_info, dict) else {}
    source_type = str(source_info.get("type") or source.get("source_class") or "").lower()
    source_url = str(source_info.get("url") or source_info.get("id") or "").strip()
    source_id = str(source_info.get("id") or "").strip()
    if "/minio/" in source_url and source_id and not source_id.startswith(("http://", "https://")):
        return False
    return source_type == "web" or source_url.startswith(("http://", "https://"))


def _metadata_indicates_no_evidence_state(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False

    diagnostics = metadata.get("retrieval_diagnostics")
    if isinstance(diagnostics, list):
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            classification = str(item.get("classification") or "").strip().lower()
            outcome = str(item.get("outcome") or item.get("status") or "").strip().lower()
            if classification in _NO_EVIDENCE_DIAGNOSTIC_CLASSIFICATIONS:
                return True
            if outcome in _NO_EVIDENCE_DIAGNOSTIC_OUTCOMES:
                return True

    tool_status = metadata.get("retrieval_tool_status")
    if isinstance(tool_status, list):
        for item in tool_status:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status in _NO_EVIDENCE_TOOL_STATUS_VALUES:
                return True

    return False


def _compact_execution_profile_routing_diagnostics(metadata: object) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}

    routing = (
        metadata.get("execution_profile_routing_diagnostics")
        if isinstance(metadata.get("execution_profile_routing_diagnostics"), dict)
        else {}
    )
    profile_lock = (
        metadata.get("first_pass_profile_lock")
        if isinstance(metadata.get("first_pass_profile_lock"), dict)
        else {}
    )
    resolved_execution_profile = str(
        routing.get("resolved_execution_profile")
        or metadata.get("resolved_execution_profile")
        or metadata.get("execution_profile")
        or profile_lock.get("resolved_execution_profile")
        or ""
    ).strip()
    classified_evidence_need = str(
        routing.get("classified_evidence_need")
        or profile_lock.get("classified_evidence_need")
        or ""
    ).strip()
    promotion_reason = str(routing.get("promotion_reason") or "").strip()
    non_promotion_reason = str(
        routing.get("non_promotion_reason") or profile_lock.get("non_promotion_reason") or ""
    ).strip()
    explicit_profile_lock = bool(
        profile_lock.get("incompatible")
        or non_promotion_reason.strip().lower()
        in _EXPLICIT_INCOMPATIBLE_PROFILE_LOCK_REASONS
    )
    strategy = (
        metadata.get("first_pass_retrieval_strategy")
        if isinstance(metadata.get("first_pass_retrieval_strategy"), dict)
        else {}
    )
    if not classified_evidence_need or classified_evidence_need.lower() == "none":
        strategy_evidence_need = str(strategy.get("evidence_need") or "").strip().lower()
        strategy_retrieval = str(strategy.get("retrieval_strategy") or "").strip().lower()
        if strategy_evidence_need and strategy_evidence_need != "none":
            classified_evidence_need = strategy_evidence_need
        elif bool(strategy.get("metadata_first_intent")) or (
            strategy_retrieval == "metadata_first_then_targeted_chunks"
        ):
            classified_evidence_need = "metadata_first_inventory"

    if not (
        resolved_execution_profile
        or classified_evidence_need
        or promotion_reason
        or non_promotion_reason
        or explicit_profile_lock
    ):
        return {}

    compact: dict[str, Any] = {
        "resolved_execution_profile": resolved_execution_profile or "general",
        "classified_evidence_need": classified_evidence_need or "none",
        "promotion_reason": promotion_reason,
        "non_promotion_reason": non_promotion_reason,
        "explicit_profile_lock": explicit_profile_lock,
    }
    return compact


def _filter_uncited_no_evidence_source_cards(
    sources: object,
    *,
    content: str,
    metadata: object = None,
) -> list[dict]:
    """Avoid rendering weak selected-KB diagnostics as accepted source cards."""

    if not isinstance(sources, list):
        return []

    clean_sources = [source for source in sources if isinstance(source, dict)]
    if not clean_sources:
        return []

    if _selected_source_metadata_first_diagnostics_lane(metadata):
        # Selected-source metadata-first blocked/no-evidence/unsupported turns
        # are diagnostics-only and must not persist any source cards.
        return []

    content = str(content or "")
    if _content_is_source_scope_clarification(content):
        return []

    no_evidence_text = bool(_NO_SELECTED_SOURCE_EVIDENCE_PATTERN.search(content))
    no_evidence_metadata = _metadata_indicates_no_evidence_state(metadata)
    if not no_evidence_text and not no_evidence_metadata:
        return clean_sources

    # If the model cited a source explicitly, keep the source list; otherwise
    # remove local/KB-only cards from a no-evidence answer while preserving web
    # evidence in mixed local+web turns.
    if re.search(r"\[\s*\d+(?:\s*[-,，]\s*\d+)*\s*\]", content):
        return clean_sources

    return [source for source in clean_sources if _source_reference_is_web(source)]


def _metadata_is_selected_source_turn(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False

    active_scope = (
        metadata.get("active_source_scope")
        if isinstance(metadata.get("active_source_scope"), dict)
        else {}
    )
    focus_state = str(active_scope.get("focus_state") or "").strip().lower()
    authority = str(active_scope.get("authority") or "").strip().lower()
    if focus_state in {"single", "multi"} and authority in {
        "selected_source",
        "current_upload",
        "user_clarification",
        "canonical_reference",
    }:
        return True

    for item in metadata.get("retrieval_tool_status") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("tool_name") or "").strip() in _SELECTED_SOURCE_RETRIEVAL_TOOL_NAMES:
            return True
    for item in metadata.get("retrieval_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("tool_name") or "").strip() in _SELECTED_SOURCE_RETRIEVAL_TOOL_NAMES:
            return True

    strategy = (
        metadata.get("first_pass_retrieval_strategy")
        if isinstance(metadata.get("first_pass_retrieval_strategy"), dict)
        else {}
    )
    if strategy:
        return True

    return False


def _selected_source_metadata_first_diagnostics_lane(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    if not _metadata_is_selected_source_turn(metadata):
        return False
    if not _selected_source_metadata_first_intent(metadata):
        return False

    status = str(metadata.get("status") or "").strip().lower()
    terminal_reason = str(metadata.get("terminal_reason") or "").strip().lower()
    if status in {"blocked", "no_evidence"}:
        return True
    if terminal_reason.startswith("unsupported_"):
        return True

    reason_codes = {
        str(item.get("reason") or "").strip().lower()
        for item in (metadata.get("retrieval_diagnostics") or [])
        if isinstance(item, dict)
    }
    return any(
        reason.startswith("unsupported_")
        or reason
        in {
            "missing_date_metadata",
            "missing_type_metadata",
            "missing_topic_metadata",
            "no_accepted_references",
        }
        for reason in reason_codes
    )


def _merge_persisted_and_response_sources(
    persisted_sources: list[dict],
    response_sources: object,
    *,
    completion_metadata: object,
) -> list[dict]:
    if not isinstance(persisted_sources, list):
        persisted_sources = []
    persisted_sources = [item for item in persisted_sources if isinstance(item, dict)]
    if _metadata_is_selected_source_turn(completion_metadata):
        active_source_scope = (
            completion_metadata.get("active_source_scope")
            if isinstance(completion_metadata, dict)
            else None
        )
        scoped_sources = [
            source
            for source in persisted_sources
            if _source_fits_active_scope(source, active_source_scope)
        ]
        # Keep selected-source persistence scoped to the active selection when
        # at least one accepted scoped reference exists; otherwise preserve the
        # original list to avoid dropping legitimate narrow-fact citations.
        if scoped_sources:
            return scoped_sources
        return persisted_sources
    return Chats._merge_message_sources(persisted_sources, response_sources)


def _build_assistant_reference_persistence_metadata(
    metadata: dict,
    *,
    message_metadata: Any = None,
    tool_outputs: Any = None,
) -> dict:
    if not isinstance(metadata, dict):
        return {}

    base_metadata = {}
    if isinstance(message_metadata, dict):
        base_metadata.update(message_metadata)

    status_value = str(metadata.get("status") or "").strip().lower()
    if status_value:
        base_metadata["status"] = status_value
    terminal_reason = str(metadata.get("terminal_reason") or "").strip().lower()
    if terminal_reason:
        base_metadata["terminal_reason"] = terminal_reason

    active_source_scope = Chats.normalize_active_source_scope(
        metadata.get("active_source_scope")
    )
    if isinstance(active_source_scope, dict) and active_source_scope:
        base_metadata["active_source_scope"] = active_source_scope

    retrieval_diagnostics = Chats._merge_retrieval_diagnostics(
        base_metadata.get("retrieval_diagnostics"),
        metadata.get("retrieval_diagnostics"),
    )
    if retrieval_diagnostics:
        base_metadata["retrieval_diagnostics"] = retrieval_diagnostics

    explicit_references = (
        metadata.get("references") if isinstance(metadata.get("references"), list) else None
    )
    accepted_outputs = (
        metadata.get("accepted_outputs")
        if isinstance(metadata.get("accepted_outputs"), list)
        else []
    )
    first_pass_strategy = (
        metadata.get("first_pass_retrieval_strategy")
        if isinstance(metadata.get("first_pass_retrieval_strategy"), dict)
        else {}
    )
    if explicit_references is not None:
        explicit_references = _selected_source_references_for_persistence(
            explicit_references,
            accepted_outputs=accepted_outputs,
            strategy=first_pass_strategy,
            active_source_scope=active_source_scope,
        )
    if (
        not status_value
        and bool(first_pass_strategy.get("metadata_first_intent"))
        and not bool(first_pass_strategy.get("targeted_context_bounded"))
    ):
        status_value = "blocked"
        terminal_reason = "metadata_first_targeted_evidence_required"
        base_metadata["status"] = status_value
        base_metadata["terminal_reason"] = terminal_reason
    if explicit_references is not None:
        # Current-turn accepted references take precedence over stale message metadata.
        for key in (
            "canonical_references",
            "reference_cards",
            "sources",
            "citations",
            "references",
            "documents",
        ):
            base_metadata.pop(key, None)
        base_metadata["references"] = explicit_references
    sources_for_sidecar: Any = (
        explicit_references
        if explicit_references is not None
        else metadata.get("sources")
    )
    if (
        explicit_references is not None
        and len(explicit_references) == 0
        and str(metadata.get("status") or "").strip().lower() in {"blocked", "no_evidence"}
    ):
        sources_for_sidecar = []
    if (
        str(metadata.get("status") or "").strip().lower() in {"blocked", "no_evidence"}
        and _metadata_indicates_no_evidence_state(metadata)
    ):
        sources_for_sidecar = []
    if status_value in {"blocked", "no_evidence"} and not explicit_references:
        sources_for_sidecar = []
    if (
        status_value in {"blocked", "no_evidence"}
        and not retrieval_diagnostics
        and terminal_reason
    ):
        retrieval_diagnostics = [
            _safe_retrieval_diagnostic(
                classification="no_evidence",
                reason=terminal_reason,
                candidate_index=-1,
                outcome=status_value,
            )
        ]
        base_metadata["retrieval_diagnostics"] = retrieval_diagnostics

    sidecar = Chats.build_reference_metadata_sidecar(
        metadata=base_metadata,
        sources=sources_for_sidecar,
        diagnostics=retrieval_diagnostics,
        tool_outputs=tool_outputs,
    )

    persisted: dict[str, Any] = {}
    if isinstance(active_source_scope, dict) and active_source_scope:
        persisted["active_source_scope"] = active_source_scope
    if sidecar.get("retrieval_diagnostics"):
        persisted["retrieval_diagnostics"] = sidecar["retrieval_diagnostics"]
    elif retrieval_diagnostics and not sidecar.get("canonical_references"):
        persisted["retrieval_diagnostics"] = retrieval_diagnostics
    if sidecar.get("canonical_references"):
        persisted["canonical_references"] = sidecar["canonical_references"]
    if sidecar.get("reference_cards"):
        persisted["reference_cards"] = sidecar["reference_cards"]
    if status_value:
        persisted["status"] = status_value
    if terminal_reason:
        persisted["terminal_reason"] = terminal_reason

    compact_profile_routing = _compact_execution_profile_routing_diagnostics(metadata)
    if compact_profile_routing:
        persisted["execution_profile_routing_diagnostics"] = compact_profile_routing

    existing_tool_status = (
        base_metadata.get("retrieval_tool_status")
        if isinstance(base_metadata.get("retrieval_tool_status"), list)
        else []
    )
    retrieval_tool_status = _retrieval_tool_status_for_persistence(tool_outputs)
    merged_tool_status = []
    seen_tool_status: set[str] = set()
    for item in [*existing_tool_status, *retrieval_tool_status]:
        if not isinstance(item, dict):
            continue
        signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen_tool_status:
            continue
        seen_tool_status.add(signature)
        merged_tool_status.append(item)
    if merged_tool_status:
        persisted["retrieval_tool_status"] = merged_tool_status

    provenance = metadata.get("provenance") if isinstance(metadata, dict) else {}
    if isinstance(provenance, dict):
        compact = provenance.get("compact_retrieval_provenance")
        if isinstance(compact, dict) and compact:
            persisted["retrieval_provenance"] = copy.deepcopy(compact)
        elif provenance:
            persisted["retrieval_provenance"] = {
                "strategy": {
                    "evidence_need": str(
                        (metadata.get("first_pass_retrieval_strategy") or {}).get(
                            "evidence_need"
                        )
                        or ""
                    ),
                    "retrieval_strategy": str(
                        (metadata.get("first_pass_retrieval_strategy") or {}).get(
                            "retrieval_strategy"
                        )
                        or ""
                    ),
                    "semantic_chunk_lookup_ok": bool(
                        (metadata.get("first_pass_retrieval_strategy") or {}).get(
                            "semantic_chunk_lookup_ok", True
                        )
                    ),
                },
                "accepted_counts": {
                    "reference_count": len(
                        [
                            item
                            for item in metadata.get("references") or []
                            if isinstance(item, dict)
                        ]
                    ),
                    "accepted_output_count": len(
                        [
                            item
                            for item in metadata.get("accepted_outputs") or []
                            if isinstance(item, dict)
                        ]
                    ),
                },
                "material_limitations": [
                    str(item.get("reason") or "").strip()
                    for item in metadata.get("retrieval_diagnostics") or []
                    if isinstance(item, dict)
                    and str(item.get("reason") or "").strip()
                ],
            }

    return persisted


def _build_assistant_reference_seed_metadata(metadata: dict) -> dict:
    seed_metadata = _build_assistant_reference_persistence_metadata(metadata)
    if not isinstance(seed_metadata, dict) or not seed_metadata:
        return {}

    seed_metadata.pop("canonical_references", None)
    seed_metadata.pop("reference_cards", None)
    if not seed_metadata:
        return {}

    if seed_metadata.get("retrieval_diagnostics"):
        return seed_metadata

    if Chats._should_suppress_canonical_references(metadata=seed_metadata):
        return seed_metadata

    return {}


def deep_merge(target, source):
    """
    Merge source into target recursively (returning new structure).
    - Dicts: Recursive merge.
    - Strings: Concatenation.
    - Others: Overwrite.
    """
    if isinstance(target, dict) and isinstance(source, dict):
        new_target = target.copy()
        for k, v in source.items():
            if k in new_target:
                new_target[k] = deep_merge(new_target[k], v)
            else:
                new_target[k] = v
        return new_target
    elif isinstance(target, str) and isinstance(source, str):
        return target + source
    else:
        return source


def _resolve_output_item_index(
    current_output: list,
    *,
    output_index: int | None = None,
    item: dict | None = None,
) -> int | None:
    if output_index is not None and 0 <= output_index < len(current_output):
        return output_index

    if not isinstance(item, dict):
        return None

    item_id = item.get("id")
    call_id = item.get("call_id")
    for index, current_item in enumerate(current_output):
        if not isinstance(current_item, dict):
            continue
        if item_id and current_item.get("id") == item_id:
            return index
        if call_id and current_item.get("call_id") == call_id:
            return index

    return None


def handle_responses_streaming_event(
    data: dict,
    current_output: list,
) -> tuple[list, dict | None]:
    """
    Handle Responses API streaming events in a pure functional way.

    Args:
        data: The event data
        current_output: List of output items (treated as immutable)

    Returns:
        tuple[list, dict | None]: (new_output, metadata)
        - new_output: The updated output list.
        - metadata: Metadata to emit (e.g. usage), {} if update occurred, None if skip.
    """
    # Default: no change
    # Note: treating current_output as immutable, but avoiding full deepcopy for perf.
    # We will shallow copy only if we need to modify the list structure or items.

    event_type = data.get("type", "")

    def output_has_tool_items(items: Any) -> bool:
        if not isinstance(items, list):
            return False
        for entry in items:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") in {"function_call", "function_call_output"}:
                return True
        return False

    if event_type == "response.output_item.added":
        item = data.get("item", {})
        if item:
            new_output = list(current_output)
            new_output.append(item)
            return new_output, None
        return current_output, None

    elif event_type == "response.content_part.added":
        part = data.get("part", {})
        output_index = data.get("output_index", len(current_output) - 1)

        if current_output and 0 <= output_index < len(current_output):
            new_output = list(current_output)
            # Copy the item to mutate it
            item = new_output[output_index].copy()
            new_output[output_index] = item

            if "content" not in item:
                item["content"] = []
            else:
                # Copy content list
                item["content"] = list(item["content"])

            if item.get("type") == "reasoning":
                # Reasoning items should not have content parts
                pass
            else:
                item["content"].append(part)
            return new_output, None
        return current_output, None

    elif event_type == "response.reasoning_summary_part.added":
        part = data.get("part", {})
        output_index = data.get("output_index", len(current_output) - 1)

        if current_output and 0 <= output_index < len(current_output):
            new_output = list(current_output)
            item = new_output[output_index].copy()
            new_output[output_index] = item

            if "summary" not in item:
                item["summary"] = []
            else:
                item["summary"] = list(item["summary"])

            item["summary"].append(part)
            return new_output, None
        return current_output, None

    elif event_type.startswith("response.") and event_type.endswith(".delta"):
        # Generic Delta Handling
        parts = event_type.split(".")
        if len(parts) >= 3:
            delta_type = parts[1]
            delta = data.get("delta", "")

            output_index = data.get("output_index", len(current_output) - 1)

            if current_output and 0 <= output_index < len(current_output):
                new_output = list(current_output)
                item = new_output[output_index].copy()
                new_output[output_index] = item
                item_type = item.get("type", "")

                # Determine target field and object based on delta_type and item_type
                if delta_type == "function_call_arguments":
                    key = "arguments"
                    if item_type == "function_call":
                        # Function call args are usually strings
                        item[key] = item.get(key, "") + str(delta)
                else:
                    # Generic handling, refined by item type below
                    pass

                    if item_type == "message":
                        # Message items: "text"/"output_text" -> "text"
                        # "reasoning_text" -> Skipped (should use reasoning item)
                        if delta_type in ["text", "output_text"]:
                            key = "text"
                        elif delta_type in ["reasoning_text", "reasoning_summary_text"]:
                            # Skip reasoning updates for message items
                            return new_output, None
                        else:
                            key = delta_type

                        content_index = data.get("content_index", 0)
                        if "content" not in item:
                            item["content"] = []
                        else:
                            item["content"] = list(item["content"])
                        content_list = item["content"]

                        while len(content_list) <= content_index:
                            content_list.append({"type": "text", "text": ""})

                        # Copy the part to mutate it
                        part = content_list[content_index].copy()
                        content_list[content_index] = part

                        current_val = part.get(key)
                        if current_val is None:
                            # Initialize based on delta type
                            current_val = {} if isinstance(delta, dict) else ""

                        part[key] = deep_merge(current_val, delta)

                    elif item_type == "reasoning":
                        # Reasoning items: "reasoning_text"/"reasoning_summary_text" -> "text"
                        # "text"/"output_text" -> Skipped (should use message item)
                        if delta_type == "reasoning_summary_text":
                            # Summary updates -> item['summary']
                            key = "text"
                            summary_index = data.get("summary_index", 0)
                            if "summary" not in item:
                                item["summary"] = []
                            else:
                                item["summary"] = list(item["summary"])
                            summary_list = item["summary"]

                            while len(summary_list) <= summary_index:
                                summary_list.append(
                                    {"type": "summary_text", "text": ""}
                                )

                            part = summary_list[summary_index].copy()
                            summary_list[summary_index] = part

                            target_val = part.get(key, "")
                            part[key] = deep_merge(target_val, delta)

                        elif delta_type == "reasoning_text":
                            # Reasoning body updates -> item['content']
                            key = "text"
                            content_index = data.get("content_index", 0)
                            if "content" not in item:
                                item["content"] = []
                            else:
                                item["content"] = list(item["content"])
                            content_list = item["content"]

                            while len(content_list) <= content_index:
                                # Reasoning content parts default to text
                                content_list.append({"type": "text", "text": ""})

                            part = content_list[content_index].copy()
                            content_list[content_index] = part

                            target_val = part.get(key, "")
                            part[key] = deep_merge(target_val, delta)

                        elif delta_type in ["text", "output_text"]:
                            return new_output, None
                        else:
                            # Fallback just in case other deltas target reasoning?
                            pass

                    else:
                        # Fallback for other item types
                        if delta_type in ["text", "output_text"]:
                            key = "text"
                        else:
                            key = delta_type

                        current_val = item.get(key)
                        if current_val is None:
                            current_val = {} if isinstance(delta, dict) else ""
                        item[key] = deep_merge(current_val, delta)

            return new_output, None

    elif event_type.startswith("response.") and event_type.endswith(".done"):
        # Delta Events: response.content_part.done, response.text.done, etc.
        parts = event_type.split(".")
        if len(parts) >= 3:
            type_name = parts[1]

            # 1. Handle specific Delta "done" signals
            if type_name == "content_part":
                # "Signaling that no further changes will occur to a content part"
                # If payloads contains the full part, we could update it.
                # Usually purely signaling in standard implementation, but we check payload.
                part = data.get("part")
                output_index = data.get("output_index", len(current_output) - 1)

                if part and current_output and 0 <= output_index < len(current_output):
                    new_output = list(current_output)
                    item = new_output[output_index].copy()
                    new_output[output_index] = item

                    if "content" in item:
                        item["content"] = list(item["content"])
                        content_index = data.get(
                            "content_index", len(item["content"]) - 1
                        )
                        if 0 <= content_index < len(item["content"]):
                            item["content"][content_index] = part
                            return new_output, {}
                return current_output, None

            elif type_name == "reasoning_summary_part":
                part = data.get("part")
                output_index = data.get("output_index", len(current_output) - 1)

                if part and current_output and 0 <= output_index < len(current_output):
                    new_output = list(current_output)
                    item = new_output[output_index].copy()
                    new_output[output_index] = item

                    if "summary" in item:
                        item["summary"] = list(item["summary"])
                        summary_index = data.get(
                            "summary_index", len(item["summary"]) - 1
                        )
                        if 0 <= summary_index < len(item["summary"]):
                            item["summary"][summary_index] = part
                            return new_output, {}
                return current_output, None

            # 2. Skip Output Item done (handled specifically below)
            if type_name == "output_item":
                pass

            # 3. Generic Field Done (text.done, audio.done)
            elif type_name not in ["completed", "failed"]:
                output_index = data.get("output_index", len(current_output) - 1)
                if current_output and 0 <= output_index < len(current_output):

                    key = (
                        "text"
                        if type_name
                        in [
                            "text",
                            "output_text",
                            "reasoning_text",
                            "reasoning_summary_text",
                        ]
                        else type_name
                    )
                    if type_name == "function_call_arguments":
                        key = "arguments"

                    if key in data:
                        final_value = data[key]
                        new_output = list(current_output)
                        item = new_output[output_index].copy()
                        new_output[output_index] = item
                        item_type = item.get("type", "")

                        if type_name == "function_call_arguments":
                            if item_type == "function_call":
                                item["arguments"] = final_value
                        elif item_type == "message":
                            content_index = data.get("content_index", 0)
                            if "content" in item:
                                item["content"] = list(item["content"])
                                if len(item["content"]) > content_index:
                                    part = item["content"][content_index].copy()
                                    item["content"][content_index] = part
                                    part[key] = final_value
                        elif item_type == "reasoning":
                            item["status"] = "completed"
                        else:
                            item[key] = final_value

                        return new_output, {}

        return current_output, None

    elif event_type == "response.output_item.done":
        # Delta Event: Output item complete
        item = data.get("item")
        output_index = _resolve_output_item_index(
            current_output,
            output_index=data.get("output_index"),
            item=item,
        )

        new_output = list(current_output)
        if item and output_index is not None:
            new_output[output_index] = item
        elif item:
            new_output.append(item)
        return new_output, {}

    elif event_type == "response.completed":
        # State Machine Event: Completed
        response_data = data.get("response", {})
        final_output = response_data.get("output")

        if isinstance(final_output, list):
            # Prefer accumulated streaming output when the completed payload omits tool items.
            if current_output and output_has_tool_items(current_output) and not output_has_tool_items(final_output):
                new_output = current_output
            elif current_output and not final_output:
                new_output = current_output
            else:
                new_output = final_output
        else:
            new_output = current_output

        # Ensure reasoning items are marked as completed in the final output
        if new_output:
            for item in new_output:
                if (
                    item.get("type") == "reasoning"
                    and item.get("status") != "completed"
                ):
                    item["status"] = "completed"

        return new_output, {"usage": response_data.get("usage"), "done": True}

    elif event_type == "response.in_progress":
        # State Machine Event: In Progress
        # We could extract metadata if needed, but for now just acknowledge iteration
        return current_output, None

    elif event_type == "response.failed":
        # State Machine Event: Failed
        error = data.get("response", {}).get("error", {})
        return current_output, {"error": error}

    else:
        return current_output, None


def get_source_context(
    sources: list, source_ids: dict = None, include_content: bool = True
) -> str:
    """
    Build <source> tag context string from citation sources.
    """
    context_string = ""
    if source_ids is None:
        source_ids = {}
    for source in sources:
        for doc, meta in zip(source.get("document", []), source.get("metadata", [])):
            source_id = (
                meta.get("source") or source.get("source", {}).get("id") or "N/A"
            )
            if source_id not in source_ids:
                source_ids[source_id] = len(source_ids) + 1
            src_name = source.get("source", {}).get("name")
            body = doc if include_content else ""
            context_string += (
                f'<source id="{source_ids[source_id]}"'
                + (f' name="{src_name}"' if src_name else "")
                + f">{body}</source>\n"
            )
    return context_string


def apply_source_context_to_messages(
    request: Request,
    messages: list,
    sources: list,
    user_message: str,
    include_content: bool = True,
) -> list:
    """
    Build source context from citation sources and apply to messages.
    Uses RAG template to format context for model consumption.

    When include_content is False, emit <source> tags with id/name but no
    document body — useful when the content is already present elsewhere
    (e.g. in a tool result message) and only citation markers are needed.
    """
    if not sources or not user_message:
        return messages

    context = get_source_context(sources, include_content=include_content)

    context = context.strip()
    if not context:
        return messages

    rag_prompt = rag_template(request.app.state.config.RAG_TEMPLATE, context, user_message)
    follow_up_context_guidance = build_follow_up_context_guidance(messages)
    if follow_up_context_guidance:
        rag_prompt = f"{rag_prompt}\n\n{follow_up_context_guidance}"

    if RAG_SYSTEM_CONTEXT:
        return add_or_update_system_message(
            rag_prompt,
            messages,
            append=True,
        )
    else:
        return add_or_update_user_message(
            rag_prompt,
            messages,
            append=False,
        )


TERMINAL_GENERATED_FILE_TOOLS = {
    "write_file",
    "replace_file_content",
    "write_structured_file",
    "gotenberg_convert",
}
BRIDGE_GENERATED_FILE_TOOLS = {
    "write_file",
    "replace_file_content",
    "write_structured_file",
}
BRIDGE_BINARY_GENERATED_FILE_TOOLS = {
    "gotenberg_convert",
    "pdf_create_document",
    "pdf_inspect_form",
    "pdf_fill_form_tool",
    "pdf_reformat_document",
    "docx_export_document",
    "pptx_export_presentation",
    "xlsx_create_workbook",
    "xlsx_add_column_tool",
    "xlsx_insert_row_tool",
}
TERMINAL_GENERATED_FILE_PATH_KEYS = (
    "path",
    "output_path",
    "target_path",
    "file_path",
    "pdf_path",
)
TOOL_RESULT_FILE_REF_PREFIXES = (
    "http://",
    "https://",
    "data:",
    "/api/v1/files/",
    "/openai/v1/files/",
    "/v1/files/",
)
BRIDGE_TOOL_CALL_BLOCK_RE = re.compile(
    r"<details\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</details>",
    re.IGNORECASE,
)
BRIDGE_TOOL_CALL_ATTR_RE = re.compile(r'([A-Za-z_:][\w:.-]*)="([^"]*)"')
BRIDGE_DETAILS_TYPE_RE = re.compile(r'\btype\s*=\s*"([^"]+)"', re.IGNORECASE)
BRIDGE_TEXTUAL_CONTENT_KEYS = ("content", "text", "body")
BRIDGE_FILE_PATH_KEYS = ("path", "file_path", "file", "output_path", "target_path")
BRIDGE_TEXTUAL_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/rtf",
    "application/sql",
    "application/xml",
    "application/x-sh",
    "application/x-yaml",
    "application/yaml",
}
CLIENT_CAPABILITIES_SCHEMA_VERSION = 2
CLIENT_CAPABILITIES_PREVIEW_TYPES = (
    "image",
    "pdf",
    "docx",
    "xlsx",
    "pptx",
    "html",
    "mermaid",
    "text",
    "markdown",
    "code",
    "json",
    "csv",
    "notebook",
    "sqlite",
    "audio",
    "video",
)


def _normalize_bridge_output_events(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _strip_bridge_details_blocks(
    content: str,
    *,
    types: Optional[set[str]] = None,
) -> str:
    if not isinstance(content, str) or "<details" not in content:
        return content
    type_filter = {t.lower() for t in (types or {"tool_calls", "reasoning"})}

    def replace_block(match: re.Match[str]) -> str:
        attrs_text = match.group("attrs") or ""
        type_match = BRIDGE_DETAILS_TYPE_RE.search(attrs_text)
        if not type_match:
            return match.group(0)
        if type_match.group(1).strip().lower() in type_filter:
            return ""
        return match.group(0)

    return BRIDGE_TOOL_CALL_BLOCK_RE.sub(replace_block, content)


def _tool_result_explicitly_failed(tool_result: Any) -> bool:
    if not isinstance(tool_result, dict):
        return False

    if tool_result.get("success") is False:
        return True

    status = str(tool_result.get("status", "") or "").strip().lower()
    if status in {"error", "failed", "timeout"}:
        return True

    return False


def _normalize_terminal_file_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if not normalized or normalized.lower() in {"null", "undefined"}:
        return None

    if normalized.startswith(TOOL_RESULT_FILE_REF_PREFIXES):
        return None

    return normalized


def _extract_terminal_generated_file_candidates(
    tool_function_name: str, tool_result: Any
) -> list[dict]:
    if tool_function_name not in TERMINAL_GENERATED_FILE_TOOLS:
        return []

    candidates: list[dict] = []
    seen_paths: set[str] = set()

    def extract_path_from_text(text: str) -> Optional[str]:
        if not isinstance(text, str):
            return None

        candidate = text.strip()
        if not candidate or candidate.lower() in {"null", "undefined"}:
            return None

        if candidate.startswith(("/", "./", "../", "~/")):
            return candidate

        match = re.search(r"(/[^\\s\"']+)", candidate)
        if match:
            return match.group(1)

        return None

    def append_candidate(path_value: Any, payload: dict) -> None:
        path = _normalize_terminal_file_path(path_value)
        if not path or path in seen_paths:
            return

        seen_paths.add(path)

        filename = next(
            (
                str(payload.get(key)).strip()
                for key in ("filename", "fileName", "name")
                if isinstance(payload.get(key), str) and str(payload.get(key)).strip()
            ),
            os.path.basename(path.rstrip("/")) or "generated-file",
        )
        content_type = next(
            (
                str(payload.get(key)).strip()
                for key in ("content_type", "contentType", "mime_type", "mimeType")
                if isinstance(payload.get(key), str) and str(payload.get(key)).strip()
            ),
            None,
        )
        size_value = payload.get("size")
        size = int(size_value) if isinstance(size_value, (int, float)) else None

        candidates.append(
            {
                "path": path,
                "name": filename,
                "content_type": content_type,
                "size": size,
            }
        )

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if _tool_result_explicitly_failed(node):
                return

            for key in TERMINAL_GENERATED_FILE_PATH_KEYS:
                if key in node:
                    append_candidate(node.get(key), node)

            for value in node.values():
                walk(value)
        elif isinstance(node, str):
            extracted = extract_path_from_text(node)
            if extracted:
                append_candidate(extracted, {"path": extracted})
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tool_result)
    return candidates


def _extract_filename_from_content_disposition(content_disposition: str) -> Optional[str]:
    if not content_disposition:
        return None

    utf8_match = re.search(
        r"filename\*\s*=\s*UTF-8''(?P<value>[^;]+)",
        content_disposition,
        flags=re.IGNORECASE,
    )
    if utf8_match:
        value = utf8_match.group("value").strip().strip('"')
        return requests.utils.unquote(value)

    quoted_match = re.search(
        r'filename\s*=\s*"(?P<value>[^"]+)"',
        content_disposition,
        flags=re.IGNORECASE,
    )
    if quoted_match:
        return quoted_match.group("value").strip()

    plain_match = re.search(
        r"filename\s*=\s*(?P<value>[^;]+)",
        content_disposition,
        flags=re.IGNORECASE,
    )
    if plain_match:
        return plain_match.group("value").strip().strip('"')

    return None


def _get_terminal_connection(request: Request, terminal_id: str, user: UserModel) -> Optional[dict]:
    connections = request.app.state.config.TERMINAL_SERVER_CONNECTIONS or []
    connection = next(
        (
            item
            for item in connections
            if item.get("id") == terminal_id and item.get("enabled", True)
        ),
        None,
    )
    if connection is None:
        return None

    if not has_connection_access(user, connection):
        return None

    return connection


def _download_terminal_file(
    request: Request,
    terminal_id: str,
    path: str,
    user: UserModel,
) -> Optional[dict]:
    connection = _get_terminal_connection(request, terminal_id, user)
    if connection is None:
        return None

    base_url = str(connection.get("url", "") or "").rstrip("/")
    if not base_url:
        return None

    target_url = f"{base_url}/files/view"
    headers = {"X-User-Id": user.id}
    cookies = {}

    auth_type = connection.get("auth_type", "bearer")
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {connection.get('key', '')}"
    elif auth_type == "session":
        token = getattr(getattr(request.state, "token", None), "credentials", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        cookies = request.cookies
    elif auth_type == "system_oauth":
        oauth_token = request.headers.get("x-oauth-access-token", "")
        if oauth_token:
            headers["Authorization"] = f"Bearer {oauth_token}"
        cookies = request.cookies

    try:
        response = requests.get(
            target_url,
            params={"path": path},
            headers=headers,
            cookies=cookies,
            timeout=(10, 300),
            allow_redirects=False,
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("Failed to download terminal file %s from %s: %s", path, terminal_id, exc)
        return None

    filename = _extract_filename_from_content_disposition(
        response.headers.get("Content-Disposition", "")
    ) or os.path.basename(path.rstrip("/"))
    if not filename:
        filename = "generated-file"

    content_type = (
        response.headers.get("Content-Type")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    return {
        "content": response.content,
        "filename": filename,
        "content_type": content_type,
    }


def _upload_terminal_generated_file(
    request: Request,
    tool_function_name: str,
    candidate: dict,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> Optional[dict]:
    if user is None or not isinstance(metadata, dict):
        return None

    terminal_id = str(metadata.get("terminal_id") or "").strip()
    if not terminal_id:
        return None

    path = str(candidate.get("path") or "").strip()
    if not path:
        return None

    download = _download_terminal_file(request, terminal_id, path, user)
    if not download or not isinstance(download.get("content"), (bytes, bytearray)):
        return None

    filename = str(download.get("filename") or candidate.get("name") or "").strip()
    if not filename:
        filename = os.path.basename(path.rstrip("/")) or "generated-file"

    content_type = str(
        download.get("content_type")
        or candidate.get("content_type")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    ).strip()

    file = UploadFile(
        file=io.BytesIO(download["content"]),
        filename=filename,
        headers={"content-type": content_type},
    )

    file_metadata = {
        "source": "terminal_tool_result",
        "tool_name": tool_function_name,
        "terminal_id": terminal_id,
        "terminal_path": path,
        "chat_id": metadata.get("chat_id"),
        "message_id": metadata.get("message_id"),
        "session_id": metadata.get("session_id"),
    }
    file_metadata = {
        key: value for key, value in file_metadata.items() if value not in (None, "")
    }

    try:
        file_item = upload_file_handler(
            request,
            file=file,
            metadata=file_metadata,
            process=False,
            user=user,
        )
    except Exception as exc:
        log.warning("Failed to upload terminal file %s into storage: %s", path, exc)
        return None

    if not file_item:
        return None

    file_meta = getattr(file_item, "meta", {}) or {}
    file_id = str(getattr(file_item, "id", "") or "").strip()
    if not file_id:
        return None

    content_path = request.app.url_path_for("get_file_content_by_id", id=file_id)
    normalized_content_type = (
        str(file_meta.get("content_type") or content_type).strip()
        or "application/octet-stream"
    )

    return {
        "id": file_id,
        "url": str(content_path),
        "name": str(file_meta.get("name") or getattr(file_item, "filename", filename)),
        "filename": str(getattr(file_item, "filename", filename)),
        "type": "image" if normalized_content_type.startswith("image/") else "file",
        "content_type": normalized_content_type,
        "size": file_meta.get("size"),
    }


def _collect_terminal_generated_files(
    request: Request,
    tool_function_name: str,
    tool_result: Any,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> list[dict]:
    if user is None:
        return []

    files: list[dict] = []
    for candidate in _extract_terminal_generated_file_candidates(
        tool_function_name, tool_result
    ):
        uploaded = _upload_terminal_generated_file(
            request,
            tool_function_name,
            candidate,
            metadata,
            user,
        )
        if uploaded:
            files.append(uploaded)

    return files


def process_tool_result(
    request,
    tool_function_name,
    tool_result,
    tool_type,
    direct_tool=False,
    metadata=None,
    user=None,
):
    tool_result_embeds = []
    EXTERNAL_TOOL_TYPES = ("external", "action", "terminal")

    if isinstance(tool_result, HTMLResponse):
        content_disposition = tool_result.headers.get("Content-Disposition", "")
        if "inline" in content_disposition:
            content = tool_result.body.decode("utf-8", "replace")
            tool_result_embeds.append(content)

            if 200 <= tool_result.status_code < 300:
                tool_result = {
                    "status": "success",
                    "code": "ui_component",
                    "message": f"{tool_function_name}: Embedded UI result is active and visible to the user.",
                }
            elif 400 <= tool_result.status_code < 500:
                tool_result = {
                    "status": "error",
                    "code": "ui_component",
                    "message": f"{tool_function_name}: Client error {tool_result.status_code} from embedded UI result.",
                }
            elif 500 <= tool_result.status_code < 600:
                tool_result = {
                    "status": "error",
                    "code": "ui_component",
                    "message": f"{tool_function_name}: Server error {tool_result.status_code} from embedded UI result.",
                }
            else:
                tool_result = {
                    "status": "error",
                    "code": "ui_component",
                    "message": f"{tool_function_name}: Unexpected status code {tool_result.status_code} from embedded UI result.",
                }
        else:
            tool_result = tool_result.body.decode("utf-8", "replace")

    elif (tool_type in EXTERNAL_TOOL_TYPES and isinstance(tool_result, tuple)) or (
        direct_tool and isinstance(tool_result, list) and len(tool_result) == 2
    ):
        tool_result, tool_response_headers = tool_result

        try:
            if not isinstance(tool_response_headers, dict):
                tool_response_headers = dict(tool_response_headers)
        except Exception as e:
            tool_response_headers = {}
            log.debug(e)

        if tool_response_headers and isinstance(tool_response_headers, dict):
            content_disposition = tool_response_headers.get(
                "Content-Disposition",
                tool_response_headers.get("content-disposition", ""),
            )

            if "inline" in content_disposition:
                content_type = tool_response_headers.get(
                    "Content-Type",
                    tool_response_headers.get("content-type", ""),
                )
                location = tool_response_headers.get(
                    "Location",
                    tool_response_headers.get("location", ""),
                )

                if "text/html" in content_type:
                    # Display as iframe embed
                    tool_result_embeds.append(tool_result)
                    tool_result = {
                        "status": "success",
                        "code": "ui_component",
                        "message": f"{tool_function_name}: Embedded UI result is active and visible to the user.",
                    }
                elif location:
                    tool_result_embeds.append(location)
                    tool_result = {
                        "status": "success",
                        "code": "ui_component",
                        "message": f"{tool_function_name}: Embedded UI result is active and visible to the user.",
                    }

    tool_result_files = []

    if isinstance(tool_result, list):
        if tool_type == "mcp":  # MCP
            tool_response = []
            for item in tool_result:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if isinstance(text, str):
                            try:
                                text = json.loads(text)
                            except json.JSONDecodeError:
                                pass
                        tool_response.append(text)
                    elif item.get("type") in ["image", "audio"]:
                        file_url = get_file_url_from_base64(
                            request,
                            f"data:{item.get('mimeType')};base64,{item.get('data', item.get('blob', ''))}",
                            {
                                "chat_id": metadata.get("chat_id", None),
                                "message_id": metadata.get("message_id", None),
                                "session_id": metadata.get("session_id", None),
                                "result": item,
                            },
                            user,
                        )

                        tool_result_files.append(
                            {
                                "type": item.get("type", "data"),
                                "url": file_url,
                            }
                        )
            tool_result = tool_response[0] if len(tool_response) == 1 else tool_response
        else:  # OpenAPI
            for item in tool_result:
                if isinstance(item, str) and item.startswith("data:"):
                    tool_result_files.append(
                        {
                            "type": "data",
                            "content": item,
                        }
                    )
                    tool_result.remove(item)

    terminal_generated_files = _collect_terminal_generated_files(
        request,
        tool_function_name,
        tool_result,
        metadata,
        user,
    )
    if terminal_generated_files:
        existing_keys = {
            json.dumps(file_item, sort_keys=True, ensure_ascii=False)
            for file_item in tool_result_files
            if isinstance(file_item, dict)
        }
        for file_item in terminal_generated_files:
            dedupe_key = json.dumps(file_item, sort_keys=True, ensure_ascii=False)
            if dedupe_key in existing_keys:
                continue
            existing_keys.add(dedupe_key)
            tool_result_files.append(file_item)

    if isinstance(tool_result, list):
        tool_result = {"results": tool_result}

    if isinstance(tool_result, dict) or isinstance(tool_result, list):
        tool_result = json.dumps(tool_result, indent=2, ensure_ascii=False)

    # Safety: ensure tool_result is always a string (or None) to prevent
    # downstream TypeError when concatenating (e.g. if an upstream callable
    # returned a tuple that was not unpacked by the branches above).
    if tool_result is not None and not isinstance(tool_result, str):
        if isinstance(tool_result, tuple):
            # execute_tool_server returns (data, headers); unpack the data part
            tool_result = (
                json.dumps(tool_result[0], indent=2, ensure_ascii=False)
                if len(tool_result) > 0
                else ""
            )
        else:
            tool_result = str(tool_result)

    return tool_result, tool_result_files, tool_result_embeds


async def terminal_event_handler(
    tool_function_name: str,
    tool_function_params: dict,
    tool_result,
    event_emitter,
):
    """Emit terminal:* events for Open Terminal tools.

    - display_file  → emits 'terminal:display_file' to open the file preview.
    - write_file / replace_file_content → emits 'terminal:write_file' to refresh.
    - run_command → emits 'terminal:run_command' with cwd to refresh if relevant.
    """
    if not event_emitter:
        return

    if tool_function_name == "display_file":
        path = tool_function_params.get("path", "")
        if not path:
            return
        # Only emit if the file actually exists
        parsed = tool_result
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(parsed, dict) and parsed.get("exists") is False:
            return

        await event_emitter(
            {
                "type": f"terminal:{tool_function_name}",
                "data": {"path": path},
            }
        )
    elif tool_function_name in ("write_file", "replace_file_content"):
        path = tool_function_params.get("path", "")
        if not path:
            return
        await event_emitter(
            {
                "type": f"terminal:{tool_function_name}",
                "data": {"path": path},
            }
        )
    elif tool_function_name == "run_command":
        await event_emitter(
            {
                "type": "terminal:run_command",
                "data": {},
            }
        )


def strip_leading_message_output_before_tool_call(output: list) -> list:
    """
    Remove assistant message items that appear before the last tool call.

    Some tool-using models emit a short planning/progress sentence before the
    next `tool_calls` delta (for example, "我来为您查询..."). That text is
    part of the execution flow and should not remain in the persisted final
    answer once the tool workflow continues.
    """
    last_tool_call_index = next(
        (
            index
            for index, item in reversed(list(enumerate(output)))
            if item.get("type") == "function_call"
        ),
        None,
    )
    if last_tool_call_index is None:
        return output

    if not any(
        item.get("type") == "message"
        for index, item in enumerate(output)
        if index < last_tool_call_index
    ):
        return output

    cleaned_output = []
    for index, item in enumerate(output):
        if item.get("type") == "message" and index < last_tool_call_index:
            continue
        cleaned_output.append(item)

    return cleaned_output


async def chat_completion_tools_handler(
    request: Request, body: dict, extra_params: dict, user: UserModel, models, tools
) -> tuple[dict, dict]:
    async def get_content_from_response(response) -> Optional[str]:
        content = None
        if hasattr(response, "body_iterator"):
            async for chunk in response.body_iterator:
                data = json.loads(chunk.decode("utf-8", "replace"))
                content = data["choices"][0]["message"]["content"]

            # Cleanup any remaining background tasks if necessary
            if response.background is not None:
                await response.background()
        else:
            content = response["choices"][0]["message"]["content"]
        return content

    def get_tools_function_calling_payload(messages, task_model_id, content):
        user_message = get_last_user_message(messages)

        if user_message and messages and messages[-1]["role"] == "user":
            # Remove the last user message to avoid duplication
            messages = messages[:-1]

        recent_messages = messages[-4:] if len(messages) > 4 else messages
        chat_history = "\n".join(
            f"{message['role'].upper()}: \"\"\"{get_content_from_message(message)}\"\"\""
            for message in recent_messages
        )

        prompt = (
            f"History:\n{chat_history}\nQuery: {user_message}"
            if chat_history
            else f"Query: {user_message}"
        )

        return {
            "model": task_model_id,
            "messages": [
                {"role": "system", "content": content},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "metadata": {"task": str(TASKS.FUNCTION_CALLING)},
        }

    event_caller = extra_params["__event_call__"]
    event_emitter = extra_params["__event_emitter__"]
    metadata = extra_params["__metadata__"]

    task_model_id = get_task_model_id(
        body["model"],
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    skip_files = False
    sources = []

    specs = [tool["spec"] for tool in tools.values()]
    tools_specs = json.dumps(specs, ensure_ascii=False)

    if request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE != "":
        template = request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE
    else:
        template = DEFAULT_TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE

    tools_function_calling_prompt = tools_function_calling_generation_template(
        template, tools_specs
    )
    payload = get_tools_function_calling_payload(
        body["messages"], task_model_id, tools_function_calling_prompt
    )

    try:
        response = await generate_chat_completion(request, form_data=payload, user=user)
        log.debug(f"{response=}")
        content = await get_content_from_response(response)
        log.debug(f"{content=}")

        if not content:
            return body, {}

        try:
            content = content[content.find("{") : content.rfind("}") + 1]
            if not content:
                raise Exception("No JSON object found in the response")

            result = json.loads(content)

            async def tool_call_handler(tool_call):
                nonlocal skip_files

                log.debug(f"{tool_call=}")

                tool_function_name = tool_call.get("name", None)
                if tool_function_name not in tools:
                    return body, {}

                tool_function_params = tool_call.get("parameters", {})

                tool = None
                tool_type = ""
                direct_tool = False

                try:
                    tool = tools[tool_function_name]
                    tool_type = tool.get("type", "")
                    direct_tool = tool.get("direct", False)

                    spec = tool.get("spec", {})
                    allowed_params = (
                        spec.get("parameters", {}).get("properties", {}).keys()
                    )
                    tool_function_params = {
                        k: v
                        for k, v in tool_function_params.items()
                        if k in allowed_params
                    }

                    if tool.get("direct", False):
                        tool_result = await event_caller(
                            {
                                "type": "execute:tool",
                                "data": {
                                    "id": str(uuid4()),
                                    "name": tool_function_name,
                                    "params": tool_function_params,
                                    "server": tool.get("server", {}),
                                    "session_id": metadata.get("session_id", None),
                                },
                            }
                        )
                    else:
                        tool_function = tool["callable"]
                        tool_result = await tool_function(**tool_function_params)

                except Exception as e:
                    tool_result = str(e)

                tool_result, tool_result_files, tool_result_embeds = (
                    process_tool_result(
                        request,
                        tool_function_name,
                        tool_result,
                        tool_type,
                        direct_tool,
                        metadata,
                        user,
                    )
                )

                if event_emitter:
                    await terminal_event_handler(
                        tool_function_name,
                        tool_function_params,
                        tool_result,
                        event_emitter,
                    )

                    if tool_result_files:
                        await event_emitter(
                            {
                                "type": "files",
                                "data": {
                                    "files": tool_result_files,
                                },
                            }
                        )

                    if tool_result_embeds:
                        await event_emitter(
                            {
                                "type": "embeds",
                                "data": {
                                    "embeds": tool_result_embeds,
                                },
                            }
                        )

                if tool_result:
                    tool = tools[tool_function_name]
                    tool_id = tool.get("tool_id", "")

                    tool_name = (
                        f"{tool_id}/{tool_function_name}"
                        if tool_id
                        else f"{tool_function_name}"
                    )

                    # Citation is enabled for this tool
                    sources.append(
                        {
                            "source": {
                                "name": (f"{tool_name}"),
                            },
                            "document": [str(tool_result)],
                            "metadata": [
                                {
                                    "source": (f"{tool_name}"),
                                    "parameters": tool_function_params,
                                }
                            ],
                            "tool_result": True,
                        }
                    )

                    if (
                        tools[tool_function_name]
                        .get("metadata", {})
                        .get("file_handler", False)
                    ):
                        skip_files = True

            # check if "tool_calls" in result
            if result.get("tool_calls"):
                for tool_call in result.get("tool_calls"):
                    await tool_call_handler(tool_call)
            else:
                await tool_call_handler(result)

        except Exception as e:
            log.debug(f"Error: {e}")
            content = None
    except Exception as e:
        log.debug(f"Error: {e}")
        content = None

    log.debug(f"tool_contexts: {sources}")

    if skip_files and "files" in body.get("metadata", {}):
        del body["metadata"]["files"]

    return body, {"sources": sources}


async def chat_memory_handler(
    request: Request, form_data: dict, extra_params: dict, user
):
    try:
        results = await query_memory(
            request,
            QueryMemoryForm(
                **{
                    "content": get_last_user_message(form_data["messages"]) or "",
                    "k": 3,
                }
            ),
            user,
        )
    except Exception as e:
        log.debug(e)
        results = None

    user_context = ""
    if results and hasattr(results, "documents"):
        if results.documents and len(results.documents) > 0:
            for doc_idx, doc in enumerate(results.documents[0]):
                created_at_date = "Unknown Date"

                if results.metadatas[0][doc_idx].get("created_at"):
                    created_at_timestamp = results.metadatas[0][doc_idx]["created_at"]
                    created_at_date = time.strftime(
                        "%Y-%m-%d", time.localtime(created_at_timestamp)
                    )

                user_context += f"{doc_idx + 1}. [{created_at_date}] {doc}\n"

    form_data["messages"] = add_or_update_system_message(
        f"User Context:\n{user_context}\n", form_data["messages"], append=True
    )

    return form_data


async def chat_web_search_handler(
    request: Request, form_data: dict, extra_params: dict, user
):
    event_emitter = extra_params["__event_emitter__"]
    await event_emitter(
        {
            "type": "status",
            "data": {
                "action": "web_search",
                "description": "Searching the web",
                "done": False,
            },
        }
    )

    messages = form_data["messages"]
    user_message = get_last_user_message(messages)

    queries = []
    try:
        res = await generate_queries(
            request,
            {
                "model": form_data["model"],
                "messages": messages,
                "prompt": user_message,
                "type": "web_search",
                "chat_id": extra_params.get("__chat_id__"),
            },
            user,
        )

        response = res["choices"][0]["message"]["content"]

        try:
            bracket_start = response.find("{")
            bracket_end = response.rfind("}") + 1

            if bracket_start == -1 or bracket_end == -1:
                raise Exception("No JSON object found in the response")

            response = response[bracket_start:bracket_end]
            queries = json.loads(response)
            queries = queries.get("queries", [])
        except Exception as e:
            queries = [response]

        if ENABLE_QUERIES_CACHE:
            request.state.cached_queries = queries

    except Exception as e:
        log.exception(e)
        queries = [user_message]

    # Check if generated queries are empty
    if len(queries) == 1 and queries[0].strip() == "":
        queries = [user_message]

    # Check if queries are not found
    if len(queries) == 0:
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_search",
                    "description": "No search query generated",
                    "done": True,
                },
            }
        )
        return form_data

    await event_emitter(
        {
            "type": "status",
            "data": {
                "action": "web_search_queries_generated",
                "queries": queries,
                "done": False,
            },
        }
    )

    try:
        results = await process_web_search(
            request,
            SearchForm(queries=queries),
            user=user,
        )

        if results:
            files = form_data.get("files", [])

            if results.get("collection_names"):
                for col_idx, collection_name in enumerate(
                    results.get("collection_names")
                ):
                    files.append(
                        {
                            "collection_name": collection_name,
                            "name": ", ".join(queries),
                            "type": "web_search",
                            "urls": results["filenames"],
                            "queries": queries,
                        }
                    )
            elif results.get("docs"):
                # Invoked when bypass embedding and retrieval is set to True
                docs = results["docs"]
                files.append(
                    {
                        "docs": docs,
                        "name": ", ".join(queries),
                        "type": "web_search",
                        "urls": results["filenames"],
                        "queries": queries,
                    }
                )

            form_data["files"] = files

            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "web_search",
                        "description": "Searched {{count}} sites",
                        "urls": results["filenames"],
                        "items": results.get("items", []),
                        "done": True,
                    },
                }
            )
        else:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "web_search",
                        "description": "No search results found",
                        "done": True,
                        "error": True,
                    },
                }
            )

    except Exception as e:
        log.exception(e)
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_search",
                    "description": "An error occurred while searching the web",
                    "queries": queries,
                    "done": True,
                    "error": True,
                },
            }
        )

    return form_data


def get_images_from_messages(message_list):
    images = []

    for message in reversed(message_list):

        message_images = []
        for file in message.get("files", []):
            if file.get("type") == "image":
                message_images.append(file.get("url"))
            elif file.get("content_type", "").startswith("image/"):
                message_images.append(file.get("url"))

        if message_images:
            images.append(message_images)

    return images


def get_image_urls(delta_images, request, metadata, user) -> list[str]:
    if not isinstance(delta_images, list):
        return []

    image_urls = []
    for img in delta_images:
        if not isinstance(img, dict) or img.get("type") != "image_url":
            continue

        url = img.get("image_url", {}).get("url")
        if not url:
            continue

        if url.startswith("data:image/png;base64"):
            url = get_image_url_from_base64(request, url, metadata, user)

        image_urls.append(url)

    return image_urls


def _normalize_generated_file_entry(item: Any) -> Optional[dict]:
    def normalize_generated_file_ref(value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized or normalized.lower() in {"null", "undefined"}:
            return ""

        knowflow_asset_ref = get_knowflow_asset_ref_key(normalized)
        if knowflow_asset_ref:
            return knowflow_asset_ref

        return normalized

    if isinstance(item, str):
        ref = normalize_generated_file_ref(item)
        if not ref:
            return None
        return {"url": ref}

    if not isinstance(item, dict):
        return None

    candidates = [
        item.get("url"),
        item.get("download_url"),
        item.get("downloadUrl"),
        item.get("id"),
        item.get("file_id"),
        item.get("fileId"),
    ]
    ref = next(
        (
            normalize_generated_file_ref(value)
            for value in candidates
            if isinstance(value, str)
            and normalize_generated_file_ref(value)
        ),
        "",
    )
    inline_content = str(
        item.get("content_base64") or item.get("contentBase64") or ""
    ).strip()
    if not ref and not inline_content:
        return None

    normalized = {"url": ref} if ref else {}

    file_id = item.get("id")
    if isinstance(file_id, str):
        normalized_id = normalize_generated_file_ref(file_id)
        if normalized_id:
            normalized["id"] = normalized_id

    name = item.get("filename") or item.get("fileName") or item.get("name")
    if isinstance(name, str) and name.strip():
        normalized["name"] = name.strip()

    file_type = item.get("type")
    if isinstance(file_type, str) and file_type.strip():
        normalized["type"] = file_type.strip()

    content_type = item.get("content_type") or item.get("contentType")
    if isinstance(content_type, str) and content_type.strip():
        normalized["content_type"] = content_type.strip()

    file_size = item.get("bytes", item.get("size"))
    if isinstance(file_size, (int, float)) and file_size >= 0:
        normalized["size"] = int(file_size)

    call_id = item.get("call_id") or item.get("tool_call_id") or item.get("toolCallId")
    if isinstance(call_id, str) and call_id.strip():
        normalized["call_id"] = call_id.strip()

    if inline_content:
        normalized["content_base64"] = inline_content

    return normalized


_OPENWEBUI_FILE_REF_REGEX = re.compile(
    r"/(?:api/v1|openai/v1|v1)/files/[^/?#]+", flags=re.IGNORECASE
)
_OPENWEBUI_GENERATED_FILE_REF_REGEX = re.compile(
    r"/(?:api/v1|openai/v1|v1)/generated-files/[^/?#]+", flags=re.IGNORECASE
)


def _is_persisted_openwebui_file_entry(item: Any) -> bool:
    normalized = _normalize_generated_file_entry(item)
    if not normalized:
        return False

    url = str(normalized.get("url") or "").strip()
    if not url:
        return False

    return bool(_OPENWEBUI_FILE_REF_REGEX.search(url))


def _is_proxy_generated_file_entry(item: Any) -> bool:
    normalized = _normalize_generated_file_entry(item)
    if not normalized:
        return False

    ref = str(normalized.get("url") or normalized.get("id") or "").strip()
    if not ref:
        return False

    return bool(_OPENWEBUI_GENERATED_FILE_REF_REGEX.search(ref))


def _generated_file_ref_key(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    ref = item.get("url") or item.get("id")
    if isinstance(ref, str):
        normalized_ref = ref.strip()
        if normalized_ref:
            knowflow_asset_ref = get_knowflow_asset_ref_key(normalized_ref)
            if knowflow_asset_ref:
                return f"knowflow:{knowflow_asset_ref}"
            return normalized_ref
    inline_content = item.get("content_base64")
    if isinstance(inline_content, str) and inline_content.strip():
        inline_name = str(item.get("name") or item.get("filename") or "generated-file").strip()
        inline_size = item.get("size")
        return f"inline:{inline_name}:{inline_size}:{len(inline_content.strip())}"
    return ""


def _score_generated_file_ref(value: Any) -> int:
    normalized_ref = str(value or "").strip()
    knowflow_asset_ref = get_knowflow_asset_ref_key(normalized_ref)
    normalized = knowflow_asset_ref or normalized_ref
    if not normalized:
        return 0

    lowered = normalized.lower()
    if "/api/v1/files/" in lowered and "/content" in lowered:
        return 5
    if "/api/v1/files/" in lowered:
        return 4
    if knowflow_asset_ref:
        return 3
    if lowered.startswith(("https://", "http://")):
        return 2
    return 0


def _score_generated_file_label(value: Any) -> int:
    normalized = str(value or "").strip()
    if not normalized:
        return 0

    lowered = normalized.lower()
    if lowered in {"generated-file", "knowledge visual", "knowflow", "image", "file"}:
        return 1
    return len(normalized)


def _merge_duplicate_generated_file_entries(existing: dict, incoming: dict) -> dict:
    merged = {
        **existing,
        **{
            key: value
            for key, value in incoming.items()
            if value not in (None, "", [], {})
        },
    }

    existing_ref = str(existing.get("url") or "").strip()
    incoming_ref = str(incoming.get("url") or "").strip()
    if _score_generated_file_ref(existing_ref) >= _score_generated_file_ref(incoming_ref):
        if existing_ref:
            merged["url"] = existing_ref
    elif incoming_ref:
        merged["url"] = incoming_ref

    existing_id = str(existing.get("id") or "").strip()
    incoming_id = str(incoming.get("id") or "").strip()
    if _score_generated_file_label(existing_id) >= _score_generated_file_label(incoming_id):
        if existing_id:
            merged["id"] = existing_id
    elif incoming_id:
        merged["id"] = incoming_id

    existing_name = str(existing.get("name") or "").strip()
    incoming_name = str(incoming.get("name") or "").strip()
    if _score_generated_file_label(existing_name) >= _score_generated_file_label(incoming_name):
        if existing_name:
            merged["name"] = existing_name
    elif incoming_name:
        merged["name"] = incoming_name

    existing_call_id = str(existing.get("call_id") or "").strip()
    incoming_call_id = str(incoming.get("call_id") or "").strip()
    if existing_call_id and not incoming_call_id:
        merged["call_id"] = existing_call_id

    return merged


def _upload_inline_generated_file(
    request: Request,
    file_item: dict,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> Optional[dict]:
    if user is None or not isinstance(metadata, dict):
        return None

    content_base64 = str(file_item.get("content_base64") or "").strip()
    if not content_base64:
        return None

    filename = str(
        file_item.get("name") or file_item.get("filename") or "generated-file"
    ).strip()
    if not filename:
        filename = "generated-file"

    content_type = str(
        file_item.get("content_type")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    ).strip()

    try:
        content_bytes = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        log.warning("Failed to decode inline generated file %s: %s", filename, exc)
        return None

    upload = UploadFile(
        file=io.BytesIO(content_bytes),
        filename=filename,
        headers={"content-type": content_type},
    )

    file_metadata = {
        "source": "bridge_generated_file",
        "chat_id": metadata.get("chat_id"),
        "message_id": metadata.get("message_id"),
        "session_id": metadata.get("session_id"),
    }
    file_metadata = {
        key: value for key, value in file_metadata.items() if value not in (None, "")
    }

    try:
        uploaded = upload_file_handler(
            request,
            file=upload,
            metadata=file_metadata,
            process=False,
            user=user,
        )
    except Exception as exc:
        log.warning("Failed to upload inline generated file %s: %s", filename, exc)
        return None

    file_id = str(getattr(uploaded, "id", "") or "").strip()
    if not file_id:
        return None

    file_meta = getattr(uploaded, "meta", {}) or {}
    return {
        "id": file_id,
        "url": str(request.app.url_path_for("get_file_content_by_id", id=file_id)),
        "name": str(file_meta.get("name") or getattr(uploaded, "filename", filename)),
        "filename": str(getattr(uploaded, "filename", filename)),
        "type": "image" if content_type.startswith("image/") else "file",
        "content_type": str(file_meta.get("content_type") or content_type),
        "size": file_meta.get("size", len(content_bytes)),
    }


def _materialize_generated_files(
    request: Request,
    files: list[dict],
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> list[dict]:
    materialized: list[dict] = []
    seen_refs: set[str] = set()

    for item in files or []:
        candidate = item
        if isinstance(item, dict) and item.get("content_base64"):
            uploaded = _upload_inline_generated_file(request, item, metadata, user)
            if not uploaded:
                continue
            candidate = uploaded
        elif isinstance(item, dict):
            download_ref = (
                item.get("bridge_download_url")
                or item.get("bridgeDownloadUrl")
                or item.get("download_url")
                or item.get("downloadUrl")
                or item.get("bridge_url")
                or item.get("bridgeUrl")
                or item.get("url")
            )
            download_url = _normalize_bridge_download_url(request, download_ref)
            if download_url and _is_proxy_generated_file_entry(item):
                uploaded = _upload_bridge_downloaded_file(
                    request,
                    str(item.get("tool_name") or "bridge_generated_file"),
                    item,
                    download_url,
                    metadata,
                    user,
                )
                if uploaded:
                    candidate = uploaded
            elif download_url and not _is_openwebui_file_ref(download_ref):
                # Keep bridge-owned generated files on the existing OpenAI proxy path so
                # completion delivery is not blocked by download-and-reupload work.
                proxied_url = _build_openai_proxy_download_url(
                    download_ref if isinstance(download_ref, str) and download_ref.strip() else download_url
                )
                if proxied_url:
                    candidate = {
                        **item,
                        "url": proxied_url,
                        "download_url": proxied_url,
                    }
                else:
                    uploaded = _upload_bridge_downloaded_file(
                        request,
                        str(item.get("tool_name") or "bridge_generated_file"),
                        item,
                        download_url,
                        metadata,
                        user,
                    )
                    if uploaded:
                        candidate = uploaded

        if not isinstance(candidate, dict):
            continue

        if isinstance(item, dict):
            for binding_key in ("call_id", "tool_call_id", "toolCallId"):
                binding_value = item.get(binding_key)
                if isinstance(binding_value, str) and binding_value.strip():
                    candidate.setdefault("call_id", binding_value.strip())
                    break

        ref_key = _generated_file_ref_key(candidate)
        if ref_key and ref_key in seen_refs:
            continue
        if ref_key:
            seen_refs.add(ref_key)
        materialized.append(candidate)

    return materialized


def _merge_generated_file_entries(*groups: Any) -> list[dict]:
    merged_files: list[dict] = []
    index_by_ref: dict[str, int] = {}

    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            normalized = _normalize_generated_file_entry(item)
            if not normalized:
                continue
            ref_key = _generated_file_ref_key(normalized)
            if ref_key and ref_key in index_by_ref:
                existing = merged_files[index_by_ref[ref_key]]
                merged_files[index_by_ref[ref_key]] = _merge_duplicate_generated_file_entries(
                    existing, normalized
                )
                continue
            if ref_key:
                index_by_ref[ref_key] = len(merged_files)
            merged_files.append(normalized)

    return merged_files


def _collect_generated_files_from_output_items(output: Any) -> list[dict]:
    if not isinstance(output, list):
        return []

    collected_files: list[dict] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("files"), list):
            continue
        collected_files.extend(item.get("files"))

    return _merge_generated_file_entries(collected_files)


def _collect_embeds_from_output_items(output: Any) -> list[str]:
    if not isinstance(output, list):
        return []

    collected_embeds: list[Any] = []
    for item in output:
        if not isinstance(item, dict):
            continue

        embeds = item.get("embeds")
        if isinstance(embeds, (list, tuple, str)):
            collected_embeds.append(embeds)

        item_metadata = item.get("metadata")
        if isinstance(item_metadata, dict):
            metadata_embeds = item_metadata.get("embeds")
            if isinstance(metadata_embeds, (list, tuple, str)):
                collected_embeds.append(metadata_embeds)

    return _merge_embed_entries(*collected_embeds)


def _output_contains_proxy_generated_files(output: Any) -> bool:
    return any(
        _is_proxy_generated_file_entry(item)
        for item in _collect_generated_files_from_output_items(output)
    )


def _strip_proxy_generated_files_from_output(output: list) -> list:
    if not isinstance(output, list) or not output:
        return output

    cleaned_output = copy.deepcopy(output)
    changed = False

    for item in cleaned_output:
        if not isinstance(item, dict):
            continue

        files = item.get("files")
        if not isinstance(files, list):
            continue

        filtered_files = [
            file_item for file_item in files if not _is_proxy_generated_file_entry(file_item)
        ]
        if len(filtered_files) == len(files):
            continue

        item["files"] = filtered_files
        changed = True

    return cleaned_output if changed else output


def _stabilize_output_generated_files(
    request: Request,
    output: list,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> tuple[list, dict, list[dict]]:
    final_output, final_payload = _build_chat_completion_payload(output)
    if not output or user is None or not isinstance(metadata, dict):
        return final_output, final_payload, []

    serialized_markup = serialize_output(final_output)
    _updated_content, bridge_generated_files = _collect_bridge_generated_files_from_content(
        request,
        serialized_markup,
        metadata,
        user,
    )

    existing_output_files = _collect_generated_files_from_output_items(final_output)
    if not bridge_generated_files:
        proxy_output_files = [
            item for item in existing_output_files if _is_proxy_generated_file_entry(item)
        ]
        if proxy_output_files:
            bridge_generated_files = _materialize_generated_files(
                request,
                proxy_output_files,
                metadata,
                user,
            )

    if bridge_generated_files:
        final_output = _strip_proxy_generated_files_from_output(final_output)
        existing_output_files = [
            item
            for item in existing_output_files
            if not _is_proxy_generated_file_entry(item)
        ]

    stabilized_files = _merge_generated_file_entries(
        existing_output_files,
        bridge_generated_files,
    )
    if stabilized_files:
        final_output = _attach_generated_files_to_output(final_output, stabilized_files)
        final_output, final_payload = _build_chat_completion_payload(final_output)

    return final_output, final_payload, stabilized_files


def _attach_generated_files_to_output(output: list, files: list[dict]) -> list:
    if not isinstance(output, list) or not output or not isinstance(files, list) or not files:
        return output

    normalized_files = _merge_generated_file_entries(files)
    if not normalized_files:
        return output

    enriched_output = copy.deepcopy(output)
    scoped_files_by_call_id: dict[str, list[dict]] = {}
    unscoped_files: list[dict] = []
    function_call_index_by_call_id: dict[str, int] = {}

    for file_item in normalized_files:
        call_id = str(file_item.get("call_id") or "").strip()
        if call_id:
            scoped_files_by_call_id.setdefault(call_id, []).append(file_item)
        else:
            unscoped_files.append(file_item)

    target_index = None
    for index, item in enumerate(enriched_output):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if call_id:
                function_call_index_by_call_id[call_id] = index
            continue
        if item_type != "function_call_output":
            continue

        target_index = index
        call_id = str(item.get("call_id") or "").strip()
        if not call_id or call_id not in scoped_files_by_call_id:
            continue

        existing_files = item.get("files")
        item["files"] = _merge_generated_file_entries(
            existing_files if isinstance(existing_files, list) else [],
            scoped_files_by_call_id.pop(call_id),
        )
        if str(item.get("status") or "").strip().lower() in {"", "running", "in_progress"}:
            item["status"] = "success"

    synthesized_output_items: list[dict] = []
    for call_id, scoped_files in list(scoped_files_by_call_id.items()):
        function_call_index = function_call_index_by_call_id.get(call_id)
        if function_call_index is None:
            continue

        function_call_item = enriched_output[function_call_index]
        tool_name = str(function_call_item.get("name") or "tool").strip() or "tool"
        function_call_item["status"] = "completed"
        synthesized_output_items.append(
            {
                "type": "function_call_output",
                "id": f"fco_{call_id}",
                "call_id": call_id,
                "name": tool_name,
                "status": "success",
                "files": scoped_files,
                "output": [
                    {
                        "type": "input_text",
                        "text": "工具已完成并生成文件，可在界面中预览或下载。",
                    }
                ],
            }
        )
        scoped_files_by_call_id.pop(call_id, None)

    if synthesized_output_items:
        enriched_output.extend(synthesized_output_items)

    remaining_files = _merge_generated_file_entries(unscoped_files)
    if remaining_files and target_index is not None:
        existing_files = enriched_output[target_index].get("files")
        enriched_output[target_index]["files"] = _merge_generated_file_entries(
            existing_files if isinstance(existing_files, list) else [],
            remaining_files,
        )

    return enriched_output


def _extract_generated_files_from_choices(choices: Any) -> list[dict]:
    if not isinstance(choices, list):
        return []

    generated_files: list[dict] = []
    seen_refs: set[str] = set()

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        metadata_candidates = []
        for key in ("message", "delta"):
            node = choice.get(key)
            if not isinstance(node, dict):
                continue
            metadata = node.get("metadata")
            if isinstance(metadata, dict):
                metadata_candidates.append(metadata)

        for metadata in metadata_candidates:
            for key in ("generated_files", "generatedFiles"):
                items = metadata.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    normalized = _normalize_generated_file_entry(item)
                    if not normalized:
                        continue
                    ref_key = _generated_file_ref_key(normalized)
                    if ref_key and ref_key in seen_refs:
                        continue
                    if ref_key:
                        seen_refs.add(ref_key)
                    generated_files.append(normalized)

    return generated_files


def _normalize_embed_entry(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return normalized


def _merge_embed_entries(*groups: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()

    for group in groups:
        if isinstance(group, str):
            items = [group]
        elif isinstance(group, (list, tuple)):
            items = group
        else:
            continue

        for item in items:
            normalized = _normalize_embed_entry(item)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)

    return merged


_SOURCE_INLINE_IMAGE_SRC_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*(['\"])(?P<url>.+?)\1",
    flags=re.IGNORECASE | re.DOTALL,
)
_SOURCE_INLINE_TABLE_RE = re.compile(
    r"<table\b[\s\S]*?</table>",
    flags=re.IGNORECASE | re.DOTALL,
)
_SOURCE_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\((?P<url>[^)\s]+)",
    flags=re.IGNORECASE,
)
_SOURCE_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)?$"
)
_SOURCE_HTML_TABLE_ROW_RE = re.compile(
    r"<tr\b[^>]*>(?P<row>[\s\S]*?)</tr>",
    flags=re.IGNORECASE | re.DOTALL,
)
_SOURCE_HTML_TABLE_CELL_RE = re.compile(
    r"<(?:td|th)\b[^>]*>(?P<cell>[\s\S]*?)</(?:td|th)>",
    flags=re.IGNORECASE | re.DOTALL,
)
_SOURCE_HTML_BREAK_RE = re.compile(r"<br\s*/?>", flags=re.IGNORECASE)
_SOURCE_HTML_TAG_RE = re.compile(r"<[^>]+>", flags=re.IGNORECASE)
_SOURCE_FENCED_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_VISUAL_REQUEST_HINTS = (
    "图片",
    "配图",
    "附图",
    "原图",
    "图示",
    "流程图",
    "示意图",
    "架构图",
    "表格",
    "图表",
    "image",
    "figure",
    "table",
    "chart",
)


def _text_requests_visual_rendering(text: Any) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    return any(term in normalized for term in _VISUAL_REQUEST_HINTS)


def _normalize_visual_match_text(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _visual_text_overlap_score(reference: Any, candidate: Any) -> float:
    normalized_reference = _normalize_visual_match_text(reference)
    normalized_candidate = _normalize_visual_match_text(candidate)
    if not normalized_reference or not normalized_candidate:
        return 0.0
    if normalized_reference in normalized_candidate:
        return 1.0
    if len(normalized_reference) < 2 or len(normalized_candidate) < 2:
        return 0.0

    reference_ngrams = {
        normalized_reference[index : index + 2]
        for index in range(len(normalized_reference) - 1)
    }
    candidate_ngrams = {
        normalized_candidate[index : index + 2]
        for index in range(len(normalized_candidate) - 1)
    }
    if not reference_ngrams or not candidate_ngrams:
        return 0.0
    return len(reference_ngrams.intersection(candidate_ngrams)) / len(reference_ngrams)


def _resolve_source_visual_url(
    config: Any,
    value: Any,
    *,
    knowflow_source: bool,
) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if knowflow_source or normalized.startswith("/minio/"):
        return resolve_knowflow_asset_url(config, normalized)
    return normalized


def _resolve_source_visual_html(
    config: Any,
    value: Any,
    *,
    knowflow_source: bool,
) -> str:
    html_content = str(value or "").strip()
    if not html_content:
        return ""
    if knowflow_source or "/minio/" in html_content:
        return resolve_knowflow_html_content(config, html_content)
    return html_content


def _extract_source_visual_assets(
    source_item: dict[str, Any],
    document: Any,
    metadata: dict[str, Any],
    config: Any,
) -> tuple[list[dict], list[str]]:
    source_meta = source_item.get("source") if isinstance(source_item, dict) else {}
    source_name = str(
        metadata.get("name")
        or metadata.get("source")
        or (source_meta or {}).get("name")
        or "Knowledge visual"
    ).strip() or "Knowledge visual"
    source_label = str(
        metadata.get("source") or (source_meta or {}).get("name") or ""
    ).strip()
    knowflow_source = source_label.lower() == "knowflow"

    image_files: list[dict] = []
    table_embeds: list[str] = []

    primary_url = _resolve_source_visual_url(
        config,
        metadata.get("url") or metadata.get("embed_url"),
        knowflow_source=knowflow_source,
    )
    if primary_url and is_knowflow_image_ref(primary_url):
        image_files.append(
            {
                "type": "image",
                "url": primary_url,
                "name": source_name,
                "filename": source_name,
            }
        )

    raw_fragments: list[str] = []
    html_content = _resolve_source_visual_html(
        config, metadata.get("html_content"), knowflow_source=knowflow_source
    )
    if html_content:
        raw_fragments.append(html_content)

    document_text = str(document or "").strip()
    if "<" in document_text:
        raw_fragments.append(
            _resolve_source_visual_html(
                config, document_text, knowflow_source=knowflow_source
            )
        )

    for fragment in raw_fragments:
        for match in _SOURCE_INLINE_IMAGE_SRC_RE.finditer(fragment):
            resolved_url = _resolve_source_visual_url(
                config,
                match.group("url"),
                knowflow_source=knowflow_source,
            )
            if not resolved_url or not is_knowflow_image_ref(resolved_url):
                continue
            image_files.append(
                {
                    "type": "image",
                    "url": resolved_url,
                    "name": source_name,
                    "filename": source_name,
                }
            )

        for match in _SOURCE_INLINE_TABLE_RE.finditer(fragment):
            table_html = _resolve_source_visual_html(
                config,
                match.group(0),
                knowflow_source=knowflow_source,
            )
            if table_html:
                table_embeds.append(table_html)

    for match in _SOURCE_MARKDOWN_IMAGE_RE.finditer(document_text):
        resolved_url = _resolve_source_visual_url(
            config,
            match.group("url"),
            knowflow_source=knowflow_source,
        )
        if not resolved_url or not is_knowflow_image_ref(resolved_url):
            continue
        image_files.append(
            {
                "type": "image",
                "url": resolved_url,
                "name": source_name,
                "filename": source_name,
            }
        )

    return _merge_generated_file_entries(image_files), _merge_embed_entries(table_embeds)


def _normalize_visual_image_ref(value: Any) -> str:
    normalized = html.unescape(str(value or "").strip())
    if not normalized:
        return ""

    normalized_entry = _normalize_generated_file_entry({"url": normalized})
    if not normalized_entry:
        return normalized

    ref_key = _generated_file_ref_key(normalized_entry)
    if ref_key:
        return ref_key

    return str(normalized_entry.get("url") or normalized).strip()


def _normalize_visual_table_cell(value: Any) -> str:
    normalized = html.unescape(str(value or ""))
    if not normalized:
        return ""

    normalized = _SOURCE_HTML_BREAK_RE.sub(" ", normalized)
    normalized = _SOURCE_HTML_TAG_RE.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return ""

    return _normalize_visual_match_text(normalized)


def _parse_markdown_table_row(line: str) -> list[str]:
    normalized = str(line or "").strip()
    if not normalized or "|" not in normalized:
        return []

    if normalized.startswith("|"):
        normalized = normalized[1:]
    if normalized.endswith("|"):
        normalized = normalized[:-1]

    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", normalized)]


def _build_visual_table_signature(rows: list[list[str]]) -> str:
    normalized_rows: list[str] = []

    for row in rows:
        if not isinstance(row, list):
            continue

        normalized_cells = [_normalize_visual_table_cell(cell) for cell in row]
        while normalized_cells and not normalized_cells[0]:
            normalized_cells = normalized_cells[1:]
        while normalized_cells and not normalized_cells[-1]:
            normalized_cells = normalized_cells[:-1]
        if not normalized_cells:
            continue

        normalized_rows.append("|".join(normalized_cells))

    return "||".join(normalized_rows)


def _extract_html_table_signature(table_html: Any) -> str:
    normalized = str(table_html or "").strip()
    if not normalized:
        return ""

    rows: list[list[str]] = []
    for row_match in _SOURCE_HTML_TABLE_ROW_RE.finditer(normalized):
        cells = [
            cell_match.group("cell")
            for cell_match in _SOURCE_HTML_TABLE_CELL_RE.finditer(row_match.group("row"))
        ]
        if cells:
            rows.append(cells)

    return _build_visual_table_signature(rows)


def _collect_markdown_table_signatures(document_text: Any) -> set[str]:
    normalized = _SOURCE_FENCED_CODE_BLOCK_RE.sub("", str(document_text or ""))
    if not normalized:
        return set()

    signatures: set[str] = set()
    lines = normalized.splitlines()
    index = 0

    while index + 2 < len(lines):
        header_line = lines[index]
        separator_line = lines[index + 1].strip()
        if "|" not in header_line or not _SOURCE_MARKDOWN_TABLE_SEPARATOR_RE.match(
            separator_line
        ):
            index += 1
            continue

        rows = [_parse_markdown_table_row(header_line)]
        body_row_count = 0
        next_index = index + 2

        while next_index < len(lines):
            candidate_line = lines[next_index]
            candidate_text = candidate_line.strip()
            if (
                not candidate_text
                or candidate_text.startswith("```")
                or "|" not in candidate_line
            ):
                break

            candidate_row = _parse_markdown_table_row(candidate_line)
            if not candidate_row:
                break

            rows.append(candidate_row)
            body_row_count += 1
            next_index += 1

        if body_row_count:
            signature = _build_visual_table_signature(rows)
            if signature:
                signatures.add(signature)
            index = next_index
            continue

        index += 1

    return signatures


def _collect_inline_content_visual_signatures(
    content: Any,
) -> tuple[set[str], set[str]]:
    document_text = _SOURCE_FENCED_CODE_BLOCK_RE.sub("", str(content or ""))
    if not document_text:
        return set(), set()

    image_refs: set[str] = set()
    for match in _SOURCE_MARKDOWN_IMAGE_RE.finditer(document_text):
        ref_key = _normalize_visual_image_ref(match.group("url"))
        if ref_key:
            image_refs.add(ref_key)

    if "<" in document_text:
        for match in _SOURCE_INLINE_IMAGE_SRC_RE.finditer(document_text):
            ref_key = _normalize_visual_image_ref(match.group("url"))
            if ref_key:
                image_refs.add(ref_key)

    table_signatures = _collect_markdown_table_signatures(document_text)
    if "<" in document_text:
        for match in _SOURCE_INLINE_TABLE_RE.finditer(document_text):
            signature = _extract_html_table_signature(match.group(0))
            if signature:
                table_signatures.add(signature)

    return image_refs, table_signatures


def _filter_content_duplicated_retrieval_visuals(
    response_content: Any,
    generated_files: list[dict],
    embeds: list[str],
) -> tuple[list[dict], list[str]]:
    inline_image_refs, inline_table_signatures = (
        _collect_inline_content_visual_signatures(response_content)
    )
    if not inline_image_refs and not inline_table_signatures:
        return (
            _merge_generated_file_entries(generated_files),
            _merge_embed_entries(embeds),
        )

    filtered_files: list[dict] = []
    for item in generated_files:
        normalized_item = _normalize_generated_file_entry(item)
        if not normalized_item:
            continue

        ref_key = _generated_file_ref_key(normalized_item)
        if ref_key and ref_key in inline_image_refs:
            continue

        filtered_files.append(normalized_item)

    filtered_embeds: list[str] = []
    for embed in embeds:
        normalized_embed = str(embed or "").strip()
        if not normalized_embed:
            continue

        signature = _extract_html_table_signature(normalized_embed)
        if signature and signature in inline_table_signatures:
            continue

        filtered_embeds.append(normalized_embed)

    return (
        _merge_generated_file_entries(filtered_files),
        _merge_embed_entries(filtered_embeds),
    )


def _select_retrieval_source_visual_payloads(
    metadata: Optional[dict],
    response_content: Any,
    config: Any,
) -> tuple[list[dict], list[str]]:
    if not isinstance(metadata, dict):
        return [], []

    sources = metadata.get("sources")
    if not isinstance(sources, list) or not sources:
        return [], []

    prompt_text = str(metadata.get("user_prompt") or "").strip()
    if not prompt_text:
        return [], []

    prompt_requests_visuals = _text_requests_visual_rendering(prompt_text)
    response_requests_visuals = _text_requests_visual_rendering(response_content)

    candidates: list[dict[str, Any]] = []
    for source_item in sources:
        if not isinstance(source_item, dict):
            continue

        documents = source_item.get("document")
        metadatas = source_item.get("metadata")
        distances = source_item.get("distances")
        if not isinstance(documents, list):
            continue

        for index, document in enumerate(documents):
            document_metadata = (
                metadatas[index]
                if isinstance(metadatas, list)
                and index < len(metadatas)
                and isinstance(metadatas[index], dict)
                else {}
            )
            image_files, table_embeds = _extract_source_visual_assets(
                source_item,
                document,
                document_metadata,
                config,
            )
            if not image_files and not table_embeds:
                continue

            prompt_overlap = _visual_text_overlap_score(prompt_text, document)
            response_overlap = _visual_text_overlap_score(response_content, document)

            similarity_value = None
            if isinstance(distances, list) and index < len(distances):
                similarity_value = distances[index]
            if similarity_value is None:
                similarity_value = document_metadata.get("similarity")

            try:
                similarity_score = float(similarity_value)
            except (TypeError, ValueError):
                similarity_score = 0.0

            candidates.append(
                {
                    "file_id": str(document_metadata.get("file_id") or "").strip(),
                    "images": image_files,
                    "embeds": table_embeds,
                    "prompt_overlap": prompt_overlap,
                    "response_overlap": response_overlap,
                    "similarity": similarity_score,
                    "total_score": (prompt_overlap * 2.0)
                    + (response_overlap * 0.5)
                    + (max(0.0, min(similarity_score, 1.0)) * 0.05),
                }
            )

    if not candidates:
        return [], []

    candidates.sort(
        key=lambda item: (
            item["total_score"],
            item["prompt_overlap"],
            item["response_overlap"],
            item["similarity"],
            len(item["images"]) + len(item["embeds"]),
        ),
        reverse=True,
    )

    best_candidate = candidates[0]
    if best_candidate["prompt_overlap"] < 0.08:
        return [], []
    if not prompt_requests_visuals and not response_requests_visuals:
        return [], []

    best_file_id = str(best_candidate.get("file_id") or "").strip()
    selected_candidates = (
        [
            item
            for item in candidates
            if str(item.get("file_id") or "").strip() == best_file_id
            and item["prompt_overlap"] >= best_candidate["prompt_overlap"] * 0.5
        ]
        if best_file_id
        else []
    )
    if not selected_candidates:
        selected_candidates = [best_candidate]

    return _filter_content_duplicated_retrieval_visuals(
        response_content,
        _merge_generated_file_entries(
            *[item.get("images") for item in selected_candidates]
        ),
        _merge_embed_entries(*[item.get("embeds") for item in selected_candidates]),
    )


def _extract_embeds_from_choices(choices: Any) -> list[str]:
    if not isinstance(choices, list):
        return []

    embeds: list[str] = []
    seen: set[str] = set()

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        metadata_candidates = []
        for key in ("message", "delta"):
            node = choice.get(key)
            if not isinstance(node, dict):
                continue
            metadata = node.get("metadata")
            if isinstance(metadata, dict):
                metadata_candidates.append(metadata)

        for metadata in metadata_candidates:
            items = metadata.get("embeds")
            if not isinstance(items, list):
                continue
            for item in items:
                normalized = _normalize_embed_entry(item)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                embeds.append(normalized)

    return embeds


def _default_client_capabilities(
    request: Request,
    user: Optional[UserModel],
    metadata: Optional[dict],
) -> dict:
    share_enabled = False
    tool_draft_enabled = False
    skill_draft_enabled = False
    chat_id = ""
    permissions = {}
    if isinstance(metadata, dict):
        chat_id = str(metadata.get("chat_id") or "").strip()

    if user is not None:
        permissions = get_permissions(
            user.id,
            request.app.state.config.USER_PERMISSIONS,
        )

        tool_draft_enabled = bool(
            user.role == "admin"
            or (permissions.get("workspace", {}) or {}).get("tools", False)
        )
        skill_draft_enabled = bool(
            user.role == "admin"
            or (permissions.get("workspace", {}) or {}).get("skills", False)
        )

    if user is not None and chat_id and not chat_id.startswith("local:"):
        share_enabled = bool(
            user.role == "admin" or (permissions.get("chat", {}) or {}).get("share", True)
        )

    return {
        "schema_version": CLIENT_CAPABILITIES_SCHEMA_VERSION,
        "generated_file_download": {
            "enabled": True,
            "delivery": "openwebui_file",
            "storage": "object_storage",
        },
        "chat_share": {
            "enabled": share_enabled,
            "mode": "share_link",
        },
        "file_preview": {
            "enabled": True,
            "mode": "inline_or_modal",
            "types": list(CLIENT_CAPABILITIES_PREVIEW_TYPES),
        },
        "diagram_render": {
            "enabled": True,
            "engines": ["mermaid", "vega", "vega-lite"],
            "surfaces": ["assistant_message"],
            "assistant_message_engines": ["mermaid", "vega", "vega-lite"],
            "file_preview_engines": ["mermaid"],
            "file_extensions": [".md", ".markdown", ".mdx", ".mermaid", ".mmd"],
        },
        "workspace_tool_draft": {
            "enabled": tool_draft_enabled,
            "mode": "confirm_then_edit",
            "format": "python_tool_class",
        },
        "workspace_skill_draft": {
            "enabled": skill_draft_enabled,
            "mode": "confirm_then_edit",
            "format": "markdown_skill",
        },
    }


def _resolve_client_capabilities(
    request: Request,
    user: Optional[UserModel],
    metadata: Optional[dict],
) -> dict:
    defaults = _default_client_capabilities(request, user, metadata)

    incoming = {}
    if isinstance(metadata, dict) and isinstance(metadata.get("client_capabilities"), dict):
        incoming = metadata.get("client_capabilities") or {}

    resolved = deep_update(copy.deepcopy(defaults), incoming)
    resolved["schema_version"] = CLIENT_CAPABILITIES_SCHEMA_VERSION

    for key in (
        "generated_file_download",
        "chat_share",
        "file_preview",
        "diagram_render",
        "workspace_tool_draft",
        "workspace_skill_draft",
    ):
        default_enabled = bool((defaults.get(key) or {}).get("enabled"))
        requested_enabled = bool((resolved.get(key) or {}).get("enabled"))
        resolved.setdefault(key, {})
        resolved[key]["enabled"] = default_enabled and requested_enabled

    if not isinstance((resolved.get("file_preview") or {}).get("types"), list):
        resolved["file_preview"]["types"] = list(CLIENT_CAPABILITIES_PREVIEW_TYPES)
    else:
        sanitized_types = []
        for item in resolved["file_preview"]["types"]:
            if isinstance(item, str) and item.strip():
                sanitized_types.append(item.strip())
        resolved["file_preview"]["types"] = (
            sanitized_types[:24] if sanitized_types else list(CLIENT_CAPABILITIES_PREVIEW_TYPES)
        )

    if isinstance(metadata, dict):
        metadata["client_capabilities"] = resolved

    return resolved


def _build_client_capabilities_system_prompt(client_capabilities: Optional[dict]) -> str:
    if not isinstance(client_capabilities, dict):
        return ""

    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []

        sanitized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = item.strip()
            if normalized and normalized not in sanitized:
                sanitized.append(normalized)

        return sanitized

    generated_file_download = client_capabilities.get("generated_file_download") or {}
    chat_share = client_capabilities.get("chat_share") or {}
    file_preview = client_capabilities.get("file_preview") or {}
    diagram_render = client_capabilities.get("diagram_render") or {}
    workspace_tool_draft = client_capabilities.get("workspace_tool_draft") or {}
    workspace_skill_draft = client_capabilities.get("workspace_skill_draft") or {}

    download_enabled = bool(generated_file_download.get("enabled"))
    share_enabled = bool(chat_share.get("enabled"))
    preview_enabled = bool(file_preview.get("enabled"))
    diagram_enabled = bool(diagram_render.get("enabled"))
    tool_draft_enabled = bool(workspace_tool_draft.get("enabled"))
    skill_draft_enabled = bool(workspace_skill_draft.get("enabled"))
    preview_types = ", ".join(file_preview.get("types") or [])
    diagram_engines_list = _string_list(diagram_render.get("engines"))
    diagram_surfaces_list = _string_list(diagram_render.get("surfaces"))
    diagram_extensions_list = _string_list(diagram_render.get("file_extensions"))
    assistant_message_engines_list = _string_list(
        diagram_render.get("assistant_message_engines")
    )
    if (
        not assistant_message_engines_list
        and "assistant_message" in diagram_surfaces_list
    ):
        assistant_message_engines_list = list(diagram_engines_list)

    file_preview_engines_list = _string_list(diagram_render.get("file_preview_engines"))
    if not file_preview_engines_list and "file_preview" in diagram_surfaces_list:
        file_preview_engines_list = list(diagram_engines_list)
    if not file_preview_engines_list and diagram_extensions_list:
        if any(engine.lower() == "mermaid" for engine in diagram_engines_list):
            file_preview_engines_list = ["mermaid"]

    assistant_message_engines = ", ".join(assistant_message_engines_list)
    file_preview_engines = ", ".join(file_preview_engines_list)
    diagram_extensions = ", ".join(diagram_extensions_list)
    assistant_supports_vega = any(
        engine.lower() in {"vega", "vega-lite"}
        for engine in assistant_message_engines_list
    )
    preview_supports_vega = any(
        engine.lower() in {"vega", "vega-lite"}
        for engine in file_preview_engines_list
    )

    diagram_summary = "不要承诺前端会直接渲染 Mermaid、Vega 或 Vega-Lite 图表。"
    if diagram_enabled and assistant_message_engines:
        diagram_parts = [f"聊天消息代码块可渲染：{assistant_message_engines}。"]
        if file_preview_engines:
            file_preview_suffix = (
                f"（文件扩展名：{diagram_extensions}）" if diagram_extensions else ""
            )
            diagram_parts.append(
                f"文件预览可渲染：{file_preview_engines}{file_preview_suffix}。"
            )
        else:
            diagram_parts.append("当前不要承诺文件预览中的图表渲染能力。")
        if assistant_supports_vega and not preview_supports_vega:
            diagram_parts.append(
                "Vega/Vega-Lite 仅支持聊天消息渲染，不支持文件预览渲染。"
            )
        diagram_summary = " ".join(diagram_parts)

    lines = [
        "以下是当前中电慧语聊天前端的能力边界，请将其视为准确的产品事实。",
        f"- generated_file_download.enabled={'true' if download_enabled else 'false'}：当 assistant 消息已附带标准文件引用时，界面会显示可下载文件链接。",
        f"- chat_share.enabled={'true' if share_enabled else 'false'}：{'当前会话可通过界面分享链接共享' if share_enabled else '当前会话不应向用户承诺可直接分享'}。",
        f"- file_preview.enabled={'true' if preview_enabled else 'false'}：{'界面可预览这些类型：' + preview_types if preview_enabled and preview_types else '不要承诺界面预览能力'}。",
        f"- diagram_render.enabled={'true' if diagram_enabled else 'false'}：{diagram_summary}",
        f"- workspace_tool_draft.enabled={'true' if tool_draft_enabled else 'false'}：{'当用户要求创建新工具时，你可以在回答中给出 1 个完整的工具草稿代码块；界面会提供确认创建入口，确认后再进入工具编辑器。' if tool_draft_enabled else '不要承诺可以在聊天中起草并创建工具'}",
        f"- workspace_skill_draft.enabled={'true' if skill_draft_enabled else 'false'}：{'当用户要求创建新技能时，你可以在回答中给出 1 个完整的技能草稿代码块；界面会提供确认创建入口，确认后再进入技能编辑器。' if skill_draft_enabled else '不要承诺可以在聊天中起草并创建技能'}",
        "回答规则：",
        "1. 不要在 generated_file_download.enabled=true 的情况下说“不能下载”或“只能保存到本地”。",
        "2. 只有在对应 capability enabled=true 时，才能告诉用户界面支持该操作。",
        "3. 不要编造下载地址、分享链接或前端按钮状态；若链接由界面生成，只说明“可在界面直接下载/分享”。",
        "4. 若文件尚未生成或尚未作为标准文件引用返回，应明确说明需要先完成生成或上传步骤。",
        "5. 当 workspace_tool_draft.enabled=true 或 workspace_skill_draft.enabled=true 时，不要把“不能自动静默创建”误说成“完全不能创建”。更准确的说法是：你可以先在聊天中起草，用户确认后再创建到工作区。",
        "6. 不要声称工具或技能已经创建、安装、启用，除非用户已确认创建且创建结果已成功返回。",
        "7. 工具草稿格式：返回 1 个 ```python 代码块，内容必须包含 `class Tools:`；如需元信息，可在代码开头使用三引号 frontmatter，例如 `title:`、`description:`、`requirements:`、`required_open_webui_version:`。",
        "8. 技能草稿格式：返回 1 个 ```markdown 代码块，内容使用 `---` frontmatter，至少包含 `name`，可附带 `description`、`visibility`、`published`、`dependencies`，其后紧跟技能正文。",
        "9. 当用户要求生成、导出、填写、转换或提供 Excel/PDF/DOCX/PPTX/表格模板等可下载文件时，必须先调用对应文档工具；只有本轮消息已经返回标准文件引用时，才能说“已生成”“可下载”或“界面已提供文件”。若本轮没有返回文件引用，必须明确说明文件尚未生成成功，不能假装文件已经在界面中。",
        "10. 当 diagram_render.enabled=true 且用户要结构图（流程、架构、时序、依赖、树等）时，默认输出 ```mermaid 代码块。",
        "11. 当 diagram_render.enabled=true 且用户要定量图（柱状、折线、面积、散点、直方图、饼图等）时，默认输出 ```vega-lite 代码块；仅在确有必要时再用 ```vega。",
        "12. 当 diagram_render.enabled=true 时，可承诺聊天消息支持 Mermaid 与 Vega/Vega-Lite 渲染；但若 capability 未声明 Vega/Vega-Lite 文件预览支持，不要承诺文件预览里可渲染 Vega/Vega-Lite。",
    ]

    return "\n".join(lines)


def _parse_nested_json_value(value: Any) -> Any:
    parsed = value
    for _ in range(3):
        if not isinstance(parsed, str):
            break
        candidate = parsed.strip()
        if not candidate:
            return ""
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return parsed
    return parsed


def _parse_tool_call_attrs(attrs_text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    if not isinstance(attrs_text, str) or not attrs_text:
        return attrs

    for key, value in BRIDGE_TOOL_CALL_ATTR_RE.findall(attrs_text):
        attrs[key] = html.unescape(value)

    return attrs


def _normalize_tool_call_tool_id(attrs: dict[str, str]) -> str:
    for key in ("tool_id", "name"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _tool_call_block_succeeded(attrs: dict[str, str], parsed_result: Any) -> bool:
    done = str(attrs.get("done", "") or "").strip().lower() == "true"
    status = str(attrs.get("status", "") or "").strip().lower()

    if status in {"error", "failed", "timeout"}:
        return False
    if not done and status not in {"success", "completed"}:
        return False

    if isinstance(parsed_result, dict):
        if parsed_result.get("success") is False:
            return False
        result_status = str(parsed_result.get("status", "") or "").strip().lower()
        if result_status in {"error", "failed", "timeout"}:
            return False

    return True


def _extract_bridge_file_path(parsed_args: dict[str, Any]) -> str:
    for key in BRIDGE_FILE_PATH_KEYS:
        value = parsed_args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_bridge_text_content(parsed_args: dict[str, Any]) -> Optional[str]:
    for key in BRIDGE_TEXTUAL_CONTENT_KEYS:
        value = parsed_args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _guess_bridge_generated_file_content_type(filename: str) -> str:
    guessed = mimetypes.guess_type(filename)[0]
    if guessed:
        if guessed.startswith("text/") or guessed in BRIDGE_TEXTUAL_MIME_TYPES:
            return guessed
        return "application/octet-stream"
    return "text/plain"


def _upload_bridge_generated_file(
    request: Request,
    tool_id: str,
    file_path: str,
    file_content: str,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> Optional[dict]:
    if user is None or not isinstance(metadata, dict):
        return None

    filename = os.path.basename(file_path.rstrip("/")) or "generated-file"
    content_bytes = file_content.encode("utf-8")
    content_type = _guess_bridge_generated_file_content_type(filename)

    file = UploadFile(
        file=io.BytesIO(content_bytes),
        filename=filename,
        headers={"content-type": content_type},
    )

    file_metadata = {
        "source": "bridge_tool_call",
        "tool_name": tool_id,
        "bridge_file_path": file_path,
        "chat_id": metadata.get("chat_id"),
        "message_id": metadata.get("message_id"),
        "session_id": metadata.get("session_id"),
    }
    file_metadata = {
        key: value for key, value in file_metadata.items() if value not in (None, "")
    }

    try:
        file_item = upload_file_handler(
            request,
            file=file,
            metadata=file_metadata,
            process=False,
            user=user,
        )
    except Exception as exc:
        log.warning(
            "Failed to upload bridge generated file %s into storage: %s",
            file_path,
            exc,
        )
        return None

    if not file_item:
        return None

    file_meta = getattr(file_item, "meta", {}) or {}
    file_id = str(getattr(file_item, "id", "") or "").strip()
    if not file_id:
        return None

    content_path = request.app.url_path_for("get_file_content_by_id", id=file_id)
    normalized_content_type = (
        str(file_meta.get("content_type") or content_type).strip()
        or "application/octet-stream"
    )

    return {
        "id": file_id,
        "url": str(content_path),
        "name": str(file_meta.get("name") or getattr(file_item, "filename", filename)),
        "filename": str(getattr(file_item, "filename", filename)),
        "type": "image" if normalized_content_type.startswith("image/") else "file",
        "content_type": normalized_content_type,
        "size": file_meta.get("size"),
    }


def _resolve_bridge_api_base_url(request: Request) -> str:
    base_urls = getattr(request.app.state.config, "OPENAI_API_BASE_URLS", []) or []
    if isinstance(base_urls, str):
        base_urls = [base_urls]
    if not isinstance(base_urls, (list, tuple)) or not base_urls:
        return ""
    base_url = str(base_urls[0] or "").strip()
    if base_url.endswith("/"):
        base_url = base_url[:-1]
    return base_url


def _resolve_bridge_api_key(request: Request) -> str:
    api_keys = getattr(request.app.state.config, "OPENAI_API_KEYS", []) or []
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    if not isinstance(api_keys, (list, tuple)) or not api_keys:
        return ""
    return str(api_keys[0] or "").strip()


def _normalize_bridge_download_url(request: Request, value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or candidate.lower() in {"null", "undefined"}:
        return ""

    if candidate.startswith("/openai/"):
        candidate = candidate[len("/openai") :]

    base_url = _resolve_bridge_api_base_url(request)
    if candidate.startswith(("http://", "https://")):
        if base_url and candidate.startswith(base_url):
            return candidate
        return ""

    if not base_url:
        return ""

    if not candidate.startswith("/"):
        candidate = f"/{candidate}"
    if base_url.endswith("/v1") and candidate.startswith("/v1/"):
        return base_url[:-3] + candidate
    return f"{base_url}{candidate}"


def _build_openai_proxy_download_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    candidate = value.strip()
    if not candidate or candidate.lower() in {"null", "undefined"}:
        return ""

    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        candidate = parsed.path or ""
        if parsed.query:
            candidate = f"{candidate}?{parsed.query}"

    if not candidate:
        return ""
    if candidate.startswith("/openai/"):
        return candidate
    if not candidate.startswith("/"):
        candidate = f"/{candidate}"
    return f"/openai{candidate}"


def _is_openwebui_file_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized:
        return False
    if normalized.startswith(
        (
            "/api/v1/files/",
            "/openai/v1/files/",
            "/v1/files/",
            "/api/v1/generated-files/",
            "/openai/v1/generated-files/",
            "/v1/generated-files/",
        )
    ):
        return True
    return bool(
        re.search(
            r"/(?:api/v1|openai/v1|v1)/(?:files|generated-files)/[^/?#]+",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def _download_bridge_generated_file(
    request: Request,
    download_url: str,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> Optional[dict]:
    if not download_url:
        return None

    headers: dict[str, str] = {}
    api_key = _resolve_bridge_api_key(request)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if ENABLE_FORWARD_USER_INFO_HEADERS and user is not None:
        headers = include_user_info_headers(headers, user)
        if isinstance(metadata, dict):
            chat_id = metadata.get("chat_id")
            message_id = metadata.get("message_id")
            if chat_id:
                headers[FORWARD_SESSION_INFO_HEADER_CHAT_ID] = str(chat_id)
            if message_id:
                headers[FORWARD_SESSION_INFO_HEADER_MESSAGE_ID] = str(message_id)

    try:
        response = requests.get(
            download_url,
            headers=headers,
            timeout=(10, 300),
            allow_redirects=False,
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("Failed to download bridge generated file from %s: %s", download_url, exc)
        return None

    filename = _extract_filename_from_content_disposition(
        response.headers.get("Content-Disposition", "")
    ) or os.path.basename(download_url.split("?", 1)[0].rstrip("/"))
    if not filename:
        filename = "generated-file"

    content_type = (
        response.headers.get("Content-Type")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    return {
        "content": response.content,
        "filename": filename,
        "content_type": content_type,
    }


def _find_existing_bridge_uploaded_file(
    request: Request,
    filename: str,
    download_url: str,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> Optional[dict]:
    if user is None or not filename or not download_url or not isinstance(metadata, dict):
        return None

    chat_id = str(metadata.get("chat_id") or "").strip()
    message_id = str(metadata.get("message_id") or "").strip()

    try:
        with SessionLocal() as db:
            existing_files = (
                db.query(File)
                .filter_by(user_id=user.id, filename=filename)
                .order_by(File.created_at.desc())
                .limit(10)
                .all()
            )

            for file_row in existing_files:
                file_meta = getattr(file_row, "meta", {}) or {}
                file_data = file_meta.get("data") or {}

                if str(file_data.get("source") or "").strip() != "bridge_generated_file":
                    continue
                if str(file_data.get("bridge_url") or "").strip() != download_url:
                    continue
                if chat_id and str(file_data.get("chat_id") or "").strip() != chat_id:
                    continue
                if message_id and str(file_data.get("message_id") or "").strip() != message_id:
                    continue

                file_id = str(getattr(file_row, "id", "") or "").strip()
                if not file_id:
                    continue

                content_type = (
                    str(file_meta.get("content_type") or "").strip()
                    or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream"
                )

                return {
                    "id": file_id,
                    "url": str(request.app.url_path_for("get_file_content_by_id", id=file_id)),
                    "name": str(file_meta.get("name") or getattr(file_row, "filename", filename)),
                    "filename": str(getattr(file_row, "filename", filename)),
                    "type": "image" if content_type.startswith("image/") else "file",
                    "content_type": content_type,
                    "size": file_meta.get("size"),
                }
    except Exception as exc:
        log.warning(
            "Failed to look up existing bridge generated file %s: %s",
            download_url,
            exc,
        )

    return None


def _upload_bridge_downloaded_file(
    request: Request,
    tool_id: str,
    candidate: dict,
    download_url: str,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> Optional[dict]:
    if user is None or not isinstance(metadata, dict):
        return None

    filename = str(
        candidate.get("name")
        or candidate.get("filename")
        or os.path.basename(download_url.split("?", 1)[0].rstrip("/"))
        or "generated-file"
    ).strip()
    if not filename:
        filename = "generated-file"

    existing_file = _find_existing_bridge_uploaded_file(
        request,
        filename,
        download_url,
        metadata,
        user,
    )
    if existing_file:
        return existing_file

    download = _download_bridge_generated_file(request, download_url, metadata, user)
    if not download or not isinstance(download.get("content"), (bytes, bytearray)):
        return None

    content_bytes = download["content"]
    if not content_bytes:
        return None

    content_type = str(
        candidate.get("content_type")
        or candidate.get("contentType")
        or download.get("content_type")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    ).strip()

    upload = UploadFile(
        file=io.BytesIO(content_bytes),
        filename=filename,
        headers={"content-type": content_type},
    )

    file_metadata = {
        "source": "bridge_generated_file",
        "tool_name": tool_id,
        "bridge_url": download_url,
        "chat_id": metadata.get("chat_id"),
        "message_id": metadata.get("message_id"),
        "session_id": metadata.get("session_id"),
    }
    file_metadata = {
        key: value for key, value in file_metadata.items() if value not in (None, "")
    }

    try:
        file_item = upload_file_handler(
            request,
            file=upload,
            metadata=file_metadata,
            process=False,
            user=user,
        )
    except Exception as exc:
        log.warning("Failed to upload bridge generated file %s: %s", filename, exc)
        return None

    if not file_item:
        return None

    file_meta = getattr(file_item, "meta", {}) or {}
    file_id = str(getattr(file_item, "id", "") or "").strip()
    if not file_id:
        return None

    content_path = request.app.url_path_for("get_file_content_by_id", id=file_id)
    normalized_content_type = (
        str(file_meta.get("content_type") or content_type).strip()
        or "application/octet-stream"
    )

    return {
        "id": file_id,
        "url": str(content_path),
        "name": str(file_meta.get("name") or getattr(file_item, "filename", filename)),
        "filename": str(getattr(file_item, "filename", filename)),
        "type": "image" if normalized_content_type.startswith("image/") else "file",
        "content_type": normalized_content_type,
        "size": file_meta.get("size", len(content_bytes)),
    }


def _set_tool_call_block_files_attr(block: str, files: list[dict]) -> str:
    if not block or not files:
        return block

    escaped_files = html.escape(json.dumps(files, ensure_ascii=False), quote=True)
    if re.search(r'\sfiles="[^"]*"', block, flags=re.IGNORECASE):
        return re.sub(
            r'\sfiles="[^"]*"',
            f' files="{escaped_files}"',
            block,
            count=1,
            flags=re.IGNORECASE,
        )

    return re.sub(
        r"(<details\b[^>]*)(>)",
        rf'\1 files="{escaped_files}"\2',
        block,
        count=1,
        flags=re.IGNORECASE,
    )


def _extract_bridge_binary_file_candidates(parsed_result: Any) -> list[Any]:
    if not isinstance(parsed_result, dict):
        return []

    candidates: list[Any] = []
    for key in (
        "bridge_download_url",
        "bridgeDownloadUrl",
        "download_url",
        "downloadUrl",
        "bridge_url",
        "bridgeUrl",
    ):
        value = parsed_result.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(parsed_result)
            break

    for key in (
        "bridge_generated_files",
        "bridgeGeneratedFiles",
        "generated_files",
        "generatedFiles",
        "bridge_files",
        "bridgeFiles",
    ):
        items = parsed_result.get(key)
        if isinstance(items, list):
            candidates.extend(items)

    for key in (
        "bridge_generated_file",
        "bridgeGeneratedFile",
        "generated_file",
        "generatedFile",
        "bridge_file",
        "bridgeFile",
    ):
        item = parsed_result.get(key)
        if item is not None:
            candidates.append(item)

    return candidates


def _collect_bridge_binary_generated_files(
    request: Request,
    tool_id: str,
    parsed_result: Any,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> list[dict]:
    if user is None or not isinstance(metadata, dict):
        return []

    candidates = _extract_bridge_binary_file_candidates(parsed_result)
    if not candidates:
        return []

    uploaded_files: list[dict] = []
    for candidate in candidates:
        normalized = _normalize_generated_file_entry(candidate)
        if not normalized:
            continue

        if normalized.get("content_base64"):
            uploaded = _upload_inline_generated_file(request, normalized, metadata, user)
            if uploaded:
                uploaded_files.append(uploaded)
            continue

        download_ref = None
        if isinstance(candidate, dict):
            download_ref = (
                candidate.get("bridge_download_url")
                or candidate.get("bridgeDownloadUrl")
                or candidate.get("download_url")
                or candidate.get("downloadUrl")
                or candidate.get("bridge_url")
                or candidate.get("bridgeUrl")
                or candidate.get("url")
            )
        elif isinstance(candidate, str):
            download_ref = candidate

        if not download_ref and isinstance(normalized.get("url"), str):
            download_ref = normalized.get("url")

        download_url = _normalize_bridge_download_url(request, download_ref)
        if not download_url:
            if _is_openwebui_file_ref(normalized.get("url")):
                uploaded_files.append(normalized)
            continue

        uploaded = _upload_bridge_downloaded_file(
            request,
            tool_id,
            normalized,
            download_url,
            metadata,
            user,
        )
        if uploaded:
            uploaded_files.append(uploaded)

    return uploaded_files


def _collect_bridge_generated_files_from_content(
    request: Request,
    content: str,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> tuple[str, list[dict]]:
    if (
        not isinstance(content, str)
        or 'type="tool_calls"' not in content
        or user is None
        or not isinstance(metadata, dict)
    ):
        return content, []

    generated_files: list[dict] = []

    def replace_block(match: re.Match[str]) -> str:
        block = match.group(0)
        attrs = _parse_tool_call_attrs(match.group("attrs") or "")
        if attrs.get("type") != "tool_calls":
            return block

        existing_files = _parse_nested_json_value(attrs.get("files", ""))
        if isinstance(existing_files, list) and any(
            _is_persisted_openwebui_file_entry(item) for item in existing_files
        ):
            return block

        tool_id = _normalize_tool_call_tool_id(attrs)
        if (
            tool_id not in BRIDGE_GENERATED_FILE_TOOLS
            and tool_id not in BRIDGE_BINARY_GENERATED_FILE_TOOLS
        ):
            return block

        parsed_result = _parse_nested_json_value(attrs.get("result", ""))
        if not _tool_call_block_succeeded(attrs, parsed_result):
            return block

        parsed_args = _parse_nested_json_value(attrs.get("arguments", ""))
        uploaded_files: list[dict] = []

        if tool_id in BRIDGE_GENERATED_FILE_TOOLS and isinstance(parsed_args, dict):
            file_path = _extract_bridge_file_path(parsed_args)
            file_content = _extract_bridge_text_content(parsed_args)
            if file_path and file_content is not None:
                uploaded = _upload_bridge_generated_file(
                    request,
                    tool_id,
                    file_path,
                    file_content,
                    metadata,
                    user,
                )
                if uploaded:
                    uploaded_files.append(uploaded)

        if tool_id in BRIDGE_BINARY_GENERATED_FILE_TOOLS:
            uploaded_files.extend(
                _collect_bridge_binary_generated_files(
                    request,
                    tool_id,
                    parsed_result,
                    metadata,
                    user,
                )
            )

        if not uploaded_files:
            return block

        call_id = str(attrs.get("id") or "").strip()
        if call_id:
            uploaded_files = [
                {
                    **item,
                    "call_id": str(item.get("call_id") or call_id),
                }
                for item in uploaded_files
            ]

        generated_files.extend(uploaded_files)
        return _set_tool_call_block_files_attr(block, uploaded_files)

    updated_content = BRIDGE_TOOL_CALL_BLOCK_RE.sub(replace_block, content)
    return updated_content, generated_files


async def _finalize_bridge_generated_files_from_output(
    request: Request,
    output: list,
    metadata: Optional[dict],
    user: Optional[UserModel],
) -> None:
    if not output or user is None or not isinstance(metadata, dict):
        return

    chat_id = str(metadata.get("chat_id") or "").strip()
    message_id = str(metadata.get("message_id") or "").strip()
    if not chat_id or not message_id:
        return

    try:
        final_output, final_payload, combined_generated_files = await asyncio.to_thread(
            _stabilize_output_generated_files,
            request,
            output,
            metadata,
            user,
        )

        emitted_files = None
        if combined_generated_files:
            message_files = await asyncio.to_thread(
                Chats.add_message_files_by_id_and_message_id,
                chat_id,
                message_id,
                combined_generated_files,
            )
            emitted_files = (
                message_files
                if isinstance(message_files, list) and message_files
                else combined_generated_files
            )
            final_output = _attach_generated_files_to_output(final_output, emitted_files)
            final_output, final_payload = _build_chat_completion_payload(final_output)

        await asyncio.to_thread(
            Chats.upsert_message_to_chat_by_id_and_message_id,
            chat_id,
            message_id,
            {
                "role": "assistant",
                "content": final_payload.get("content", ""),
                "output": final_output,
                **({"files": emitted_files} if isinstance(emitted_files, list) else {}),
            },
        )

        event_emitter = get_event_emitter(metadata, update_db=False)
        if event_emitter and isinstance(emitted_files, list) and emitted_files:
            await event_emitter(
                {
                    "type": "files",
                    "data": {
                        "files": emitted_files,
                    },
                }
            )
            await event_emitter(
                {
                    "type": "chat:completion",
                    "data": {
                        "files": emitted_files,
                        "metadata": {
                            "generated_files": emitted_files,
                        },
                    },
                }
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception(
            "Failed to finalize bridge generated files for chat %s message %s",
            chat_id,
            message_id,
        )


def _resolve_file_context_url(file_item: dict) -> str:
    if not isinstance(file_item, dict):
        return ""

    raw_url = str(file_item.get("url") or "").strip()
    file_id = str(file_item.get("id") or "").strip()

    if raw_url and not raw_url.startswith("data:"):
        if raw_url.startswith(("http://", "https://", "/")):
            return raw_url
        if raw_url.startswith(("api/", "openai/", "v1/")):
            return f"/{raw_url}"
        if "/" in raw_url:
            return f"/{raw_url.lstrip('/')}"
        return f"/api/v1/files/{raw_url}/content"

    if file_id:
        return f"/api/v1/files/{file_id}/content"

    return ""


def _file_context_key(file_item: dict) -> str:
    if not isinstance(file_item, dict):
        return ""

    file_type = str(file_item.get("type", "file") or "file").strip().lower()

    for key in ("id", "url", "collection_name", "name", "filename"):
        value = str(file_item.get(key) or "").strip()
        if value:
            return f"{file_type}:{key}:{value}"

    collection_names = file_item.get("collection_names")
    if isinstance(collection_names, list):
        normalized_names = [str(item).strip() for item in collection_names if str(item).strip()]
        if normalized_names:
            return f"{file_type}:collection_names:{json.dumps(normalized_names, ensure_ascii=False)}"

    return ""


_ADAPTIVE_FOCUS_ACTIVE = "active"
_ADAPTIVE_FOCUS_REFERENCE = "reference"
_ADAPTIVE_FOCUS_CURRENT_TURN = "current_turn"
_ADAPTIVE_FOCUS_HISTORY = "history"
_ADAPTIVE_FOCUS_DERIVED = "derived_context"
_ADAPTIVE_FOCUS_FOLDER = "folder_knowledge"
_ADAPTIVE_FOCUS_MODEL = "model_knowledge"
_ADAPTIVE_FOCUS_METADATA_KEYS = {"focus_origin", "focus_tier"}
_PRIOR_ATTACHMENT_REFERENCE_PATTERN = re.compile(
    r"(previous|earlier|prior|older)\s+(uploaded\s+)?(file|attachment|document|pdf|doc|spreadsheet|sheet)"
    r"|first\s+(uploaded\s+)?(file|attachment|document|pdf|doc|spreadsheet|sheet)"
    r"|other\s+(file|attachment|document)"
    r"|previous\s+upload|earlier\s+upload|prior\s+upload"
    r"|上一个附件|前一个附件|上一个文件|前一个文件|之前的文件|之前的附件|前面的文件|前面的附件"
    r"|先前的文件|之前上传的文件|前面上传的文件|旧文件|历史附件",
    flags=re.IGNORECASE,
)


def _normalize_adaptive_focus_tier(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == _ADAPTIVE_FOCUS_ACTIVE:
        return _ADAPTIVE_FOCUS_ACTIVE
    if normalized == _ADAPTIVE_FOCUS_REFERENCE:
        return _ADAPTIVE_FOCUS_REFERENCE
    return ""


def _adaptive_focus_priority(file_item: Optional[dict]) -> int:
    if not isinstance(file_item, dict):
        return 0

    if str(file_item.get("context") or "").strip().lower() == "full":
        return 3

    tier = _normalize_adaptive_focus_tier(file_item.get("focus_tier"))
    if tier == _ADAPTIVE_FOCUS_ACTIVE:
        return 2
    if tier == _ADAPTIVE_FOCUS_REFERENCE:
        return 1
    return 0


def _is_active_focus_file(file_item: Optional[dict]) -> bool:
    return _adaptive_focus_priority(file_item) >= 2


def _apply_adaptive_focus_metadata(
    file_item: Any,
    *,
    origin: Optional[str] = None,
    tier: Optional[str] = None,
    force: bool = False,
) -> Any:
    if not isinstance(file_item, dict):
        return file_item

    prepared = copy.deepcopy(file_item)

    if origin:
        if force or not str(prepared.get("focus_origin") or "").strip():
            prepared["focus_origin"] = origin

    normalized_tier = _normalize_adaptive_focus_tier(tier)
    if normalized_tier:
        existing_priority = _adaptive_focus_priority(prepared)
        candidate_priority = _adaptive_focus_priority(
            {"focus_tier": normalized_tier, "context": prepared.get("context")}
        )
        if force or candidate_priority > existing_priority or not prepared.get("focus_tier"):
            prepared["focus_tier"] = normalized_tier

    return prepared


def _apply_adaptive_focus_metadata_to_items(
    file_items: Any,
    *,
    origin: Optional[str] = None,
    tier: Optional[str] = None,
    force: bool = False,
) -> list[dict]:
    prepared_items: list[dict] = []

    if not isinstance(file_items, list):
        return prepared_items

    for file_item in file_items:
        if not isinstance(file_item, dict):
            continue
        prepared_items.append(
            _apply_adaptive_focus_metadata(
                file_item,
                origin=origin,
                tier=tier,
                force=force,
            )
        )

    return prepared_items


def _merge_file_context_items(existing: dict, incoming: dict) -> dict:
    merged = deep_update(copy.deepcopy(existing), incoming)

    existing_priority = _adaptive_focus_priority(existing)
    incoming_priority = _adaptive_focus_priority(incoming)

    stronger = incoming if incoming_priority > existing_priority else existing
    stronger_tier = _normalize_adaptive_focus_tier(stronger.get("focus_tier"))
    if stronger_tier:
        merged["focus_tier"] = stronger_tier

    stronger_origin = str(stronger.get("focus_origin") or "").strip()
    if stronger_origin:
        merged["focus_origin"] = stronger_origin
    elif str(merged.get("focus_origin") or "").strip() == "":
        fallback_origin = str(
            existing.get("focus_origin") or incoming.get("focus_origin") or ""
        ).strip()
        if fallback_origin:
            merged["focus_origin"] = fallback_origin

    if (
        str(existing.get("context") or "").strip().lower() == "full"
        or str(incoming.get("context") or "").strip().lower() == "full"
    ):
        merged["context"] = "full"

    return merged


def _file_context_identity_key(file_item: Any) -> str:
    if not isinstance(file_item, dict):
        return ""

    file_key = _file_context_key(file_item)
    if file_key:
        return file_key

    normalized = {
        key: value
        for key, value in file_item.items()
        if key not in _ADAPTIVE_FOCUS_METADATA_KEYS
    }
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False)


def _dedupe_file_context_items(file_items: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    indexes: dict[str, int] = {}

    for file_item in file_items:
        if not isinstance(file_item, dict):
            continue

        identity_key = _file_context_identity_key(file_item)
        if not identity_key:
            deduped.append(file_item)
            continue

        if identity_key not in indexes:
            indexes[identity_key] = len(deduped)
            deduped.append(file_item)
            continue

        current_index = indexes[identity_key]
        deduped[current_index] = _merge_file_context_items(
            deduped[current_index], file_item
        )

    return deduped


def _split_files_by_focus(file_items: list[dict]) -> tuple[list[dict], list[dict]]:
    active_files: list[dict] = []
    reference_files: list[dict] = []

    for file_item in file_items:
        if _is_active_focus_file(file_item):
            active_files.append(file_item)
        else:
            reference_files.append(file_item)

    return active_files, reference_files


def _normalize_focus_match_text(value: Any) -> str:
    normalized = os.path.basename(str(value or "").strip().lower())
    normalized = re.sub(r"[\\/_\-.]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _has_specific_focus_label(label: str) -> bool:
    if not label:
        return False
    if re.search(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]", label):
        return len(label) >= 2
    return len(label) >= 3


def _get_reference_label_variants(file_item: dict) -> set[str]:
    labels: set[str] = set()

    for key in ("name", "filename", "collection_name"):
        value = str(file_item.get(key) or "").strip()
        if value:
            labels.add(value)

    collection_names = file_item.get("collection_names")
    if isinstance(collection_names, list):
        for value in collection_names:
            normalized_value = str(value or "").strip()
            if normalized_value:
                labels.add(normalized_value)

    variants: set[str] = set()
    for label in labels:
        normalized = _normalize_focus_match_text(label)
        if _has_specific_focus_label(normalized):
            variants.add(normalized)

        stem = _normalize_focus_match_text(os.path.splitext(label)[0])
        if _has_specific_focus_label(stem):
            variants.add(stem)

    return variants


def _prompt_mentions_reference_file(prompt: str, file_item: dict) -> bool:
    normalized_prompt = _normalize_focus_match_text(prompt)
    if not normalized_prompt:
        return False

    return any(
        variant and variant in normalized_prompt
        for variant in _get_reference_label_variants(file_item)
    )


def _prompt_requests_prior_attachments(prompt: str) -> bool:
    return bool(_PRIOR_ATTACHMENT_REFERENCE_PATTERN.search(str(prompt or "").strip().lower()))


def _has_usable_sources(sources: list[dict]) -> bool:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for document in source.get("document") or []:
            if str(document or "").strip():
                return True
    return False


def _safe_retrieval_diagnostic(
    *,
    classification: str,
    reason: str,
    candidate_index: int,
    chunk_total: int = 0,
    usable_chunk_total: int = 0,
    outcome: Optional[str] = None,
    source_id: Optional[str] = None,
) -> dict[str, Any]:
    diagnostic = {
        "kind": "retrieval_quality",
        "classification": classification,
        "reason": reason,
        "candidate_index": candidate_index,
        "chunk_total": chunk_total,
        "usable_chunk_total": usable_chunk_total,
    }
    if outcome:
        diagnostic["outcome"] = outcome
    if source_id:
        diagnostic["source_id"] = source_id
    return diagnostic


def _append_retrieval_diagnostic_once(
    diagnostics: list[dict[str, Any]], diagnostic: dict[str, Any]
) -> None:
    if diagnostic not in diagnostics:
        diagnostics.append(diagnostic)


def _source_has_explicit_conflict_marker(source: dict) -> bool:
    markers = [
        source.get("classification"),
        source.get("evidence_type"),
        source.get("retrieval_classification"),
    ]

    metadatas = source.get("metadata")
    if isinstance(metadatas, list):
        for item in metadatas:
            if not isinstance(item, dict):
                continue
            if item.get("conflict") is True:
                return True
            markers.extend(
                [
                    item.get("classification"),
                    item.get("evidence_type"),
                    item.get("retrieval_classification"),
                ]
            )

    return any(str(marker or "").strip().lower() == "conflict" for marker in markers)


def _metadata_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _metadata_falsey(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "off"}
    return False


def _source_identifier_values(source: dict, metadata: Optional[dict] = None) -> set[str]:
    values: set[str] = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            values.add(text)
            values.add(_normalize_focus_match_text(text))
            values.add(_normalize_anchor_text(text))

    source_info = source.get("source")
    if isinstance(source_info, dict):
        for key in (
            "id",
            "file_id",
            "collection_id",
            "knowledge_id",
            "name",
            "filename",
            "title",
            "source",
        ):
            add(source_info.get(key))

    if isinstance(metadata, dict):
        for key in (
            "id",
            "file_id",
            "collection_id",
            "knowledge_id",
            "name",
            "filename",
            "title",
            "source",
        ):
            add(metadata.get(key))

    return {value for value in values if value}


def _source_id_for_diagnostic(source: dict, metadata: Optional[dict] = None) -> str:
    source_info = source.get("source")
    if isinstance(source_info, dict):
        for key in ("id", "file_id", "collection_id", "knowledge_id", "name"):
            value = str(source_info.get(key) or "").strip()
            if value:
                return value
    if isinstance(metadata, dict):
        for key in ("file_id", "collection_id", "knowledge_id", "id", "source", "name"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
    return ""


def _source_fits_active_scope(source: dict, active_source_scope: Optional[dict]) -> bool:
    if not isinstance(active_source_scope, dict):
        return True

    status = str(active_source_scope.get("status") or "").strip().lower()
    source_set_mode = str(
        active_source_scope.get("source_set_mode") or ""
    ).strip().lower()
    if status != "resolved" or source_set_mode not in {"single", "multi"}:
        return True

    scope_values = _active_source_scope_values(active_source_scope)
    if not scope_values:
        return True

    documents = source.get("document") or []
    metadatas = source.get("metadata") or []
    source_values = _source_identifier_values(source)
    for metadata in metadatas if isinstance(metadatas, list) else []:
        if isinstance(metadata, dict):
            source_values.update(_source_identifier_values(source, metadata))

    return bool(source_values.intersection(scope_values))


def _metadata_indicates_status(metadata: dict, source: dict, statuses: set[str]) -> bool:
    markers: list[Any] = []
    for container in (source, metadata):
        markers.extend(
            [
                container.get("status"),
                container.get("retrieval_status"),
                container.get("retrieval_outcome"),
                container.get("outcome"),
                container.get("classification"),
                container.get("evidence_type"),
                container.get("retrieval_classification"),
                container.get("reason"),
            ]
        )
    return any(str(marker or "").strip().lower() in statuses for marker in markers)


def _rejection_for_retrieval_chunk(
    *,
    source: dict,
    metadata: dict,
    distance: Any = None,
    relevance_threshold: Optional[float] = None,
) -> Optional[tuple[str, str, str]]:
    if _metadata_indicates_status(metadata, source, {"timeout"}):
        return ("timeout", "retrieval_timeout", "diagnostics")
    if _metadata_indicates_status(metadata, source, {"malformed"}):
        return ("malformed", "malformed_candidate", "diagnostics")

    unauthorized_markers = (
        metadata.get("authorized"),
        metadata.get("is_authorized"),
        metadata.get("has_access"),
        metadata.get("permission_allowed"),
        source.get("authorized"),
        source.get("is_authorized"),
        source.get("has_access"),
        source.get("permission_allowed"),
    )
    if any(_metadata_falsey(value) for value in unauthorized_markers) or any(
        _metadata_truthy(value)
        for value in (
            metadata.get("unauthorized"),
            metadata.get("permission_denied"),
            metadata.get("access_denied"),
            source.get("unauthorized"),
            source.get("permission_denied"),
            source.get("access_denied"),
        )
    ):
        return ("unauthorized", "source_unauthorized", "diagnostics")

    if _metadata_indicates_status(metadata, source, {"stale", "expired"}):
        return ("stale", "stale_or_expired_source", "diagnostics")
    if any(
        _metadata_truthy(value)
        for value in (
            metadata.get("stale"),
            metadata.get("expired"),
            source.get("stale"),
            source.get("expired"),
        )
    ) or any(
        _metadata_falsey(value)
        for value in (
            metadata.get("current"),
            metadata.get("is_current"),
            metadata.get("version_current"),
            source.get("current"),
            source.get("is_current"),
            source.get("version_current"),
        )
    ):
        return ("stale", "stale_or_expired_source", "diagnostics")

    if _source_has_explicit_conflict_marker({**source, "metadata": [metadata]}):
        return ("conflict", "explicit_conflict_marker", "conflict")

    if _metadata_indicates_status(
        metadata,
        source,
        {"no_evidence", "empty", "empty_kb", "no_results"},
    ):
        return ("empty", "no_evidence", "no_evidence")

    weak_markers = (
        metadata.get("answerable"),
        metadata.get("is_answerable"),
        metadata.get("direct"),
        metadata.get("direct_match"),
        metadata.get("source_scope_fit"),
        source.get("answerable"),
        source.get("is_answerable"),
        source.get("direct"),
        source.get("direct_match"),
        source.get("source_scope_fit"),
    )
    if any(_metadata_falsey(value) for value in weak_markers):
        return ("weak_evidence", "weak_or_indirect_evidence", "no_evidence")

    if _metadata_indicates_status(
        metadata,
        source,
        {"weak", "weak_evidence", "low_relevance", "low_confidence"},
    ):
        return ("weak_evidence", "weak_or_indirect_evidence", "no_evidence")

    if relevance_threshold is not None and distance is not None:
        try:
            if float(distance) < float(relevance_threshold):
                return ("weak_evidence", "weak_relevance_score", "no_evidence")
        except (TypeError, ValueError):
            pass

    return None


def _mark_source_injectable(source: dict) -> dict:
    marked = copy.deepcopy(source)
    marked["retrieval_outcome"] = "success"
    marked["retrieval_classification"] = "injectable"
    metadatas = marked.get("metadata")
    if isinstance(metadatas, list):
        for metadata in metadatas:
            if isinstance(metadata, dict):
                metadata.setdefault("retrieval_outcome", "success")
                metadata.setdefault("retrieval_classification", "injectable")
    return marked


def _filter_source_to_usable_documents(source: dict, usable_indexes: list[int]) -> dict:
    filtered = copy.deepcopy(source)
    documents = source.get("document") or []
    filtered["document"] = [documents[index] for index in usable_indexes]

    for key, value in source.items():
        if key == "document":
            continue
        if isinstance(value, list) and len(value) == len(documents):
            filtered[key] = [value[index] for index in usable_indexes]

    metadatas = filtered.get("metadata")
    if not isinstance(metadatas, list):
        filtered["metadata"] = [{} for _ in filtered["document"]]
    elif len(metadatas) < len(filtered["document"]):
        filtered["metadata"] = [
            *metadatas,
            *({} for _ in range(len(filtered["document"]) - len(metadatas))),
        ]

    return filtered


def _gate_retrieval_sources(
    candidates: list,
    *,
    active_source_scope: Optional[dict] = None,
    relevance_threshold: Optional[float] = None,
) -> tuple[list[dict], list[dict[str, Any]]]:
    injectable_sources: list[dict] = []
    diagnostics: list[dict[str, Any]] = []

    for candidate_index, source in enumerate(candidates or []):
        if not isinstance(source, dict):
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="diagnostics",
                    reason="malformed_candidate",
                    candidate_index=candidate_index,
                    outcome="malformed",
                )
            )
            continue

        if not source:
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="diagnostics",
                    reason="empty_candidate",
                    candidate_index=candidate_index,
                    outcome="empty",
                )
            )
            continue

        source_id = _source_id_for_diagnostic(source)
        if not _source_fits_active_scope(source, active_source_scope):
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="diagnostics",
                    reason="source_scope_mismatch",
                    candidate_index=candidate_index,
                    outcome="unauthorized",
                    source_id=source_id or None,
                )
            )
            continue

        documents = source.get("document")
        if not isinstance(documents, list) or not documents:
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="no_evidence",
                    reason="missing_document_body",
                    candidate_index=candidate_index,
                    outcome="empty",
                    source_id=source_id or None,
                )
            )
            continue

        usable_indexes = [
            index
            for index, document in enumerate(documents)
            if str(document or "").strip()
        ]

        if not usable_indexes:
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="no_evidence",
                    reason="blank_document_body",
                    candidate_index=candidate_index,
                    chunk_total=len(documents),
                    outcome="empty",
                    source_id=source_id or None,
                )
            )
            continue

        if _source_has_explicit_conflict_marker(source):
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="conflict",
                    reason="explicit_conflict_marker",
                    candidate_index=candidate_index,
                    chunk_total=len(documents),
                    usable_chunk_total=len(usable_indexes),
                    outcome="conflict",
                    source_id=source_id or None,
                )
            )
            continue

        metadatas = source.get("metadata") or []
        distances = source.get("distances") or []
        accepted_indexes: list[int] = []
        for index in usable_indexes:
            metadata = metadatas[index] if index < len(metadatas) else {}
            if not isinstance(metadata, dict):
                metadata = {}
            distance = distances[index] if index < len(distances) else None
            rejection = _rejection_for_retrieval_chunk(
                source=source,
                metadata=metadata,
                distance=distance,
                relevance_threshold=relevance_threshold,
            )
            if rejection:
                outcome, reason, classification = rejection
                diagnostics.append(
                    _safe_retrieval_diagnostic(
                        classification=classification,
                        reason=reason,
                        candidate_index=candidate_index,
                        chunk_total=len(documents),
                        usable_chunk_total=len(usable_indexes),
                        outcome=outcome,
                        source_id=_source_id_for_diagnostic(source, metadata) or None,
                    )
                )
                continue
            accepted_indexes.append(index)

        if not accepted_indexes:
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="no_evidence",
                    reason="no_injectable_evidence",
                    candidate_index=candidate_index,
                    chunk_total=len(documents),
                    usable_chunk_total=0,
                    outcome="empty",
                    source_id=source_id or None,
                )
            )
            continue

        if len(usable_indexes) != len(documents):
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="diagnostics",
                    reason="partial_blank_document_body",
                    candidate_index=candidate_index,
                    chunk_total=len(documents),
                    usable_chunk_total=len(usable_indexes),
                    outcome="malformed",
                    source_id=source_id or None,
                )
            )

        injectable_sources.append(
            _mark_source_injectable(
                _filter_source_to_usable_documents(source, accepted_indexes)
            )
        )

    if candidates and not injectable_sources:
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="no_evidence",
                reason="no_injectable_evidence",
                candidate_index=-1,
                outcome="empty",
            )
        )

    observe_llm_event(
        "retrieval.quality_gate",
        {
            "retrieval_round": "first_pass",
            "retriever_class": "open_webui_context",
            "candidate_count": len(candidates or []),
            "accepted_count": len(injectable_sources),
            "rejected_count": max(0, len(candidates or []) - len(injectable_sources)),
            "diagnostic_count": len(diagnostics),
            "reason_codes": diagnostic_reason_codes(diagnostics),
            "classification_counts": diagnostic_classification_counts(diagnostics),
        },
    )

    return injectable_sources, diagnostics


_NEUTRAL_RETRIEVAL_CAPABILITIES = {
    "literal_anchor",
    "metadata_first",
    "keyword_bm25",
    "dense_semantic",
    "hybrid",
    "query_expansion",
    "rerank",
    "diversity_mmr",
    "parent_child",
    "freshness",
    "graph/wiki",
}


def _list_from_optional_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _retrieval_output_candidates(output: Any) -> list:
    if isinstance(output, list):
        return output
    if not isinstance(output, dict):
        return []
    for key in ("candidates", "sources", "results", "documents", "data"):
        value = output.get(key)
        if isinstance(value, list):
            return value
    return []


def _neutral_strategy_values(output: Any, candidates: list) -> tuple[list[str], list[str]]:
    raw_values: list[Any] = []
    if isinstance(output, dict):
        raw_values.extend(
            [
                output.get("strategy_used"),
                output.get("strategy"),
                output.get("strategies"),
                output.get("capabilities"),
                output.get("capabilities_used"),
            ]
        )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_values.extend(
            [
                candidate.get("strategy_used"),
                candidate.get("strategy"),
                candidate.get("capabilities"),
                candidate.get("capabilities_used"),
            ]
        )
        metadatas = candidate.get("metadata")
        for metadata in metadatas if isinstance(metadatas, list) else []:
            if isinstance(metadata, dict):
                raw_values.extend(
                    [
                        metadata.get("strategy_used"),
                        metadata.get("strategy"),
                        metadata.get("capabilities"),
                        metadata.get("capabilities_used"),
                    ]
                )

    strategy_values: list[str] = []
    for raw_value in raw_values:
        for value in _list_from_optional_strings(raw_value):
            normalized = value.strip()
            if normalized and normalized not in strategy_values:
                strategy_values.append(normalized)

    capabilities = [
        value for value in strategy_values if value in _NEUTRAL_RETRIEVAL_CAPABILITIES
    ]
    strategies = [
        value for value in strategy_values if value not in _NEUTRAL_RETRIEVAL_CAPABILITIES
    ]
    return strategies, capabilities


def _provider_local_scores(candidates: list) -> list[dict[str, Any]]:
    score_keys = (
        "score",
        "distance",
        "similarity",
        "relevance",
        "rank",
        "vector_score",
        "keyword_score",
        "bm25_score",
        "hybrid_score",
        "rerank_score",
        "mmr_score",
    )
    collected: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        metadatas = candidate.get("metadata")
        metadata_items = metadatas if isinstance(metadatas, list) else [{}]
        distances = candidate.get("distances") if isinstance(candidate.get("distances"), list) else []
        for chunk_index, metadata in enumerate(metadata_items):
            metadata = metadata if isinstance(metadata, dict) else {}
            scores: dict[str, Any] = {}
            for key in score_keys:
                value = metadata.get(key, candidate.get(key))
                if value not in (None, "", [], {}):
                    scores[key] = value
            if chunk_index < len(distances) and "distance" not in scores:
                scores["distance"] = distances[chunk_index]
            if scores:
                collected.append(
                    {
                        "candidate_index": candidate_index,
                        "chunk_index": chunk_index,
                        "source_id": _source_id_for_diagnostic(candidate, metadata) or None,
                        "scores": scores,
                        "semantics": "provider_local_advisory",
                    }
                )
    return collected


def _freshness_index_state(candidates: list) -> list[dict[str, Any]]:
    freshness_keys = (
        "freshness",
        "index_state",
        "indexed_at",
        "updated_at",
        "created_at",
        "version",
        "version_current",
        "connector_freshness",
        "stale",
        "expired",
    )
    states: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        source_info = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
        metadatas = candidate.get("metadata")
        metadata_items = metadatas if isinstance(metadatas, list) else [{}]
        for chunk_index, metadata in enumerate(metadata_items):
            metadata = metadata if isinstance(metadata, dict) else {}
            state = {
                key: metadata.get(key, candidate.get(key, source_info.get(key)))
                for key in freshness_keys
                if metadata.get(key, candidate.get(key, source_info.get(key))) not in (None, "", [], {})
            }
            if state:
                states.append(
                    {
                        "candidate_index": candidate_index,
                        "chunk_index": chunk_index,
                        "source_id": _source_id_for_diagnostic(candidate, metadata) or None,
                        "state": state,
                    }
                )
    return states


def _authorization_outcomes(candidates: list) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        metadatas = candidate.get("metadata")
        metadata_items = metadatas if isinstance(metadatas, list) else [{}]
        for chunk_index, metadata in enumerate(metadata_items):
            metadata = metadata if isinstance(metadata, dict) else {}
            denied = any(
                _metadata_falsey(value)
                for value in (
                    metadata.get("authorized"),
                    metadata.get("has_access"),
                    metadata.get("permission_allowed"),
                    candidate.get("authorized"),
                    candidate.get("has_access"),
                    candidate.get("permission_allowed"),
                )
            ) or any(
                _metadata_truthy(value)
                for value in (
                    metadata.get("unauthorized"),
                    metadata.get("permission_denied"),
                    candidate.get("unauthorized"),
                    candidate.get("permission_denied"),
                )
            )
            explicit_allowed = any(
                _metadata_truthy(value)
                for value in (
                    metadata.get("authorized"),
                    metadata.get("has_access"),
                    metadata.get("permission_allowed"),
                    candidate.get("authorized"),
                    candidate.get("has_access"),
                    candidate.get("permission_allowed"),
                )
            )
            if denied or explicit_allowed:
                outcomes.append(
                    {
                        "candidate_index": candidate_index,
                        "chunk_index": chunk_index,
                        "source_id": _source_id_for_diagnostic(candidate, metadata) or None,
                        "outcome": "permission_denied" if denied else "authorized",
                    }
                )
    return outcomes


def _retrieval_output_has_provider_answer_prose(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    for key in ("answer", "answer_text", "response", "summary", "provider_answer"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _compare_knowflow_retrieval_against_neutral_contract(
    retrieval_output: Any,
    *,
    query: str,
    active_source_scope: Optional[dict] = None,
    evidence_need: str = "internal_selected_source",
    exact_anchors: Optional[list[str]] = None,
    filters: Optional[dict] = None,
    strategy_preferences: Optional[list[str]] = None,
    count: Optional[int] = None,
    retrieval_round: Any = "first_pass",
    relevance_threshold: Optional[float] = None,
) -> dict[str, Any]:
    """Compare current Knowflow-shaped retrieval output through the neutral contract.

    The result is diagnostic/evaluation metadata only. It never makes WeKnora a
    dependency and never treats provider answer prose as citation evidence.
    """

    output_dict = retrieval_output if isinstance(retrieval_output, dict) else {}
    candidates = _retrieval_output_candidates(retrieval_output)
    provider = str(
        output_dict.get("provider")
        or output_dict.get("retriever")
        or output_dict.get("backend")
        or "knowflow"
    ).strip() or "knowflow"

    injectable_sources, diagnostics = _gate_retrieval_sources(
        candidates,
        active_source_scope=active_source_scope,
        relevance_threshold=relevance_threshold,
    )
    if _retrieval_output_has_provider_answer_prose(retrieval_output):
        diagnostics = [
            *diagnostics,
            _safe_retrieval_diagnostic(
                classification="diagnostics",
                reason="provider_answer_prose_ignored",
                candidate_index=-1,
                outcome="malformed",
            ),
        ]

    sidecar = Chats.build_reference_metadata_sidecar(
        sources=injectable_sources,
        diagnostics=diagnostics,
    )
    strategies, capabilities = _neutral_strategy_values(retrieval_output, candidates)
    requested_capabilities = [
        value
        for value in _list_from_optional_strings(strategy_preferences)
        if value in _NEUTRAL_RETRIEVAL_CAPABILITIES
    ]
    if exact_anchors:
        requested_capabilities.append("literal_anchor")
    if filters:
        requested_capabilities.append("metadata_first")
    requested_capabilities = list(dict.fromkeys(requested_capabilities))

    response_status = "success" if injectable_sources else "diagnostic"
    if injectable_sources and diagnostics:
        response_status = "mixed"

    comparison = {
        "kind": "retrieval_contract_comparison",
        "subject_provider": provider,
        "reference_mechanics": "weknora_style",
        "reference_dependency": "none",
        "runtime_authority": "diagnostic_only",
        "neutral_request": {
            "query": str(query or "").strip(),
            "source_scope": active_source_scope or {},
            "evidence_need": str(evidence_need or "").strip(),
            "exact_anchors": _list_from_optional_strings(exact_anchors),
            "filters": copy.deepcopy(filters or {}),
            "strategy_preferences": _list_from_optional_strings(strategy_preferences),
            "count": count,
            "retrieval_round": retrieval_round,
        },
        "neutral_response": {
            "status": response_status,
            "provider": provider,
            "strategy_used": strategies,
            "capabilities_requested": requested_capabilities,
            "capabilities_observed": capabilities,
            "candidate_count": len(candidates),
            "accepted_count": len(injectable_sources),
            "canonical_references": sidecar.get("canonical_references", []),
            "diagnostics": sidecar.get("retrieval_diagnostics", []),
            "freshness_index_state": _freshness_index_state(candidates),
            "provider_local_scores": _provider_local_scores(candidates),
            "authorization_outcomes": _authorization_outcomes(candidates),
        },
        "quality_gate": {
            "accepted_count": len(injectable_sources),
            "diagnostic_count": len(diagnostics),
            "reason_codes": diagnostic_reason_codes(diagnostics),
            "classification_counts": diagnostic_classification_counts(diagnostics),
        },
        "retrieval_stage_trace": [
            {
                "stage": "neutral_request",
                "status": "captured",
                "fields": [
                    "query",
                    "source_scope",
                    "evidence_need",
                    "exact_anchors",
                    "filters",
                    "strategy_preferences",
                    "count",
                    "retrieval_round",
                ],
            },
            {
                "stage": "provider_recall",
                "status": "observed",
                "provider": provider,
                "candidate_count": len(candidates),
                "capabilities_observed": capabilities,
            },
            {
                "stage": "quality_gate",
                "status": response_status,
                "accepted_count": len(injectable_sources),
                "diagnostic_count": len(diagnostics),
            },
            {
                "stage": "canonicalization",
                "status": "diagnostic_only"
                if not sidecar.get("canonical_references")
                else "canonical_references_ready",
                "canonical_reference_count": len(
                    sidecar.get("canonical_references", [])
                ),
                "diagnostic_count": len(sidecar.get("retrieval_diagnostics", [])),
            },
        ],
        "notes": [
            "comparison_only_not_runtime_authority",
            "weknora_not_runtime_dependency",
            "provider_answer_prose_not_citation",
        ],
    }
    observe_llm_event(
        "retrieval.neutral_contract_comparison",
        {
            "provider": provider,
            "candidate_count": len(candidates),
            "accepted_count": len(injectable_sources),
            "diagnostic_count": len(diagnostics),
            "status": response_status,
            "capabilities_observed": capabilities,
        },
    )
    return comparison


def _append_no_evidence_guard(messages: list) -> list:
    guard = (
        "File and knowledge retrieval found no usable source evidence for this turn. "
        "Do not present claims as supported by retrieved sources; answer from general "
        "knowledge only when appropriate, or state that the provided sources do not "
        "contain enough evidence. If the requested source scope is unclear, ask a "
        "concise clarification instead of guessing."
    )
    return add_or_update_system_message(guard, messages, append=True)


def _is_selected_source_metadata_first_diagnostics_only(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False

    strategy = (
        metadata.get("first_pass_retrieval_strategy")
        if isinstance(metadata.get("first_pass_retrieval_strategy"), dict)
        else {}
    )
    if not bool(strategy.get("metadata_first_intent")):
        return False

    status = str(metadata.get("status") or "").strip().lower()
    if status not in {"blocked", "no_evidence"}:
        return False

    references = metadata.get("references")
    if isinstance(references, list) and any(isinstance(item, dict) for item in references):
        return False

    accepted_outputs = metadata.get("accepted_outputs")
    if isinstance(accepted_outputs, list) and any(
        isinstance(item, dict) for item in accepted_outputs
    ):
        return False

    return True


def _selected_source_has_accepted_evidence(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False

    for key in (
        "canonical_references",
        "reference_cards",
        "references",
        "accepted_outputs",
    ):
        value = metadata.get(key)
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return True

    return False


def _selected_source_requires_stable_limitation_response(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    if not _metadata_is_selected_source_turn(metadata):
        return False
    if _selected_source_metadata_first_diagnostics_lane(metadata):
        return True

    status = str(metadata.get("status") or "").strip().lower()
    if status not in {"blocked", "no_evidence"}:
        return False
    if _selected_source_has_accepted_evidence(metadata):
        return False

    terminal_reason = str(metadata.get("terminal_reason") or "").strip().lower()
    active_scope = (
        metadata.get("active_source_scope")
        if isinstance(metadata.get("active_source_scope"), dict)
        else {}
    )
    scope_status = str(active_scope.get("status") or "").strip().lower()
    scope_reason = str(active_scope.get("reason") or "").strip().lower()

    return (
        terminal_reason
        in {
            "ambiguous_retrieval_scope",
            "source_scope_blocked",
            "expired_or_conflicting",
        }
        or scope_status in {"ambiguous", "expired"}
        or scope_reason in {"ambiguous_retrieval_scope", "expired_or_conflicting"}
    )


def _selected_source_metadata_first_intent(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False

    strategy = (
        metadata.get("first_pass_retrieval_strategy")
        if isinstance(metadata.get("first_pass_retrieval_strategy"), dict)
        else {}
    )
    if bool(strategy.get("metadata_first_intent")):
        return True
    if (
        str(strategy.get("retrieval_strategy") or "").strip().lower()
        == "metadata_first_then_targeted_chunks"
    ):
        return True

    routing = (
        metadata.get("execution_profile_routing_diagnostics")
        if isinstance(metadata.get("execution_profile_routing_diagnostics"), dict)
        else {}
    )
    classified_evidence_need = str(
        routing.get("classified_evidence_need") or ""
    ).strip().lower()
    if classified_evidence_need.startswith("metadata_first"):
        return True

    provenance = (
        metadata.get("retrieval_provenance")
        if isinstance(metadata.get("retrieval_provenance"), dict)
        else {}
    )
    provenance_strategy = (
        provenance.get("strategy") if isinstance(provenance.get("strategy"), dict) else {}
    )
    evidence_need = str(provenance_strategy.get("evidence_need") or "").strip().lower()
    retrieval_strategy = str(
        provenance_strategy.get("retrieval_strategy") or ""
    ).strip().lower()
    return evidence_need.startswith("metadata_first") or (
        retrieval_strategy == "metadata_first_then_targeted_chunks"
    )


def _selected_source_limitation_content(metadata: object) -> str:
    active_scope = (
        metadata.get("active_source_scope")
        if isinstance(metadata, dict) and isinstance(metadata.get("active_source_scope"), dict)
        else {}
    )
    source_names: list[str] = []
    for source_item in active_scope.get("sources") or []:
        if not isinstance(source_item, dict):
            continue
        name = str(source_item.get("name") or "").strip()
        if name and name not in source_names:
            source_names.append(name)
    source_label = (
        f"当前所选来源（{source_names[0]}）"
        if source_names
        else "当前所选来源"
    )

    terminal_reason = str(
        metadata.get("terminal_reason") if isinstance(metadata, dict) else ""
    ).strip().lower()
    scope_status = str(active_scope.get("status") or "").strip().lower()
    scope_reason = str(active_scope.get("reason") or "").strip().lower()
    reason_codes = {
        str(item.get("reason") or "").strip().lower()
        for item in (
            metadata.get("retrieval_diagnostics")
            if isinstance(metadata, dict) and isinstance(metadata.get("retrieval_diagnostics"), list)
            else []
        )
        if isinstance(item, dict)
    }

    limitation_detail = ""
    if (
        terminal_reason == "ambiguous_retrieval_scope"
        or scope_status == "ambiguous"
        or scope_reason == "ambiguous_retrieval_scope"
    ):
        if len(source_names) > 1:
            candidate_names = "、".join(source_names[:4])
            suffix = "等" if len(source_names) > 4 else ""
            return (
                f"当前所选来源包含多个候选文件（{candidate_names}{suffix}），"
                "我无法判断您指的是哪一个，因此不能直接给出结论。"
                "请明确指定文件名后重试。"
            )
        if source_names:
            return (
                f"当前所选来源（{source_names[0]}）的指代仍不明确，因此不能直接给出结论。"
                "请明确指定文件名后重试。"
            )
        return (
            "当前所选来源的指代仍不明确，因此不能直接给出结论。"
            "请明确指定文件名后重试。"
        )
    if terminal_reason in {"source_scope_blocked", "expired_or_conflicting"} or (
        scope_status == "expired" or scope_reason == "expired_or_conflicting"
    ):
        return (
            "当前所选来源范围已变化或仍不明确，本轮不能把历史来源当作当前证据。"
            "请重新明确指定目标文件后重试。"
        )
    if (
        "unsupported_latest_or_recent_claim" in reason_codes
        or "unsupported_year_range_claim" in reason_codes
    ):
        limitation_detail = "缺少可验证的时间依据，无法判断“最近”或年份范围。"
    elif "unsupported_document_type_claim" in reason_codes:
        limitation_detail = "缺少可验证的文档类型标注，无法确认“公文/论文/讲话/政策”等类型。"
    elif "unsupported_topic_scope_claim" in reason_codes:
        limitation_detail = "缺少可验证的主题关联标注，无法确认“与XX相关”。"
    elif terminal_reason == "metadata_first_targeted_evidence_required":
        limitation_detail = "当前问题需要面向候选文档的定向证据，但本轮未命中可用证据。"
    else:
        limitation_detail = "本轮检索未获得可用于支撑该请求的证据。"

    return (
        f"{source_label}{limitation_detail}"
        "因此无法给出具体文档列表或结论。"
        "请提供更具体的文档标题、时间范围、类型或主题后重试。"
    )


def _replace_output_text_with_limitation(output: Any, limitation_text: str) -> list[dict]:
    normalized_output = copy.deepcopy(output) if isinstance(output, list) else []
    limitation_payload = [{"type": "output_text", "text": limitation_text}]
    for item in reversed(normalized_output):
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().lower() != "message":
            continue
        if str(item.get("role") or "assistant").strip().lower() != "assistant":
            continue
        item["content"] = limitation_payload
        item["status"] = "completed"
        return normalized_output

    normalized_output.append(
        {
            "type": "message",
            "id": output_id("msg"),
            "status": "completed",
            "role": "assistant",
            "content": limitation_payload,
        }
    )
    return normalized_output


def _apply_selected_source_diagnostics_only_content_guard(
    *,
    content: str,
    output: Any,
    metadata: object,
    completion_metadata: object,
) -> tuple[str, list[dict]]:
    metadata = metadata if isinstance(metadata, dict) else {}
    completion_metadata = completion_metadata if isinstance(completion_metadata, dict) else {}
    merged_metadata: dict[str, Any] = {**metadata, **completion_metadata}

    if not _metadata_is_selected_source_turn(merged_metadata):
        return str(content or ""), copy.deepcopy(output) if isinstance(output, list) else []
    if not _selected_source_requires_stable_limitation_response(merged_metadata):
        return str(content or ""), copy.deepcopy(output) if isinstance(output, list) else []

    limitation_text = _selected_source_limitation_content(merged_metadata)
    return limitation_text, _replace_output_text_with_limitation(output, limitation_text)


def _enforce_selected_source_metadata_first_final_consistency(
    *,
    completion_metadata: object,
    metadata: object,
) -> dict[str, Any]:
    completion_metadata = (
        copy.deepcopy(completion_metadata) if isinstance(completion_metadata, dict) else {}
    )
    metadata = metadata if isinstance(metadata, dict) else {}
    merged_metadata: dict[str, Any] = {**metadata, **completion_metadata}

    if not _metadata_is_selected_source_turn(merged_metadata):
        return completion_metadata
    if not _selected_source_metadata_first_intent(merged_metadata):
        return completion_metadata

    refs = (
        completion_metadata.get("canonical_references")
        if isinstance(completion_metadata.get("canonical_references"), list)
        else []
    )
    cards = (
        completion_metadata.get("reference_cards")
        if isinstance(completion_metadata.get("reference_cards"), list)
        else []
    )
    status_value = str(
        completion_metadata.get("status") or metadata.get("status") or ""
    ).strip().lower()
    terminal_reason = str(
        completion_metadata.get("terminal_reason")
        or metadata.get("terminal_reason")
        or ""
    ).strip().lower()
    diagnostics = Chats._merge_retrieval_diagnostics(
        completion_metadata.get("retrieval_diagnostics"),
        metadata.get("retrieval_diagnostics"),
    )
    reason_codes = {
        str(item.get("reason") or "").strip().lower()
        for item in diagnostics
        if isinstance(item, dict)
    }

    if status_value == "success" and not refs and not cards:
        status_value = "no_evidence"
        terminal_reason = "no_accepted_references"
        diagnostics = Chats._merge_retrieval_diagnostics(
            diagnostics,
            [
                _safe_retrieval_diagnostic(
                    classification="no_evidence",
                    reason="no_accepted_references",
                    candidate_index=-1,
                    outcome="no_evidence",
                )
            ],
        )

    unsupported_terminal = bool(terminal_reason.startswith("unsupported_"))
    unsupported_diagnostics = any(reason.startswith("unsupported_") for reason in reason_codes)
    diagnostics_only_lane = _selected_source_metadata_first_diagnostics_lane(
        {
            **merged_metadata,
            "status": status_value,
            "terminal_reason": terminal_reason,
            "retrieval_diagnostics": diagnostics,
        }
    ) or (unsupported_terminal and unsupported_diagnostics)

    if diagnostics_only_lane:
        completion_metadata.pop("canonical_references", None)
        completion_metadata.pop("reference_cards", None)

    if status_value:
        completion_metadata["status"] = status_value
    if terminal_reason:
        completion_metadata["terminal_reason"] = terminal_reason
    if diagnostics:
        completion_metadata["retrieval_diagnostics"] = diagnostics

    return completion_metadata


def _append_selected_source_limitation_guard(
    messages: list,
    *,
    terminal_reason: str = "",
    active_source_scope: Optional[dict] = None,
) -> list:
    reason_text = str(terminal_reason or "").strip().lower()
    scope_names: list[str] = []
    if isinstance(active_source_scope, dict):
        for source_item in active_source_scope.get("sources") or []:
            if not isinstance(source_item, dict):
                continue
            name = str(source_item.get("name") or "").strip()
            if name and name not in scope_names:
                scope_names.append(name)
    if len(scope_names) > 1:
        source_clause = (
            " If you mention selected sources, only reference the current selected set: "
            + ", ".join(scope_names[:4])
            + "."
        )
    elif scope_names:
        source_clause = (
            " If you mention the selected source, only reference "
            f"{scope_names[0]}."
        )
    else:
        source_clause = (
            " Avoid naming prior sources; use neutral wording like '当前所选来源'."
        )
    scope_status = (
        str(active_source_scope.get("status") or "").strip().lower()
        if isinstance(active_source_scope, dict)
        else ""
    )
    ambiguity_clause = ""
    if reason_text == "ambiguous_retrieval_scope" or scope_status == "ambiguous":
        ambiguity_clause = (
            " Explain that multiple selected files currently match the user's reference"
            " and ask the user to name the target file. Do not say no file was uploaded"
            " and do not ask for re-upload unless no selected source is actually present."
        )
    reason_clause = (
        f" Current retrieval terminal reason: {reason_text}."
        if reason_text
        else ""
    )
    guard = (
        "This selected-source retrieval turn is blocked or has no usable evidence."
        " Do not provide concrete document lists, article titles, dates, or counts as"
        " if supported by selected-source retrieval in this turn. Do not reuse"
        " previous-turn source-derived lists as current-turn evidence. Provide a concise"
        " limitation or clarification response instead, and ask for a narrowed target if"
        f" needed.{source_clause}{ambiguity_clause}{reason_clause}"
    )
    return add_or_update_system_message(guard, messages, append=True)


_RETRIEVAL_DEICTIC_ONLY_PATTERN = re.compile(
    r"^(this|that|it|these|those|"
    r"(?:this|that|the)\s+(file|document|attachment)|"
    r"(?:these|those)\s+(files|documents|attachments)|"
    r"这个|那个|这些|那些|该(文件|文档|附件|资料|材料|知识库)|"
    r"(?:这个|那个|这些|那些)(文件|文档|附件|资料|材料|知识库))[\s?？。.!！]*$",
    flags=re.IGNORECASE,
)
_RETRIEVAL_SOURCE_DEICTIC_PATTERN = re.compile(
    r"\b(?:this|that)\s+(?:file|document|attachment|source|knowledge\s+base|kb)\b|"
    r"(?:这个|那个|该|本)(?:文件|文档|附件|资料|材料|公文|报告|来源|知识库)|"
    r"(?:这|那)(?:份|篇)(?:文件|文档|附件|资料|材料|公文|报告)",
    flags=re.IGNORECASE,
)
_RETRIEVAL_CONTINUATION_ONLY_PATTERN = re.compile(
    r"^(?:"
    r"(?:please\s+)?continue(?:\s+please)?|"
    r"go\s+on|"
    r"keep\s+(?:going|reading|analyzing|summarizing)|"
    r"(?:tell\s+me\s+)?more|"
    r"(?:请)?继续(?:\s*(?:一下|看|分析|总结|处理|说明|说|讲|往下))?|"
    r"接着(?:\s*(?:说|讲|分析|总结|看))?|"
    r"往下(?:\s*(?:说|讲|分析|总结|看))?"
    r")[\s?？。.!！]*$",
    flags=re.IGNORECASE,
)
_RETRIEVAL_FOLLOWUP_CUE_PATTERN = re.compile(
    r"\b(?:continue|continuing|next|then|after\s+that|go\s+on|keep\s+going|"
    r"keep\s+reading|keep\s+analyzing|keep\s+summarizing|more)\b|"
    r"(?:继续|接着|然后|后面|后续|接下来|再(?:往)?下|往下|下一(?:段|条|页|部分)?)",
    flags=re.IGNORECASE,
)
_RETRIEVAL_EXPLICIT_MULTI_SOURCE_PATTERN = re.compile(
    r"\b(?:these|those|both|all(?:\s+selected)?)\s+"
    r"(?:\w+\s+){0,3}(?:files|documents|attachments|sources|knowledge\s+bases|kbs)\b|"
    r"\b(?:these|those)\s+(?:two|three|four|\d+)\s+"
    r"(?:files|documents|attachments|sources|knowledge\s+bases|kbs)\b|"
    r"\b(?:differences?|similarities|compare|comparison|contrast)\b.{0,80}"
    r"\b(?:these|those|both|all(?:\s+selected)?)\s+"
    r"(?:files|documents|attachments|sources|knowledge\s+bases|kbs)\b|"
    r"\b(?:compare|contrast)\b.{0,80}\b(?:and|with|vs\.?|versus)\b|"
    r"(?:比较|对比).{0,80}(?:和|与|及|以及|、).{1,80}|"
    r"(?:比较|对比|比对|差异|区别|异同).{0,80}"
    r"(?:这些|那些|这(?:两|二|2|三|3|四|4|几|多)(?:个|份|篇)?)"
    r"(?:文件|文档|附件|资料|材料|公文|报告|来源|知识库)|"
    r"这些(?:文件|文档|附件|资料|材料|来源|知识库)|"
    r"这(?:两|二|2|三|3|四|4|几|多)(?:个|份|篇)?"
    r"(?:文件|文档|附件|资料|材料|公文|报告|来源|知识库)",
    flags=re.IGNORECASE,
)
_GENERIC_SOURCE_SCOPE_LABELS = {
    "file",
    "document",
    "attachment",
    "source",
    "knowledge base",
    "kb",
    "files",
    "documents",
    "attachments",
    "sources",
    "knowledge bases",
    "kbs",
    "文件",
    "文档",
    "附件",
    "资料",
    "材料",
    "来源",
    "知识库",
    "公文",
    "报告",
}
_NON_SPECIFIC_FILE_LABEL_TOKENS = {
    "attachment",
    "attachments",
    "csv",
    "doc",
    "docx",
    "document",
    "documents",
    "file",
    "files",
    "html",
    "json",
    "kb",
    "knowledge",
    "markdown",
    "md",
    "note",
    "notes",
    "pdf",
    "ppt",
    "pptx",
    "report",
    "reports",
    "source",
    "sources",
    "txt",
    "xlsx",
}
_ATTACHED_FILES_CONTEXT_PATTERN = re.compile(
    r"<attached_files>.*?</attached_files>\s*",
    flags=re.IGNORECASE | re.DOTALL,
)
_RETRIEVAL_BROADENING_PATTERN = re.compile(
    r"\b(web|internet|online|all\s+knowledge\s+bases|knowledge\s+base\s+wide|"
    r"chat\s+history|previous\s+chats|historical\s+chats)\b|"
    r"全网|互联网|所有知识库|全部知识库|历史聊天|聊天记录",
    flags=re.IGNORECASE,
)
_RETRIEVAL_FILENAME_PATTERN = re.compile(
    r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff\s._\-（）()【】\[\]]{0,120}"
    r"\.(?:pdf|docx?|xlsx?|pptx?|txt|md|csv)",
    flags=re.IGNORECASE,
)
_RETRIEVAL_OFFICIAL_DOC_NO_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{0,30}[〔\[]\d{4}[〕\]]\s*[\w\u4e00-\u9fff\-]*\d+\s*号"
)
_RETRIEVAL_DATE_PATTERN = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\d{4}年\d{1,2}月(?:\d{1,2}日)?"
)
_RETRIEVAL_CLAUSE_PATTERN = re.compile(
    r"第[一二三四五六七八九十百千万0-9]+[条章节款项]|"
    r"\b\d+(?:\.\d+){1,4}\b"
)
_RETRIEVAL_PROJECT_WORKFLOW_PATTERN = re.compile(
    r"(?:项目|工程|流程|workflow|project)[：:#\s-]*[A-Za-z0-9_\-\u4e00-\u9fff]{3,}",
    flags=re.IGNORECASE,
)
_FIRST_PASS_METADATA_FIRST_EVIDENCE_NEEDS = frozenset(
    {
        "metadata_first_inventory",
        "metadata_first_targeted_chunks",
        "document_inventory",
        "document_listing",
        "temporal_listing",
        "topic_scoped_listing",
        "document_type_listing",
        "count_or_statistics",
        "table_of_contents",
        "corpus_overview",
    }
)
_FIRST_PASS_METADATA_FIRST_HINT_ALIASES = frozenset(
    {
        # Execution-profile routing diagnostics expose these selected-source
        # evidence needs; treat them as metadata-first gates in first pass.
        "metadata_first_retrieval",
        "recency_inventory_retrieval",
        "listing_or_inventory_retrieval",
        "second_pass_selected_source_retrieval",
    }
)
_FIRST_PASS_SEMANTIC_EVIDENCE_NEEDS = frozenset(
    {
        "narrow_fact",
        "selected_source_retrieval",
        "focused_read",
        "semantic_detail",
        "semantic_chunks",
    }
)
_FIRST_PASS_RESEARCH_ONLY_EVIDENCE_NEEDS = frozenset(
    {
        "metadata_first_retrieval",
        "metadata_first_inventory",
        "recency_inventory_retrieval",
        "listing_or_inventory_retrieval",
        "selected_source_research",
        "deep_selected_source_research",
        "second_pass_selected_source_retrieval",
        "insufficient_first_pass_evidence",
    }
)
_FIRST_PASS_METADATA_QUERY_FACET_PATTERN = re.compile(
    r"\b(?:document|doc_id|evidence_facets|topic_terms|requested_types|user_anchors)\s*=",
    flags=re.IGNORECASE,
)
_FIRST_PASS_WEAK_METADATA_HINT_PATTERN = re.compile(
    r"\b(?:list|listing|inventory|catalog|metadata|table\s+of\s+contents|toc|"
    r"count|statistics|latest|recent)\b|"
    r"(?:列出|清单|目录|元数据|目录结构|统计|最近|最新|前两年|近两年|近三年|公文|论文|讲话|政策|相关)",
    flags=re.IGNORECASE,
)
_FIRST_PASS_WEAK_NARROW_HINT_PATTERN = re.compile(
    r"\b(?:what\s+is|define|definition|explain|how|why)\b|"
    r"(?:什么是|解释|定义|如何|为什么|怎么|详情)",
    flags=re.IGNORECASE,
)
_FIRST_PASS_RECENCY_CLAIM_PATTERN = re.compile(
    r"\b(?:latest|recent|newest|most\s+recent)\b|"
    r"(?:最新|最近|近(?:两|2|三|3)年|前(?:两|2|三|3)年)",
    flags=re.IGNORECASE,
)
_FIRST_PASS_YEAR_RANGE_CLAIM_PATTERN = re.compile(
    r"\b(?:19|20)\d{2}\b|"
    r"(?:前(?:两|2|三|3)年|近(?:两|2|三|3)年|最近(?:两|2|三|3)年|近年)",
    flags=re.IGNORECASE,
)
_FIRST_PASS_TYPE_CLAIM_PATTERN = re.compile(
    r"\b(?:policy|policies|paper|papers|speech|speeches|report|reports|notice|notices)\b|"
    r"(?:公文|论文|讲话|政策|报告|通知|纪要)",
    flags=re.IGNORECASE,
)
_FIRST_PASS_TOPIC_CLAIM_PATTERN = re.compile(
    r"\b(?:related\s+to|about|topic|entity|with)\b|"
    r"(?:相关|关于|围绕|主题|话题|有关)",
    flags=re.IGNORECASE,
)


def _normalize_retrieval_query_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _strip_attached_file_context(value: Any) -> str:
    return _ATTACHED_FILES_CONTEXT_PATTERN.sub("", str(value or "")).strip()


def _normalize_anchor_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _clean_retrieval_anchor(value: str) -> str:
    return re.sub(
        r"^(?:请)?(?:根据|依据|按照|参照|关于)",
        "",
        str(value or "").strip(),
    ).strip()


def _extract_prompt_retrieval_anchors(prompt: str) -> list[str]:
    anchors: list[str] = []
    normalized_prompt = _strip_attached_file_context(prompt)
    patterns = (
        _RETRIEVAL_FILENAME_PATTERN,
        _RETRIEVAL_OFFICIAL_DOC_NO_PATTERN,
        _RETRIEVAL_DATE_PATTERN,
        _RETRIEVAL_CLAUSE_PATTERN,
        _RETRIEVAL_PROJECT_WORKFLOW_PATTERN,
    )
    for pattern in patterns:
        anchors.extend(
            _clean_retrieval_anchor(match.group(0))
            for match in pattern.finditer(normalized_prompt)
        )

    anchors.extend(
        _clean_retrieval_anchor(match.group(1))
        for match in re.finditer(r"['\"“”‘’《》](.{2,80}?)['\"“”‘’《》]", normalized_prompt)
        if match.group(1).strip()
    )

    deduped: list[str] = []
    seen: set[str] = set()
    for anchor in anchors:
        normalized = _normalize_anchor_text(anchor)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(anchor)
    return deduped


def _candidate_scope_labels(retrieval_candidates: list[dict]) -> list[str]:
    labels: list[str] = []
    for item in retrieval_candidates or []:
        if not isinstance(item, dict):
            continue
        for key in ("name", "title", "filename", "file_name", "document_name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())

        file_data = item.get("file")
        if isinstance(file_data, dict):
            for key in ("name", "filename"):
                value = file_data.get(key)
                if isinstance(value, str) and value.strip():
                    labels.append(value.strip())
            meta = file_data.get("meta")
            if isinstance(meta, dict):
                for key in (
                    "name",
                    "title",
                    "filename",
                    "file_name",
                    "document_name",
                ):
                    value = meta.get(key)
                    if isinstance(value, str) and value.strip():
                        labels.append(value.strip())

        for key in ("collection_name", "id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())
        collection_names = item.get("collection_names")
        if isinstance(collection_names, list):
            labels.extend(
                str(value).strip()
                for value in collection_names
                if str(value).strip()
            )

    deduped: list[str] = []
    seen: set[str] = set()
    for label in labels:
        normalized = _normalize_anchor_text(label)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(label)
    return deduped


def _prompt_explicitly_requests_multiple_sources(prompt: str) -> bool:
    return bool(
        _RETRIEVAL_EXPLICIT_MULTI_SOURCE_PATTERN.search(
            _strip_attached_file_context(prompt)
        )
    )


def _prompt_has_direct_source_deictic_reference(prompt: str) -> bool:
    normalized_prompt = _normalize_retrieval_query_text(
        _strip_attached_file_context(prompt)
    )
    if not normalized_prompt:
        return False
    return bool(
        _RETRIEVAL_DEICTIC_ONLY_PATTERN.match(normalized_prompt)
        or _RETRIEVAL_SOURCE_DEICTIC_PATTERN.search(normalized_prompt)
    )


def _prompt_has_weak_followup_intent(prompt: str) -> bool:
    normalized_prompt = _normalize_retrieval_query_text(
        _strip_attached_file_context(prompt)
    )
    if not normalized_prompt:
        return False
    if _RETRIEVAL_CONTINUATION_ONLY_PATTERN.match(normalized_prompt):
        return True

    compact_prompt = re.sub(r"[\s?？。.!！,:，；;、]+", "", normalized_prompt)
    if not compact_prompt or len(compact_prompt) > 24:
        return False

    return bool(_RETRIEVAL_FOLLOWUP_CUE_PATTERN.search(normalized_prompt))


def _prompt_has_source_deictic_reference(prompt: str) -> bool:
    return _prompt_has_direct_source_deictic_reference(
        prompt
    ) or _prompt_has_weak_followup_intent(prompt)


def _is_specific_anchor_token(token: str) -> bool:
    normalized = str(token or "").strip().lower()
    if (
        not normalized
        or normalized in _GENERIC_SOURCE_SCOPE_LABELS
        or normalized in _NON_SPECIFIC_FILE_LABEL_TOKENS
    ):
        return False
    if normalized.isdigit():
        return False
    if not re.search(r"[a-zA-Z\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]", normalized):
        return False
    return _has_specific_focus_label(normalized)


def _expanded_selected_source_anchor_tokens(token: str) -> set[str]:
    normalized = str(token or "").strip().lower()
    if not normalized:
        return set()

    variants: set[str] = set()
    generic_terms = sorted(
        {
            *(str(label).lower() for label in _GENERIC_SOURCE_SCOPE_LABELS),
            *(str(label).lower() for label in _NON_SPECIFIC_FILE_LABEL_TOKENS),
        },
        key=len,
        reverse=True,
    )

    for generic in generic_terms:
        if not generic or normalized == generic:
            continue

        candidates: list[str] = []
        if normalized.startswith(generic):
            candidates.append(normalized[len(generic) :])
        if normalized.endswith(generic):
            candidates.append(normalized[: -len(generic)])

        for candidate in candidates:
            candidate = candidate.strip(" _-.()[]{}")
            if _is_specific_anchor_token(candidate):
                variants.add(candidate)

    return variants


def _reference_label_anchor_tokens(label: str) -> set[str]:
    normalized = _normalize_focus_match_text(label)
    if not normalized:
        return set()

    tokens: set[str] = set()
    for token in normalized.split():
        if _is_specific_anchor_token(token):
            tokens.add(token)
        tokens.update(_expanded_selected_source_anchor_tokens(token))

    return tokens



def _selected_source_anchor_terms(selected_candidates: list[dict]) -> list[set[str]]:
    variant_sets: list[set[str]] = []
    token_sets: list[set[str]] = []
    token_counts: dict[str, int] = {}

    for candidate in selected_candidates or []:
        variants = {
            variant
            for variant in _get_reference_label_variants(candidate)
            if variant and variant not in _GENERIC_SOURCE_SCOPE_LABELS
        }
        tokens: set[str] = set()
        for variant in variants:
            tokens.update(_reference_label_anchor_tokens(variant))
        for token in tokens:
            token_counts[token] = token_counts.get(token, 0) + 1
        variant_sets.append(variants)
        token_sets.append(tokens)

    return [
        {
            *variants,
            *{
                token
                for token in tokens
                if token_counts.get(token, 0) == 1
            },
        }
        for variants, tokens in zip(variant_sets, token_sets)
    ]


def _prompt_mentions_selected_source_anchor(
    prompt: str, file_item: dict, anchor_terms: Optional[set[str]] = None
) -> bool:
    normalized_prompt = _normalize_focus_match_text(
        _strip_attached_file_context(prompt)
    )
    if not normalized_prompt:
        return False

    if not isinstance(anchor_terms, set):
        anchor_terms = {
            variant
            for variant in _get_reference_label_variants(file_item)
            if variant and variant not in _GENERIC_SOURCE_SCOPE_LABELS
        }

    return any(term and term in normalized_prompt for term in anchor_terms)


def _selected_source_anchor_matches(
    prompt: str, selected_candidates: list[dict]
) -> list[dict]:
    normalized_candidates = [
        candidate for candidate in selected_candidates or [] if isinstance(candidate, dict)
    ]
    anchor_terms = _selected_source_anchor_terms(normalized_candidates)
    return [
        candidate
        for candidate, candidate_terms in zip(normalized_candidates, anchor_terms)
        if _prompt_mentions_selected_source_anchor(
            prompt,
            candidate,
            anchor_terms=candidate_terms,
        )
    ]


def _selected_source_referent_intent(
    prompt: str, selected_candidates: list[dict]
) -> dict[str, Any]:
    normalized_candidates = [
        candidate for candidate in selected_candidates or [] if isinstance(candidate, dict)
    ]
    anchor_matches = _selected_source_anchor_matches(prompt, normalized_candidates)
    explicit_multi = _prompt_explicitly_requests_multiple_sources(prompt)
    direct_deictic = _prompt_has_direct_source_deictic_reference(prompt)
    weak_followup = _prompt_has_weak_followup_intent(prompt)
    return {
        "anchor_matches": anchor_matches,
        "explicit_multi": explicit_multi,
        "direct_deictic": direct_deictic,
        "weak_followup": weak_followup,
        "weak_referential_intent": direct_deictic or weak_followup,
    }


def _selected_source_scope_is_ambiguous(
    prompt: str, selected_candidates: list[dict]
) -> bool:
    selected_candidates = [
        candidate for candidate in selected_candidates or [] if isinstance(candidate, dict)
    ]
    if len(selected_candidates) <= 1:
        return False
    intent = _selected_source_referent_intent(prompt, selected_candidates)
    if intent["explicit_multi"]:
        return False
    if intent["anchor_matches"]:
        return False
    return bool(intent["weak_referential_intent"])


def _active_selected_source_candidates(file_items: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    for file_item in file_items or []:
        if not isinstance(file_item, dict):
            continue
        if _is_media_file_item(file_item):
            continue
        if _is_active_focus_file(file_item):
            candidates.append(file_item)
    return candidates


def _file_item_scope_values(file_item: dict) -> set[str]:
    values: set[str] = set()
    if not isinstance(file_item, dict):
        return values

    for key in (
        "id",
        "file_id",
        "fileId",
        "collection_name",
        "name",
        "filename",
    ):
        value = str(file_item.get(key) or "").strip()
        if value:
            values.add(_normalize_anchor_text(value))

    collection_names = file_item.get("collection_names")
    if isinstance(collection_names, list):
        for value in collection_names:
            normalized_value = str(value or "").strip()
            if normalized_value:
                values.add(_normalize_anchor_text(normalized_value))

    return {value for value in values if value}


def _source_scope_values(source: dict) -> set[str]:
    values: set[str] = set()
    if not isinstance(source, dict):
        return values

    source_info = source.get("source")
    if isinstance(source_info, dict):
        for key in ("id", "file_id", "name", "filename", "url"):
            value = str(source_info.get(key) or "").strip()
            if value:
                values.add(_normalize_anchor_text(value))

    for metadata in source.get("metadata") or []:
        if not isinstance(metadata, dict):
            continue
        for key in (
            "source",
            "id",
            "file_id",
            "name",
            "filename",
            "file_name",
            "document_name",
            "collection_name",
        ):
            value = str(metadata.get(key) or "").strip()
            if value:
                values.add(_normalize_anchor_text(value))

    return {value for value in values if value}


def _source_matches_file_item(source: dict, file_item: dict) -> bool:
    file_values = _file_item_scope_values(file_item)
    source_values = _source_scope_values(source)
    return bool(file_values and source_values and file_values.intersection(source_values))


def _filter_inline_sources_for_selected_files(
    inline_sources: list[dict], selected_files: list[dict]
) -> list[dict]:
    if not inline_sources or not selected_files:
        return []
    return [
        source
        for source in inline_sources
        if any(_source_matches_file_item(source, file_item) for file_item in selected_files)
    ]


def _filter_files_for_selected_files(
    file_items: list[dict], selected_files: list[dict]
) -> list[dict]:
    selected_signatures = {
        json.dumps(sorted(_file_item_scope_values(file_item)), ensure_ascii=False)
        for file_item in selected_files or []
        if _file_item_scope_values(file_item)
    }
    if not selected_signatures:
        return []

    filtered: list[dict] = []
    for file_item in file_items or []:
        signature = json.dumps(
            sorted(_file_item_scope_values(file_item)), ensure_ascii=False
        )
        if signature in selected_signatures:
            filtered.append(file_item)
    return filtered


def _normalize_active_source_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"file", "document", "attachment"}:
        return "file"
    if normalized in {
        "collection",
        "knowledge",
        "knowledge_base",
        "knowledge base",
        "kb",
    }:
        return "knowledge"
    if normalized in {"note", "web"}:
        return normalized
    return "unknown"


def _active_source_identity_from_file_item(file_item: dict) -> Optional[dict]:
    if not isinstance(file_item, dict):
        return None

    source_id = ""
    for key in ("id", "file_id", "fileId", "collection_name", "url"):
        value = str(file_item.get(key) or "").strip()
        if value:
            source_id = value
            break

    name = ""
    for key in ("name", "filename", "title", "collection_name"):
        value = str(file_item.get(key) or "").strip()
        if value:
            name = value
            break

    if not source_id and not name:
        return None

    return {
        "id": source_id or name,
        "name": name or source_id,
        "type": _normalize_active_source_type(file_item.get("type")),
    }


def _active_source_identity_from_reference(reference: dict) -> Optional[dict]:
    if not isinstance(reference, dict):
        return None

    source_info = reference.get("source")
    source_info = source_info if isinstance(source_info, dict) else {}

    source_id = str(
        source_info.get("id")
        or source_info.get("file_id")
        or reference.get("source_id")
        or reference.get("id")
        or ""
    ).strip()
    name = str(
        source_info.get("name")
        or source_info.get("filename")
        or reference.get("source_name")
        or reference.get("name")
        or reference.get("filename")
        or reference.get("title")
        or ""
    ).strip()
    source_type = _normalize_active_source_type(
        source_info.get("type")
        or reference.get("source_type")
        or reference.get("source_class")
        or reference.get("type")
    )

    metadatas = reference.get("metadata")
    if isinstance(metadatas, list):
        for metadata in metadatas:
            if not isinstance(metadata, dict):
                continue
            if not source_id:
                source_id = str(
                    metadata.get("file_id")
                    or metadata.get("source")
                    or metadata.get("id")
                    or metadata.get("collection_name")
                    or ""
                ).strip()
            if not name:
                name = str(
                    metadata.get("name")
                    or metadata.get("filename")
                    or metadata.get("file_name")
                    or metadata.get("document_name")
                    or metadata.get("collection_name")
                    or ""
                ).strip()
            if source_type == "unknown":
                source_type = _normalize_active_source_type(
                    metadata.get("source_type") or metadata.get("type")
                )
            if source_id and name and source_type != "unknown":
                break

    if not source_id and not name:
        return None

    return {
        "id": source_id or name,
        "name": name or source_id,
        "type": source_type,
    }


def _active_source_scope_metadata(
    candidates: list[dict],
    *,
    status: str,
    source_set_mode: str,
    reason: str,
    confidence: str,
) -> dict:
    sources: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates or []:
        identity = _active_source_identity_from_file_item(candidate)
        if not identity:
            continue
        signature = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        sources.append(identity)

    source_ids = [
        str(source.get("id") or "").strip()
        for source in sources
        if str(source.get("id") or "").strip()
    ]

    return {
        "status": status,
        "source_set_mode": source_set_mode,
        "source_ids": source_ids,
        "sources": sources,
        "reason": reason,
        "confidence": confidence,
        "expires_on": "new_upload_or_explicit_change",
    }


def _active_source_scope_values(scope: object) -> set[str]:
    values: set[str] = set()
    if not isinstance(scope, dict):
        return values

    for value in scope.get("source_ids") or []:
        normalized = _normalize_anchor_text(value)
        if normalized:
            values.add(normalized)

    for source in scope.get("sources") or []:
        if not isinstance(source, dict):
            continue
        for key in ("id", "name"):
            normalized = _normalize_anchor_text(source.get(key))
            if normalized:
                values.add(normalized)

    return values


def _active_source_focus_from_reference(reference: dict) -> Optional[dict]:
    values = _source_scope_values(reference)
    identity = _active_source_identity_from_reference(reference)
    if identity:
        for key in ("id", "name"):
            normalized = _normalize_anchor_text(identity.get(key))
            if normalized:
                values.add(normalized)
    if not values or not identity:
        return None
    return {
        "values": values,
        "source": identity,
        "reason": "previous_single_canonical_reference",
        "confidence": "high",
    }


def _active_source_focus_from_scope(scope: object) -> Optional[dict]:
    if not isinstance(scope, dict):
        return None
    if str(scope.get("status") or "").strip().lower() != "resolved":
        return None
    if str(scope.get("source_set_mode") or "").strip().lower() != "single":
        return None

    values = _active_source_scope_values(scope)
    sources = [source for source in scope.get("sources") or [] if isinstance(source, dict)]
    if not values or len(sources) != 1:
        return None

    return {
        "values": values,
        "source": {
            "id": str(sources[0].get("id") or "").strip(),
            "name": str(sources[0].get("name") or "").strip(),
            "type": _normalize_active_source_type(sources[0].get("type")),
        },
        "reason": str(scope.get("reason") or "user_clarified_deictic_reference"),
        "confidence": str(scope.get("confidence") or "medium"),
    }


def _active_source_focus_from_source(
    source: object,
    *,
    reason: str,
    confidence: str = "high",
) -> Optional[dict]:
    if not isinstance(source, dict):
        return None

    identity = _active_source_identity_from_file_item(source)
    if identity is None:
        identity = _active_source_identity_from_reference(source)
    if identity is None and isinstance(source.get("source"), dict):
        identity = _active_source_identity_from_reference(source)
    if identity is None:
        return None

    values: set[str] = set()
    for value in (
        identity.get("id"),
        identity.get("name"),
        source.get("id"),
        source.get("file_id"),
        source.get("fileId"),
        source.get("name"),
        source.get("filename"),
        source.get("title"),
    ):
        normalized = _normalize_anchor_text(value)
        if normalized:
            values.add(normalized)

    values.update(_file_item_scope_values(source))
    values.update(_source_scope_values(source))
    if not values:
        return None

    return {
        "values": values,
        "source": identity,
        "reason": reason,
        "confidence": confidence,
    }


def _active_source_focus_from_metadata_value(
    value: object,
    *,
    reason: str,
    confidence: str = "high",
) -> Optional[dict]:
    if not isinstance(value, dict):
        return None

    scope_focus = _active_source_focus_from_scope(value)
    if scope_focus:
        return {
            **scope_focus,
            "reason": str(scope_focus.get("reason") or reason),
            "confidence": str(scope_focus.get("confidence") or confidence),
        }

    return _active_source_focus_from_source(
        value,
        reason=reason,
        confidence=confidence,
    )


def _reliable_active_source_focuses_from_metadata(metadata: object) -> list[dict]:
    metadata = metadata if isinstance(metadata, dict) else {}
    focus_specs = (
        (
            "current_preview_source",
            "current_preview_source",
            "high",
        ),
        (
            "operation_panel_source",
            "operation_panel_source",
            "high",
        ),
        (
            "current_source_focus",
            "current_preview_source",
            "high",
        ),
        (
            "active_source_selection",
            "operation_panel_source",
            "high",
        ),
        (
            "pinned_source_scope",
            "pinned_source_scope",
            "high",
        ),
        (
            "pinned_active_source_scope",
            "pinned_source_scope",
            "high",
        ),
        (
            "recent_source_card_interaction",
            "recent_source_card_interaction",
            "high",
        ),
        (
            "source_card_focus",
            "recent_source_card_interaction",
            "high",
        ),
        (
            "active_source_scope",
            "active_source_scope",
            "medium",
        ),
    )

    focuses: list[dict] = []
    seen: set[str] = set()
    for key, reason, confidence in focus_specs:
        focus = _active_source_focus_from_metadata_value(
            metadata.get(key),
            reason=reason,
            confidence=confidence,
        )
        if not focus:
            continue
        signature = json.dumps(
            sorted(str(value) for value in focus.get("values") or []),
            ensure_ascii=False,
        )
        if signature in seen:
            continue
        seen.add(signature)
        focuses.append(focus)
    return focuses


def _message_retrieval_diagnostics(message: object) -> list[dict]:
    if not isinstance(message, dict):
        return []
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    diagnostics = metadata.get("retrieval_diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = message.get("retrieval_diagnostics")
    if not isinstance(diagnostics, list):
        return []
    return [item for item in diagnostics if isinstance(item, dict)]


def _latest_assistant_had_ambiguous_scope(stored_messages: list[dict]) -> bool:
    for message in reversed(stored_messages or []):
        if str(message.get("role") or "").strip().lower() != "assistant":
            continue
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        scope = metadata.get("active_source_scope")
        if isinstance(scope, dict):
            status = str(scope.get("status") or "").strip().lower()
            reason = str(
                scope.get("reason") or scope.get("ambiguity_reason") or ""
            ).strip()
            if status == "ambiguous" or reason == "ambiguous_retrieval_scope":
                return True
        return any(
            str(item.get("reason") or "").strip() == "ambiguous_retrieval_scope"
            for item in _message_retrieval_diagnostics(message)
        )
    return False


def _latest_assistant_single_source_focus(
    stored_messages: list[dict],
) -> Optional[dict]:
    for message in reversed(stored_messages or []):
        if str(message.get("role") or "").strip().lower() != "assistant":
            continue

        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}

        scope_focus = _active_source_focus_from_scope(metadata.get("active_source_scope"))
        if scope_focus:
            return scope_focus

        references = Chats.build_canonical_references(
            metadata.get("canonical_references"),
            message.get("canonical_references"),
            metadata.get("sources"),
            message.get("sources"),
        )
        if len(references) != 1:
            return None
        return _active_source_focus_from_reference(references[0])

    return None


def _latest_user_explicit_source_focus(
    stored_messages: list[dict],
    selected_candidates: list[dict],
) -> Optional[dict]:
    for message in reversed(stored_messages or []):
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        matches = _selected_source_anchor_matches(content, selected_candidates)
        if len(matches) != 1:
            return None
        return _active_source_focus_from_source(
            matches[0],
            reason="previous_explicit_user_source_mention",
            confidence="high",
        )
    return None


def _reliable_active_source_focuses(
    *,
    metadata: object,
    stored_messages: list[dict],
    candidates: list[dict],
) -> list[dict]:
    focuses = [*_reliable_active_source_focuses_from_metadata(metadata)]
    latest_assistant_focus = _latest_assistant_single_source_focus(stored_messages)
    if latest_assistant_focus:
        focuses.append(latest_assistant_focus)
    latest_user_focus = _latest_user_explicit_source_focus(
        stored_messages,
        candidates,
    )
    if latest_user_focus:
        focuses.append(latest_user_focus)
    return focuses


def _current_source_scope_values(current_files: Optional[list[dict]]) -> set[str]:
    values: set[str] = set()
    for file_item in current_files or []:
        if not isinstance(file_item, dict):
            continue
        values.update(_file_item_scope_values(file_item))
    return values


def _candidate_matches_focus(candidate: dict, focus: dict) -> bool:
    candidate_values = _file_item_scope_values(candidate)
    focus_values = focus.get("values") if isinstance(focus, dict) else None
    return bool(
        candidate_values
        and isinstance(focus_values, set)
        and candidate_values.intersection(focus_values)
    )


def _candidate_in_current_scope(candidate: dict, current_values: set[str]) -> bool:
    if not current_values:
        return True
    return bool(_file_item_scope_values(candidate).intersection(current_values))


def _focus_matching_candidates(
    focus: dict,
    candidates: list[dict],
    current_values: set[str],
) -> list[dict]:
    return [
        candidate
        for candidate in candidates or []
        if _candidate_matches_focus(candidate, focus)
        and _candidate_in_current_scope(candidate, current_values)
    ]


def _first_reliable_matching_focus(
    focuses: list[dict],
    candidates: list[dict],
    current_values: set[str],
) -> tuple[dict | None, list[dict]]:
    saw_focus = False
    for focus in focuses or []:
        if not isinstance(focus, dict):
            continue
        saw_focus = True
        matches = _focus_matching_candidates(focus, candidates, current_values)
        if len(matches) == 1:
            return focus, matches
    return ({"reason": "expired_or_conflicting"} if saw_focus else None), []


def _resolve_active_source_scope(
    prompt: str,
    selected_candidates: list[dict],
    *,
    stored_messages: Optional[list[dict]] = None,
    current_files: Optional[list[dict]] = None,
    fallback_candidates: Optional[list[dict]] = None,
    metadata: Optional[dict] = None,
) -> tuple[dict, list[dict], bool]:
    selected_candidates = [
        candidate for candidate in selected_candidates or [] if isinstance(candidate, dict)
    ]
    fallback_candidates = [
        candidate for candidate in fallback_candidates or [] if isinstance(candidate, dict)
    ]

    current_values = _current_source_scope_values(current_files)
    intent = _selected_source_referent_intent(prompt, selected_candidates)
    explicit_multi = bool(intent["explicit_multi"])
    referential_intent = bool(intent["weak_referential_intent"])
    anchor_matches = intent["anchor_matches"]
    previous_ambiguous_scope = _latest_assistant_had_ambiguous_scope(
        stored_messages or []
    )

    def finish(scope: dict, resolved_files: list[dict], blocked: bool):
        observe_llm_event(
            "source_scope.resolve",
            {
                **summarize_source_scope(scope),
                "selected_candidate_count": len(selected_candidates),
                "resolved_candidate_count": len(resolved_files),
                "current_source_count": len(current_files or []),
                "blocked": blocked,
                "prompt_has_deictic": bool(intent["direct_deictic"]),
                "prompt_has_weak_followup": bool(intent["weak_followup"]),
                "prompt_has_referential_intent": referential_intent,
                "prompt_explicit_multi": explicit_multi,
                "anchor_match_count": len(anchor_matches),
            },
        )
        return scope, resolved_files, blocked

    if not selected_candidates:
        if previous_ambiguous_scope and fallback_candidates:
            fallback_anchor_matches = _selected_source_anchor_matches(
                prompt, fallback_candidates
            )
            current_fallback_matches = [
                candidate
                for candidate in fallback_anchor_matches
                if _candidate_in_current_scope(candidate, current_values)
            ]
            if len(current_fallback_matches) == 1:
                return finish(
                    _active_source_scope_metadata(
                        current_fallback_matches,
                        status="resolved",
                        source_set_mode="single",
                        reason="user_clarified_deictic_reference",
                        confidence="high",
                    ),
                    current_fallback_matches,
                    False,
                )
            if fallback_anchor_matches:
                reason = (
                    "expired_or_conflicting"
                    if not current_fallback_matches
                    else "ambiguous_retrieval_scope"
                )
                status = "expired" if reason == "expired_or_conflicting" else "ambiguous"
                return finish(
                    _active_source_scope_metadata(
                        current_fallback_matches or fallback_anchor_matches,
                        status=status,
                        source_set_mode="none",
                        reason=reason,
                        confidence="low",
                    ),
                    [],
                    True,
                )

        if referential_intent and fallback_candidates:
            focus, matching_candidates = _first_reliable_matching_focus(
                _reliable_active_source_focuses(
                    metadata=metadata,
                    stored_messages=stored_messages or [],
                    candidates=fallback_candidates,
                ),
                fallback_candidates,
                current_values,
            )
            if focus and matching_candidates:
                if len(matching_candidates) == 1:
                    return finish(
                        _active_source_scope_metadata(
                            matching_candidates,
                            status="resolved",
                            source_set_mode="single",
                            reason=str(
                                focus.get("reason")
                                or "previous_single_canonical_reference"
                            ),
                            confidence=str(focus.get("confidence") or "high"),
                        ),
                        matching_candidates,
                        False,
                    )

        return finish({}, [], False)

    if anchor_matches:
        if previous_ambiguous_scope and len(anchor_matches) > 1 and not explicit_multi:
            ambiguous_scope = _active_source_scope_metadata(
                anchor_matches,
                status="ambiguous",
                source_set_mode="none",
                reason="ambiguous_retrieval_scope",
                confidence="low",
            )
            return finish(ambiguous_scope, [], True)

        reason = (
            "user_clarified_deictic_reference"
            if len(anchor_matches) == 1
            and previous_ambiguous_scope
            else "explicit_anchor"
        )
        mode = "single" if len(anchor_matches) == 1 else "multi"
        return finish(
            _active_source_scope_metadata(
                anchor_matches,
                status="resolved",
                source_set_mode=mode,
                reason=reason,
                confidence="high",
            ),
            anchor_matches,
            False,
        )

    if previous_ambiguous_scope and selected_candidates and fallback_candidates:
        fallback_anchor_matches = _selected_source_anchor_matches(
            prompt, fallback_candidates
        )
        if fallback_anchor_matches:
            expired_scope = _active_source_scope_metadata(
                fallback_anchor_matches,
                status="expired",
                source_set_mode="none",
                reason="expired_or_conflicting",
                confidence="low",
            )
            return finish(expired_scope, [], True)

    if explicit_multi:
        return finish(
            _active_source_scope_metadata(
                selected_candidates,
                status="resolved",
                source_set_mode="multi",
                reason="explicit_anchor",
                confidence="high",
            ),
            selected_candidates,
            False,
        )

    if len(selected_candidates) == 1:
        return finish(
            _active_source_scope_metadata(
                selected_candidates,
                status="resolved",
                source_set_mode="single",
                reason="current_turn_upload",
                confidence="high",
            ),
            selected_candidates,
            False,
        )

    if referential_intent:
        focus, matching_candidates = _first_reliable_matching_focus(
            _reliable_active_source_focuses(
                metadata=metadata,
                stored_messages=stored_messages or [],
                candidates=selected_candidates,
            ),
            selected_candidates,
            current_values,
        )
        if focus:
            if len(matching_candidates) == 1:
                return finish(
                    _active_source_scope_metadata(
                        matching_candidates,
                        status="resolved",
                        source_set_mode="single",
                        reason=str(
                            focus.get("reason")
                            or "previous_single_canonical_reference"
                        ),
                        confidence=str(focus.get("confidence") or "high"),
                    ),
                    matching_candidates,
                    False,
                )

            expired_scope = _active_source_scope_metadata(
                [],
                status="expired",
                source_set_mode="none",
                reason="expired_or_conflicting",
                confidence="low",
            )
            return finish(expired_scope, [], True)

        ambiguous_scope = _active_source_scope_metadata(
            selected_candidates,
            status="ambiguous",
            source_set_mode="none",
            reason="ambiguous_retrieval_scope",
            confidence="low",
        )
        return finish(ambiguous_scope, [], True)

    return finish({}, selected_candidates, False)


def _is_explicit_research_retrieval(body: dict) -> bool:
    metadata = body.get("metadata") if isinstance(body, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    candidates = (
        body.get("execution_profile"),
        body.get("executionProfile"),
        metadata.get("execution_profile"),
        metadata.get("executionProfile"),
    )
    if any(str(value or "").strip().lower() == "research" for value in candidates):
        return True

    for key in ("research_mode", "researchMode"):
        if body.get(key) is True or metadata.get(key) is True:
            return True

    return False


def _has_selected_source_retrieval_runtime_tool(metadata: dict) -> bool:
    snapshot = metadata.get("deepagent_runtime_tools") if isinstance(metadata, dict) else None
    if not isinstance(snapshot, dict):
        return False
    for tool_entry in snapshot.get("tools") or []:
        if not isinstance(tool_entry, dict):
            continue
        if str(tool_entry.get("tool_id") or "").strip() == "builtin:retrieval":
            return True
        for function_entry in tool_entry.get("functions") or []:
            if not isinstance(function_entry, dict):
                continue
            if str(function_entry.get("function_name") or "").strip() in {
                "query_selected_knowledge_files",
                "read_selected_file",
            }:
                return True
    return False


def _prompt_has_explicit_multi_part_retrieval_request(prompt: str) -> bool:
    normalized_prompt = _normalize_retrieval_query_text(
        _strip_attached_file_context(prompt)
    )
    if not normalized_prompt:
        return False

    if len(re.findall(r"[?？]", normalized_prompt)) >= 2:
        return True

    return bool(
        re.search(
            r"\b(?:also|separately|respectively|compare|contrast)\b|"
            r"(?:分别|同时|并且|另外|此外|对比|比较|一方面|另一方面)",
            normalized_prompt,
            flags=re.IGNORECASE,
        )
    )


def _retrieval_scope_is_ambiguous(prompt: str, retrieval_candidates: list[dict]) -> bool:
    normalized_prompt = _normalize_retrieval_query_text(
        _strip_attached_file_context(prompt)
    )
    if not normalized_prompt:
        return True
    return _selected_source_scope_is_ambiguous(normalized_prompt, retrieval_candidates)


def _query_broadens_retrieval_scope(query: str, original_query: str) -> bool:
    if not _RETRIEVAL_BROADENING_PATTERN.search(query):
        return False
    return not _RETRIEVAL_BROADENING_PATTERN.search(original_query)


def _query_preserves_required_anchors(query: str, anchors: list[str]) -> bool:
    normalized_query = _normalize_anchor_text(query)
    for anchor in anchors:
        normalized_anchor = _normalize_anchor_text(anchor)
        if normalized_anchor and normalized_anchor not in normalized_query:
            return False
    return True


def _normalize_generated_retrieval_queries(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [str(query) for query in value if str(query or "").strip()]


def _active_source_scope_blocks_query_generation(scope: object) -> bool:
    if not isinstance(scope, dict):
        return False
    status = str(scope.get("status") or "").strip().lower()
    source_set_mode = str(scope.get("source_set_mode") or "").strip().lower()
    reason = str(
        scope.get("reason")
        or scope.get("ambiguity_reason")
        or scope.get("expiration_reason")
        or ""
    ).strip()
    return status in {"ambiguous", "expired"} or (
        source_set_mode == "none"
        and reason in {"ambiguous_retrieval_scope", "expired_or_conflicting"}
    )


def _first_pass_retrieval_hint_values(body: dict, metadata: dict) -> list[str]:
    hint_values: list[str] = []
    for mapping in (body, metadata):
        if not isinstance(mapping, dict):
            continue
        for key in (
            "evidence_need",
            "retrieval_evidence_need",
            "retrieval_intent",
            "retrieval_strategy",
        ):
            for value in _list_from_optional_strings(mapping.get(key)):
                normalized = value.strip().lower()
                if normalized and normalized not in hint_values:
                    hint_values.append(normalized)

    profile_diagnostics = (
        metadata.get("execution_profile_routing_diagnostics")
        if isinstance(metadata, dict)
        else {}
    )
    if isinstance(profile_diagnostics, dict):
        for key in ("classified_evidence_need", "evidence_need", "retrieval_strategy"):
            for value in _list_from_optional_strings(profile_diagnostics.get(key)):
                normalized = value.strip().lower()
                if normalized and normalized not in hint_values:
                    hint_values.append(normalized)

    targeted_request = (
        metadata.get("selected_source_next_targeted_chunk_request")
        if isinstance(metadata, dict)
        else {}
    )
    if isinstance(targeted_request, dict):
        for value in _list_from_optional_strings(targeted_request.get("evidence_need")):
            normalized = value.strip().lower()
            if normalized and normalized not in hint_values:
                hint_values.append(normalized)

    return hint_values


def _first_pass_candidate_identity_values(retrieval_candidates: list[dict]) -> set[str]:
    values: set[str] = set()
    for candidate in retrieval_candidates or []:
        if not isinstance(candidate, dict):
            continue
        identity = _active_source_identity_from_file_item(candidate)
        if not isinstance(identity, dict):
            continue
        for key in ("id", "name"):
            normalized = _normalize_anchor_text(identity.get(key))
            if normalized:
                values.add(normalized)
    return values


def _first_pass_selected_source_strategy(
    *,
    prompt: str,
    body: dict,
    metadata: dict,
    retrieval_candidates: list[dict],
    active_source_scope: Optional[dict],
) -> dict[str, Any]:
    normalized_prompt = _normalize_retrieval_query_text(_strip_attached_file_context(prompt))
    reason_codes: list[str] = []
    hint_values = _first_pass_retrieval_hint_values(body, metadata)
    if hint_values:
        reason_codes.extend(f"evidence_hint:{value}" for value in hint_values)

    weak_metadata_hint = bool(
        normalized_prompt
        and _FIRST_PASS_WEAK_METADATA_HINT_PATTERN.search(normalized_prompt)
    )
    if weak_metadata_hint:
        reason_codes.append("weak_query_metadata_first_hint")

    weak_narrow_hint = bool(
        normalized_prompt
        and _FIRST_PASS_WEAK_NARROW_HINT_PATTERN.search(normalized_prompt)
    )
    if weak_narrow_hint:
        reason_codes.append("weak_query_narrow_detail_hint")

    strong_metadata_first_intent = any(
        value in _FIRST_PASS_METADATA_FIRST_EVIDENCE_NEEDS
        or value in _FIRST_PASS_METADATA_FIRST_HINT_ALIASES
        or value.startswith("metadata_first")
        for value in hint_values
    ) or any(value == "metadata_first_then_targeted_chunks" for value in hint_values)

    explicit_multi_request = _prompt_explicitly_requests_multiple_sources(prompt) or (
        _prompt_has_explicit_multi_part_retrieval_request(prompt)
    )
    scope_mode = (
        str(active_source_scope.get("source_set_mode") or "").strip().lower()
        if isinstance(active_source_scope, dict)
        else ""
    )
    multi_scope_comparison_intent = bool(
        scope_mode == "multi"
        and normalized_prompt
        and re.search(
            r"\b(?:difference|differences|similarity|similarities|compare|comparison|contrast)\b|"
            r"(?:差异|区别|异同|比较|对比|比对)",
            normalized_prompt,
            flags=re.IGNORECASE,
        )
    )

    metadata_first_intent = strong_metadata_first_intent or (
        weak_metadata_hint
        and not weak_narrow_hint
        and not explicit_multi_request
        and not multi_scope_comparison_intent
    )
    if weak_metadata_hint and (
        explicit_multi_request
        or (multi_scope_comparison_intent and not strong_metadata_first_intent)
    ):
        reason_codes.append("weak_metadata_hint_suppressed_for_explicit_multi_scope")

    normalized_evidence_need = next(
        (
            value
            for value in hint_values
            if value in _FIRST_PASS_METADATA_FIRST_EVIDENCE_NEEDS
            or value in _FIRST_PASS_METADATA_FIRST_HINT_ALIASES
            or value in _FIRST_PASS_SEMANTIC_EVIDENCE_NEEDS
            or value.startswith("metadata_first")
        ),
        "",
    )

    targeted_request = (
        metadata.get("selected_source_next_targeted_chunk_request")
        if isinstance(metadata, dict)
        else {}
    )
    targeted_request = targeted_request if isinstance(targeted_request, dict) else {}
    targeted_source_ids = _list_from_optional_strings(targeted_request.get("source_ids"))
    if not targeted_source_ids:
        single_source_id = str(targeted_request.get("source_id") or "").strip()
        if single_source_id:
            targeted_source_ids = [single_source_id]
    required_anchors = _list_from_optional_strings(targeted_request.get("required_anchors"))
    targeted_query = _normalize_retrieval_query_text(targeted_request.get("query") or "")

    targeted_context_present = bool(targeted_source_ids and required_anchors)
    if targeted_context_present:
        reason_codes.append("targeted_context_present")

    scope_values = _active_source_scope_values(active_source_scope)
    candidate_values = _first_pass_candidate_identity_values(retrieval_candidates)
    allowed_values = set(scope_values)
    allowed_values.update(candidate_values)
    targeted_ids_in_scope = (
        targeted_context_present
        and bool(allowed_values)
        and all(
            _normalize_anchor_text(source_id) in allowed_values
            for source_id in targeted_source_ids
        )
    )
    if targeted_context_present and not targeted_ids_in_scope:
        reason_codes.append("targeted_context_out_of_scope")

    structured_query_facets = bool(
        _FIRST_PASS_METADATA_QUERY_FACET_PATTERN.search(targeted_query or normalized_prompt)
    )
    if structured_query_facets:
        reason_codes.append("structured_query_facets")

    targeted_context_bounded = bool(
        targeted_context_present and targeted_ids_in_scope and structured_query_facets
    )
    if targeted_context_bounded:
        reason_codes.append("targeted_context_bounded")

    retrieval_strategy = "semantic_chunks"
    semantic_chunk_lookup_ok = True
    if metadata_first_intent:
        retrieval_strategy = "metadata_first_then_targeted_chunks"
        semantic_chunk_lookup_ok = targeted_context_bounded
        if not semantic_chunk_lookup_ok:
            reason_codes.append("metadata_first_requires_bounded_targeted_context")
    elif normalized_evidence_need in _FIRST_PASS_SEMANTIC_EVIDENCE_NEEDS:
        reason_codes.append("semantic_detail_evidence_need")
    elif not normalized_evidence_need:
        reason_codes.append("default_semantic_strategy")

    return {
        "evidence_need": normalized_evidence_need or "none",
        "retrieval_strategy": retrieval_strategy,
        "metadata_first_intent": bool(metadata_first_intent),
        "semantic_chunk_lookup_ok": bool(semantic_chunk_lookup_ok),
        "targeted_context_present": bool(targeted_context_present),
        "targeted_context_bounded": bool(targeted_context_bounded),
        "reason_codes": list(dict.fromkeys(reason_codes)),
    }


def _retrieval_engine_bypassed_first_pass_strategy(
    *,
    engine_contract: dict[str, Any],
    authority_state: str,
) -> dict[str, Any]:
    status = str(engine_contract.get("status") or "").strip().lower()
    terminal_reason = str(engine_contract.get("terminal_reason") or "").strip()
    reason = _retrieval_engine_bypass_reason(authority_state)
    return {
        "evidence_need": "engine_owned",
        "retrieval_strategy": _retrieval_engine_runtime_mode(authority_state),
        "metadata_first_intent": False,
        "semantic_chunk_lookup_ok": False,
        "targeted_context_present": False,
        "targeted_context_bounded": False,
        "middleware_strategy_bypassed": True,
        "bypass_reason": reason,
        "engine_authority_state": str(authority_state or ""),
        "engine_status": status,
        "engine_terminal_reason": terminal_reason,
        "reason_codes": [
            "middleware_strategy_bypassed",
            reason,
            *(
                [f"engine_status:{status}"]
                if status
                else []
            ),
        ],
    }


def _first_pass_profile_lock_incompatibility(
    *,
    metadata: dict,
    strategy: dict[str, Any],
) -> dict[str, Any]:
    diagnostics = (
        metadata.get("execution_profile_routing_diagnostics")
        if isinstance(metadata, dict)
        else {}
    )
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    non_promotion_reason = str(diagnostics.get("non_promotion_reason") or "").strip().lower()
    classified_evidence_need = str(
        diagnostics.get("classified_evidence_need") or ""
    ).strip().lower()
    resolved_execution_profile = str(
        diagnostics.get("resolved_execution_profile")
        or metadata.get("resolved_execution_profile")
        or metadata.get("execution_profile")
        or "general"
    ).strip().lower()

    explicit_lock = non_promotion_reason in {
        "explicit_incompatible_profile",
        "chat_profile_zero_tool_lane",
    }
    research_only_need = (
        classified_evidence_need in _FIRST_PASS_RESEARCH_ONLY_EVIDENCE_NEEDS
    ) or bool(
        strategy.get("metadata_first_intent")
        and str(strategy.get("retrieval_strategy") or "").strip().lower()
        == "metadata_first_then_targeted_chunks"
    )
    incompatible = explicit_lock and research_only_need
    reason_codes: list[str] = []
    if explicit_lock:
        reason_codes.append(f"profile_lock:{non_promotion_reason}")
    if classified_evidence_need:
        reason_codes.append(f"classified_evidence_need:{classified_evidence_need}")
    if research_only_need:
        reason_codes.append("research_only_selected_source_need")

    return {
        "incompatible": bool(incompatible),
        "non_promotion_reason": non_promotion_reason or "none",
        "classified_evidence_need": classified_evidence_need or "none",
        "resolved_execution_profile": resolved_execution_profile or "general",
        "reason_codes": reason_codes,
    }


def _first_pass_structured_terms(query: str) -> dict[str, list[str]]:
    terms = {
        "document": [],
        "evidence_facets": [],
        "topic_terms": [],
        "requested_types": [],
        "date_basis": [],
        "type_basis": [],
        "topic_basis": [],
    }
    for key, raw_value in re.findall(
        r"\b(document|evidence_facets|topic_terms|requested_types|date_basis|type_basis|topic_basis)\s*=\s*([^;\n]+)",
        str(query or ""),
        flags=re.IGNORECASE,
    ):
        normalized_key = str(key or "").strip().lower()
        if normalized_key not in terms:
            continue
        for part in re.split(r"[;,，、|/]", str(raw_value or "")):
            candidate = part.strip()
            if candidate and candidate not in terms[normalized_key]:
                terms[normalized_key].append(candidate)
    return terms


async def _selected_collection_inventory_entries(
    active_scope_files: list[dict],
    *,
    request: Optional[Request] = None,
    user: Optional[UserModel] = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_file_ids: set[str] = set()
    for file_item in active_scope_files or []:
        if not isinstance(file_item, dict):
            continue
        file_type = str(file_item.get("type") or "").strip().lower()
        if file_type not in {"collection", "knowledge"}:
            continue
        collection_id = str(file_item.get("id") or "").strip()
        if not collection_id:
            continue
        source_name = str(file_item.get("name") or "").strip()
        local_file_models = Knowledges.get_files_by_id(collection_id) or []
        for file_model in local_file_models:
            file_payload = (
                file_model.model_dump()
                if hasattr(file_model, "model_dump")
                else file_model
            )
            if not isinstance(file_payload, dict):
                continue
            file_id = str(file_payload.get("id") or "").strip()
            if not file_id or file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)

            file_meta = (
                file_payload.get("meta")
                if isinstance(file_payload.get("meta"), dict)
                else {}
            )
            file_data = (
                file_payload.get("data")
                if isinstance(file_payload.get("data"), dict)
                else {}
            )
            title = str(
                file_payload.get("filename")
                or file_meta.get("name")
                or file_payload.get("name")
                or file_id
            ).strip()
            snippet = str(file_data.get("content") or "").strip()
            if snippet:
                snippet = re.sub(r"\s+", " ", snippet)[:600]
            else:
                snippet = title

            entries.append(
                {
                    "source_id": collection_id,
                    "source_name": source_name or title,
                    "file_id": file_id,
                    "title": title,
                    "snippet": snippet,
                    "updated_at": int(file_payload.get("updated_at") or 0),
                }
            )

        if local_file_models:
            continue

        if request is None or user is None:
            continue

        # Runtime shared/public selected collections can be Knowflow-managed and
        # absent from local Knowledges rows. Reuse managed lookup inventory here.
        try:
            inventory_result = await list_knowledge_documents(
                request.app.state.config,
                user,
                collection_id,
                page=1,
                page_size=64,
            )
        except Exception:
            inventory_result = {}

        for file_payload in inventory_result.get("items") or []:
            if not isinstance(file_payload, dict):
                continue
            file_id = str(file_payload.get("id") or "").strip()
            if not file_id or file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)

            file_meta = (
                file_payload.get("meta")
                if isinstance(file_payload.get("meta"), dict)
                else {}
            )
            file_data = (
                file_payload.get("data")
                if isinstance(file_payload.get("data"), dict)
                else {}
            )
            title = str(
                file_payload.get("filename")
                or file_meta.get("name")
                or file_payload.get("name")
                or file_id
            ).strip()
            snippet = str(file_data.get("content") or "").strip()
            if snippet:
                snippet = re.sub(r"\s+", " ", snippet)[:600]
            else:
                snippet = title

            entries.append(
                {
                    "source_id": collection_id,
                    "source_name": source_name or title,
                    "file_id": file_id,
                    "title": title,
                    "snippet": snippet,
                    "updated_at": int(file_payload.get("updated_at") or 0),
                }
            )

    return entries


async def _build_first_pass_inventory_targeted_request(
    *,
    query: str,
    active_scope_files: list[dict],
    strategy: dict[str, Any],
    request: Optional[Request] = None,
    user: Optional[UserModel] = None,
) -> dict[str, Any]:
    if not isinstance(strategy, dict) or not strategy.get("metadata_first_intent"):
        return {}

    entries = await _selected_collection_inventory_entries(
        active_scope_files,
        request=request,
        user=user,
    )
    if not entries:
        return {}

    normalized_query = _normalize_anchor_text(query)
    shortlisted = [
        entry
        for entry in entries
        if _normalize_anchor_text(entry.get("title")) in normalized_query
    ]
    if not shortlisted:
        shortlisted = list(entries)
    shortlisted = sorted(
        shortlisted,
        key=lambda item: (-int(item.get("updated_at") or 0), str(item.get("title") or "")),
    )[:8]
    if not shortlisted:
        return {}

    source_ids = list(
        dict.fromkeys(
            str(item.get("source_id") or "").strip() for item in shortlisted if item.get("source_id")
        )
    )
    if not source_ids:
        return {}

    anchor_source_name = str(shortlisted[0].get("source_name") or "").strip()
    if not anchor_source_name:
        anchor_source_name = str(shortlisted[0].get("title") or "").strip()
    if not anchor_source_name:
        return {}

    document_term = str(shortlisted[0].get("title") or "").strip()
    targeted_query = (
        f"document={document_term}; evidence_facets=title,ordering_basis;"
        " date_basis=file_updated_at"
    )
    return {
        "query": targeted_query,
        "source_ids": source_ids,
        "required_anchors": [anchor_source_name],
        "required_facets": ["title"],
        "evidence_need": "metadata_first_inventory",
        "shortlisted_documents": [
            {
                "source_id": str(item.get("source_id") or ""),
                "file_id": str(item.get("file_id") or ""),
                "title": str(item.get("title") or ""),
                "updated_at": int(item.get("updated_at") or 0),
            }
            for item in shortlisted
        ],
        "inventory_count": len(entries),
        "shortlist_count": len(shortlisted),
        "ordering_basis": "file_updated_at_desc",
        "inventory_sources": [
            {
                "source": {
                    "id": str(item.get("source_id") or ""),
                    "name": str(item.get("source_name") or ""),
                    "type": "collection",
                    "source": "knowledge",
                },
                "document": [str(item.get("snippet") or item.get("title") or "")],
                "metadata": [
                    {
                        "file_id": str(item.get("file_id") or ""),
                        "name": str(item.get("title") or ""),
                        "title": str(item.get("title") or ""),
                        "source": str(item.get("source_name") or ""),
                        "chunk_type": "inventory_row",
                        "retrieval_outcome": "success",
                        "retrieval_classification": "injectable",
                        "ordering_basis": "file_updated_at_desc",
                        "date_basis": "file_updated_at",
                    }
                ],
                "retrieval_outcome": "success",
                "retrieval_classification": "injectable",
                "source_class": "collection",
                "type": "retrieval_reference",
            }
            for item in shortlisted
        ],
    }


def _first_pass_metadata_first_diagnostics(
    *,
    query: str,
    retrieval_round: Any,
    strategy: dict[str, Any],
    inventory_count: int,
    shortlist_count: int,
    structured_terms: dict[str, list[str]],
) -> list[dict[str, Any]]:
    if not strategy.get("metadata_first_intent"):
        return []

    diagnostics: list[dict[str, Any]] = []
    has_date_basis_for_recency = bool(structured_terms.get("date_basis"))
    has_date_basis_for_range = bool(
        structured_terms.get("date_basis")
        or _FIRST_PASS_YEAR_RANGE_CLAIM_PATTERN.search(str(query or ""))
    )
    has_type_basis = bool(
        structured_terms.get("requested_types") or structured_terms.get("type_basis")
    )
    has_topic_basis = bool(
        structured_terms.get("topic_terms") or structured_terms.get("topic_basis")
    )

    if inventory_count <= 0 or shortlist_count <= 0:
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="no_evidence",
                reason="inventory_unavailable",
                candidate_index=-1,
                outcome="blocked",
            )
        )
    if _FIRST_PASS_RECENCY_CLAIM_PATTERN.search(query or "") and not has_date_basis_for_recency:
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="diagnostics",
                reason="missing_date_metadata",
                candidate_index=-1,
                outcome="unsupported",
            )
        )
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="no_evidence",
                reason="unsupported_latest_or_recent_claim",
                candidate_index=-1,
                outcome="unsupported",
            )
        )
    if _FIRST_PASS_YEAR_RANGE_CLAIM_PATTERN.search(query or "") and not has_date_basis_for_range:
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="no_evidence",
                reason="unsupported_year_range_claim",
                candidate_index=-1,
                outcome="unsupported",
            )
        )
    if _FIRST_PASS_TYPE_CLAIM_PATTERN.search(query or "") and not has_type_basis:
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="diagnostics",
                reason="missing_type_metadata",
                candidate_index=-1,
                outcome="unsupported",
            )
        )
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="no_evidence",
                reason="unsupported_document_type_claim",
                candidate_index=-1,
                outcome="unsupported",
            )
        )
    if _FIRST_PASS_TOPIC_CLAIM_PATTERN.search(query or "") and not has_topic_basis:
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="diagnostics",
                reason="missing_topic_metadata",
                candidate_index=-1,
                outcome="unsupported",
            )
        )
        diagnostics.append(
            _safe_retrieval_diagnostic(
                classification="no_evidence",
                reason="unsupported_topic_scope_claim",
                candidate_index=-1,
                outcome="unsupported",
            )
        )
    return diagnostics


def _first_pass_reference_identity_values(reference: dict) -> set[str]:
    values: set[str] = set()
    source = reference.get("source") if isinstance(reference.get("source"), dict) else {}
    for key in ("id", "name", "title", "source"):
        normalized = _normalize_anchor_text(source.get(key))
        if normalized:
            values.add(normalized)
    for metadata in reference.get("metadata") if isinstance(reference.get("metadata"), list) else []:
        if not isinstance(metadata, dict):
            continue
        for key in ("file_id", "fileId", "name", "title", "source"):
            normalized = _normalize_anchor_text(metadata.get(key))
            if normalized:
                values.add(normalized)
    return values


def _first_pass_chunk_supports_required_facet(
    facet: str,
    *,
    snippet: str,
    metadata: dict,
    source: dict,
    combined: str,
) -> bool:
    normalized_facet = _normalize_anchor_text(facet)
    if not normalized_facet:
        return False
    if normalized_facet in combined:
        return True

    if normalized_facet in {"title", "document_title"}:
        return bool(
            str(metadata.get("title") or "").strip()
            or str(metadata.get("name") or "").strip()
            or str(source.get("name") or "").strip()
        )
    if normalized_facet in {"ordering_basis", "order_basis"}:
        return bool(
            str(metadata.get("ordering_basis") or "").strip()
            or str(metadata.get("date_basis") or "").strip()
            or str(source.get("ordering_basis") or "").strip()
            or str(source.get("date_basis") or "").strip()
        )
    if normalized_facet in {"date_basis"}:
        return bool(
            str(metadata.get("date_basis") or "").strip()
            or str(source.get("date_basis") or "").strip()
        )
    if normalized_facet in {"type_basis"}:
        return bool(
            str(metadata.get("type_basis") or "").strip()
            or str(metadata.get("document_type") or "").strip()
            or str(metadata.get("doc_type") or "").strip()
        )
    if normalized_facet in {"topic_basis"}:
        return bool(
            str(metadata.get("topic_basis") or "").strip()
            or str(metadata.get("topic") or "").strip()
            or str(metadata.get("keywords") or "").strip()
        )

    # Fallback: allow direct snippet match for custom facets.
    return normalized_facet in _normalize_anchor_text(snippet)


def _first_pass_selected_source_references(
    sources: list[dict],
    *,
    strategy: dict[str, Any],
    targeted_source_ids: list[str],
    required_anchors: list[str],
    required_facets: list[str],
    prompt: str,
    retrieval_round: Any = 1,
) -> tuple[list[dict], list[dict[str, Any]]]:
    references = Chats.build_canonical_references(sources)
    if not strategy.get("metadata_first_intent"):
        return references, []

    allowed_values = {
        _normalize_anchor_text(source_id)
        for source_id in targeted_source_ids
        if _normalize_anchor_text(source_id)
    }
    normalized_anchors = [
        _normalize_anchor_text(anchor)
        for anchor in required_anchors
        if _normalize_anchor_text(anchor)
    ]
    normalized_facets = [
        _normalize_anchor_text(facet)
        for facet in required_facets
        if _normalize_anchor_text(facet)
    ]

    filtered: list[dict] = []
    diagnostics: list[dict[str, Any]] = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        if allowed_values and not _first_pass_reference_identity_values(reference).intersection(
            allowed_values
        ):
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="no_evidence",
                    reason="off_shortlist_chunk",
                    candidate_index=-1,
                    outcome="denied",
                )
            )
            continue

        documents = reference.get("document") if isinstance(reference.get("document"), list) else []
        metadatas = reference.get("metadata") if isinstance(reference.get("metadata"), list) else []
        accepted_indexes: list[int] = []
        for index, document in enumerate(documents):
            snippet = str(document or "").strip()
            if not snippet:
                continue
            metadata = metadatas[index] if index < len(metadatas) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            source = reference.get("source") if isinstance(reference.get("source"), dict) else {}
            combined = _normalize_anchor_text(
                f"{snippet}\n{metadata.get('name') or ''}\n{metadata.get('title') or ''}\n"
                f"{metadata.get('source') or ''}\n{source.get('name') or ''}\n{source.get('id') or ''}"
            )
            if normalized_anchors and not all(anchor in combined for anchor in normalized_anchors):
                diagnostics.append(
                    _safe_retrieval_diagnostic(
                        classification="no_evidence",
                        reason="weak_targeted_chunk_evidence",
                        candidate_index=-1,
                        chunk_total=len(documents),
                        outcome="weak_evidence",
                    )
                )
                continue
            if normalized_facets and not any(
                _first_pass_chunk_supports_required_facet(
                    facet,
                    snippet=snippet,
                    metadata=metadata,
                    source=source,
                    combined=combined,
                )
                for facet in normalized_facets
            ):
                diagnostics.append(
                    _safe_retrieval_diagnostic(
                        classification="no_evidence",
                        reason="off_facet_evidence",
                        candidate_index=-1,
                        chunk_total=len(documents),
                        outcome="low_relevance",
                    )
                )
                continue
            accepted_indexes.append(index)

        if not accepted_indexes:
            diagnostics.append(
                _safe_retrieval_diagnostic(
                    classification="no_evidence",
                    reason="no_injectable_evidence",
                    candidate_index=-1,
                    chunk_total=len(documents),
                )
            )
            continue

        filtered_reference = copy.deepcopy(reference)
        for key, value in reference.items():
            if isinstance(value, list) and len(value) == len(documents):
                filtered_reference[key] = [value[index] for index in accepted_indexes]
        filtered.append(filtered_reference)

    return filtered, Chats._merge_retrieval_diagnostics(diagnostics)


def _first_pass_selected_source_accepted_outputs(references: list[dict]) -> list[dict]:
    accepted_outputs: list[dict[str, Any]] = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        source = reference.get("source") if isinstance(reference.get("source"), dict) else {}
        documents = reference.get("document") if isinstance(reference.get("document"), list) else []
        metadatas = reference.get("metadata") if isinstance(reference.get("metadata"), list) else []
        snippet = str(documents[0] or "").strip() if documents else ""
        if not snippet:
            continue
        metadata = metadatas[0] if metadatas and isinstance(metadatas[0], dict) else {}
        accepted_outputs.append(
            {
                "type": "selected_source_evidence",
                "source": {
                    "id": str(source.get("id") or metadata.get("file_id") or ""),
                    "name": str(source.get("name") or metadata.get("name") or ""),
                    "type": str(source.get("type") or metadata.get("source_type") or ""),
                },
                "snippet": snippet,
                "provenance": {
                    "tool_name": "first_pass_selected_source_retrieval",
                    "retrieval_round": 1,
                },
            }
        )
    return accepted_outputs


def _constrain_retrieval_queries(
    *,
    original_query: str,
    generated_queries: Any,
    retrieval_candidates: list[dict],
    explicit_research: bool = False,
    active_source_scope: Optional[dict] = None,
) -> list[str]:
    original_query = _normalize_retrieval_query_text(
        _strip_attached_file_context(original_query)
    )
    if _active_source_scope_blocks_query_generation(
        active_source_scope
    ) or _retrieval_scope_is_ambiguous(original_query, retrieval_candidates):
        return []

    scope_labels = _candidate_scope_labels(retrieval_candidates)
    prompt_anchors = _extract_prompt_retrieval_anchors(original_query)
    prompt_scope_anchors = [
        label
        for label in scope_labels
        if _normalize_anchor_text(label) in _normalize_anchor_text(original_query)
    ]
    required_anchors = [*prompt_anchors, *prompt_scope_anchors]

    queries: list[str] = []
    seen: set[str] = set()

    def add_query(query: Any) -> bool:
        normalized_query = _normalize_retrieval_query_text(query)
        if not normalized_query:
            return False
        signature = normalized_query.lower()
        if signature in seen:
            return False
        seen.add(signature)
        queries.append(normalized_query)
        return True

    add_query(original_query)

    allow_query_variants = explicit_research or _prompt_has_explicit_multi_part_retrieval_request(
        original_query
    )
    max_total = None if explicit_research else 3 if allow_query_variants else 1
    if max_total is not None and len(queries) >= max_total:
        return queries

    for query in _normalize_generated_retrieval_queries(generated_queries):
        normalized_query = _normalize_retrieval_query_text(query)
        if not normalized_query:
            continue
        if not explicit_research and _query_broadens_retrieval_scope(
            normalized_query, original_query
        ):
            continue
        if required_anchors and not _query_preserves_required_anchors(
            normalized_query, required_anchors
        ):
            continue
        if (
            add_query(normalized_query)
            and max_total is not None
            and len(queries) >= max_total
        ):
            break

    return queries


def _get_stored_chat_messages(
    chat_id: str,
    user: UserModel,
    message_id: Optional[str] = None,
) -> list[dict]:
    if not chat_id or chat_id.startswith("local:"):
        return []

    chat = Chats.get_chat_by_id_and_user_id(chat_id, user.id)
    if not chat:
        return []

    history = chat.chat.get("history", {})
    target_message_id = message_id or history.get("currentId")
    return get_message_list(history.get("messages", {}), target_message_id)


def _collect_stored_message_files(stored_messages: list[dict]) -> list[dict]:
    collected: list[dict] = []
    seen_keys: set[str] = set()

    for stored_message in stored_messages:
        # Carry forward prior user-provided context, not assistant-generated artifacts.
        if str(stored_message.get("role") or "").strip().lower() != "user":
            continue
        for file_item in stored_message.get("files", []):
            if not isinstance(file_item, dict):
                continue
            if not _resolve_file_context_url(file_item):
                continue
            file_key = _file_context_key(file_item)
            if file_key and file_key in seen_keys:
                continue
            if file_key:
                seen_keys.add(file_key)
            collected.append(
                _apply_adaptive_focus_metadata(
                    file_item,
                    origin=_ADAPTIVE_FOCUS_HISTORY,
                    tier=_ADAPTIVE_FOCUS_REFERENCE,
                    force=True,
                )
            )

    return collected


def _prepend_file_context_to_message(message: dict, files: list[dict]) -> None:
    def format_file_tag(file_item: dict) -> str:
        attrs = f'type="{html.escape(str(file_item.get("type", "file")), quote=True)}"'

        file_url = _resolve_file_context_url(file_item)
        if file_url:
            attrs += f' url="{html.escape(file_url, quote=True)}"'

        content_type = str(file_item.get("content_type") or "").strip()
        if content_type:
            attrs += f' content_type="{html.escape(content_type, quote=True)}"'

        file_name = str(file_item.get("name") or file_item.get("filename") or "").strip()
        if file_name:
            attrs += f' name="{html.escape(file_name, quote=True)}"'

        file_id = str(file_item.get("id") or "").strip()
        if file_id:
            attrs += f' id="{html.escape(file_id, quote=True)}"'

        return f"<file {attrs}/>"

    normalized_files = []
    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        if not _resolve_file_context_url(file_item):
            continue
        normalized_files.append(file_item)

    if not normalized_files:
        return

    file_tags = [format_file_tag(file_item) for file_item in normalized_files]
    file_context = (
        "<attached_files>\n" + "\n".join(file_tags) + "\n</attached_files>\n\n"
    )

    content = message.get("content", "")
    if isinstance(content, list):
        message["content"] = [{"type": "text", "text": file_context}] + content
    else:
        message["content"] = file_context + content


def _align_stored_user_messages_for_file_context(
    request_user_messages: list[dict],
    stored_user_messages: list[dict],
) -> list[tuple[dict, dict]]:
    if not request_user_messages or not stored_user_messages:
        return []

    if len(request_user_messages) <= len(stored_user_messages):
        start_index = len(stored_user_messages) - len(request_user_messages)
        return list(zip(request_user_messages, stored_user_messages[start_index:]))

    return list(zip(request_user_messages, stored_user_messages))


def add_file_context(
    messages: list,
    chat_id: str,
    user,
    request_files: Optional[list[dict]] = None,
    message_id: Optional[str] = None,
) -> list:
    """
    Add file URLs to messages for native function calling.
    """
    if not messages:
        return messages

    stored_messages = []
    if chat_id and not chat_id.startswith("local:"):
        stored_messages = _get_stored_chat_messages(chat_id, user, message_id)

    injected_file_keys = set()

    request_user_messages = [
        message
        for message in messages
        if str(message.get("role") or "").strip().lower() == "user"
    ]
    stored_user_messages = [
        stored_message
        for stored_message in stored_messages
        if str(stored_message.get("role") or "").strip().lower() == "user"
    ]

    stored_files_by_message = {
        id(message): stored_message.get("files", [])
        for message, stored_message in _align_stored_user_messages_for_file_context(
            request_user_messages, stored_user_messages
        )
    }

    for message in request_user_messages:
        files_for_message = []
        combined_message_files = _dedupe_file_context_items(
            [
                *(
                    message.get("files", [])
                    if isinstance(message.get("files"), list)
                    else []
                ),
                *(
                    stored_files_by_message.get(id(message), [])
                    if isinstance(stored_files_by_message.get(id(message)), list)
                    else []
                ),
            ]
        )

        for file_item in combined_message_files:
            if not _resolve_file_context_url(file_item):
                continue
            files_for_message.append(file_item)
            file_key = _file_context_key(file_item)
            if file_key:
                injected_file_keys.add(file_key)

        if not files_for_message:
            continue

        _prepend_file_context_to_message(message, files_for_message)

    pending_request_files = []
    for file_item in request_files or []:
        if not isinstance(file_item, dict):
            continue
        if not _is_active_focus_file(file_item):
            continue
        file_key = _file_context_key(file_item)
        if file_key and file_key in injected_file_keys:
            continue
        if not _resolve_file_context_url(file_item):
            continue
        pending_request_files.append(file_item)
        if file_key:
            injected_file_keys.add(file_key)

    if pending_request_files:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            _prepend_file_context_to_message(message, pending_request_files)
            break

    return messages


async def chat_image_generation_handler(
    request: Request, form_data: dict, extra_params: dict, user
):
    metadata = extra_params.get("__metadata__", {})
    chat_id = metadata.get("chat_id", None)
    __event_emitter__ = extra_params.get("__event_emitter__", None)

    if not chat_id or not isinstance(chat_id, str) or not __event_emitter__:
        return form_data

    if chat_id.startswith("local:"):
        message_list = form_data.get("messages", [])
    else:
        chat = Chats.get_chat_by_id_and_user_id(chat_id, user.id)
        await __event_emitter__(
            {
                "type": "status",
                "data": {"description": "Creating image", "done": False},
            }
        )

        messages_map = chat.chat.get("history", {}).get("messages", {})
        message_id = chat.chat.get("history", {}).get("currentId")
        message_list = get_message_list(messages_map, message_id)

    user_message = get_last_user_message(message_list)

    prompt = user_message
    message_images = get_images_from_messages(message_list)

    # Limit to first 2 sets of images
    # We may want to change this in the future to allow more images
    input_images = []
    for idx, images in enumerate(message_images):
        if idx >= 2:
            break
        for image in images:
            input_images.append(image)

    system_message_content = ""

    if len(input_images) > 0 and request.app.state.config.ENABLE_IMAGE_EDIT:
        # Edit image(s)
        try:
            images = await image_edits(
                request=request,
                form_data=EditImageForm(**{"prompt": prompt, "image": input_images}),
                metadata={
                    "chat_id": metadata.get("chat_id", None),
                    "message_id": metadata.get("message_id", None),
                },
                user=user,
            )

            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": "Image created", "done": True},
                }
            )

            await __event_emitter__(
                {
                    "type": "files",
                    "data": {
                        "files": [
                            {
                                "type": "image",
                                "url": image["url"],
                            }
                            for image in images
                        ]
                    },
                }
            )

            system_message_content = "<context>The requested image has been edited and created and is now being shown to the user. Let them know that it has been generated.</context>"
        except Exception as e:
            log.debug(e)

            error_message = ""
            if isinstance(e, HTTPException):
                if e.detail and isinstance(e.detail, dict):
                    error_message = e.detail.get("message", str(e.detail))
                else:
                    error_message = str(e.detail)

            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"An error occurred while generating an image",
                        "done": True,
                    },
                }
            )

            system_message_content = f"<context>Image generation was attempted but failed. The system is currently unable to generate the image. Tell the user that the following error occurred: {error_message}</context>"

    else:
        # Create image(s)
        if request.app.state.config.ENABLE_IMAGE_PROMPT_GENERATION:
            try:
                res = await generate_image_prompt(
                    request,
                    {
                        "model": form_data["model"],
                        "messages": form_data["messages"],
                        "chat_id": metadata.get("chat_id"),
                    },
                    user,
                )

                response = res["choices"][0]["message"]["content"]

                try:
                    bracket_start = response.find("{")
                    bracket_end = response.rfind("}") + 1

                    if bracket_start == -1 or bracket_end == -1:
                        raise Exception("No JSON object found in the response")

                    response = response[bracket_start:bracket_end]
                    response = json.loads(response)
                    prompt = response.get("prompt", [])
                except Exception as e:
                    prompt = user_message

            except Exception as e:
                log.exception(e)
                prompt = user_message

        try:
            images = await image_generations(
                request=request,
                form_data=CreateImageForm(**{"prompt": prompt}),
                metadata={
                    "chat_id": metadata.get("chat_id", None),
                    "message_id": metadata.get("message_id", None),
                },
                user=user,
            )

            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": "Image created", "done": True},
                }
            )

            await __event_emitter__(
                {
                    "type": "files",
                    "data": {
                        "files": [
                            {
                                "type": "image",
                                "url": image["url"],
                            }
                            for image in images
                        ]
                    },
                }
            )

            system_message_content = "<context>The requested image has been created by the system successfully and is now being shown to the user. Let the user know that the image they requested has been generated and is now shown in the chat.</context>"
        except Exception as e:
            log.debug(e)

            error_message = ""
            if isinstance(e, HTTPException):
                if e.detail and isinstance(e.detail, dict):
                    error_message = e.detail.get("message", str(e.detail))
                else:
                    error_message = str(e.detail)

            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"An error occurred while generating an image",
                        "done": True,
                    },
                }
            )

            system_message_content = f"<context>Image generation was attempted but failed because of an error. The system is currently unable to generate the image. Tell the user that the following error occurred: {error_message}</context>"

    if system_message_content:
        form_data["messages"] = add_or_update_system_message(
            system_message_content, form_data["messages"]
        )

    return form_data


async def chat_completion_files_handler(
    request: Request, body: dict, extra_params: dict, user: UserModel
) -> tuple[dict, dict[str, Any]]:
    __event_emitter__ = extra_params["__event_emitter__"]
    sources = []
    retrieval_diagnostics: list[dict[str, Any]] = []
    performed_retrieval = False
    retrieval_blocked_by_ambiguous_scope = False
    retrieval_blocked_by_strategy = False
    retrieval_blocked_by_profile_lock = False
    first_pass_profile_lock: dict[str, Any] = {}
    first_pass_strategy: dict[str, Any] = {}
    first_pass_inventory_context: dict[str, Any] = {}
    retrieval_engine_cutover: dict[str, Any] = {}
    active_scope_files: list[dict] = []
    active_retrieval_files: list[dict] = []

    metadata = body.setdefault("metadata", {})
    files = metadata.get("files", None)
    current_request_files = [
        copy.deepcopy(file_item)
        for file_item in (files or [])
        if isinstance(file_item, dict)
    ]

    chat_id = metadata.get("chat_id")
    parent_message_id = metadata.get("parent_message_id")
    stored_messages: list[dict] = []
    if isinstance(chat_id, str) and chat_id and not chat_id.startswith("local:"):
        stored_messages = _get_stored_chat_messages(chat_id, user, parent_message_id)
        if stored_messages:
            history_files = _collect_stored_message_files(stored_messages)
            if history_files:
                current_files = [
                    _apply_adaptive_focus_metadata(
                        file_item,
                        origin=(
                            str(file_item.get("focus_origin") or "").strip()
                            or _ADAPTIVE_FOCUS_DERIVED
                        ),
                        tier=(
                            _normalize_adaptive_focus_tier(file_item.get("focus_tier"))
                            or _ADAPTIVE_FOCUS_REFERENCE
                        ),
                    )
                    for file_item in (files or [])
                    if isinstance(file_item, dict)
                ]
                files = _dedupe_file_context_items([*current_files, *history_files])
                metadata["files"] = files
                if body.get("files") is not None:
                    body["files"] = files
                if body.get("attachments") is not None:
                    body["attachments"] = files

    if files:
        (
            files,
            active_inline_sources,
            reference_inline_sources,
            active_retrieval_files,
            reference_retrieval_files,
        ) = await _prepare_chat_files_for_retrieval(request, files, user)
        body.setdefault("metadata", {})["files"] = files
        if body.get("files") is not None:
            body["files"] = files
        if body.get("attachments") is not None:
            body["attachments"] = files

        queries_cache: Optional[list[str]] = None
        query_status_emitted = False
        retrieval_returned_candidates = False
        retrieval_compatibility_hybrid_events: list[str] = []
        retrieval_blocked_by_ambiguous_scope = False
        retrieval_blocked_by_strategy = False
        retrieval_blocked_by_profile_lock = False
        prompt = _strip_attached_file_context(
            get_last_user_message(body["messages"]) or ""
        )
        active_scope_files = _active_selected_source_candidates(files)
        (
            active_source_scope,
            resolved_active_scope_files,
            active_scope_ambiguous,
        ) = _resolve_active_source_scope(
            prompt,
            active_scope_files,
            stored_messages=stored_messages,
            current_files=current_request_files,
            metadata=metadata,
            fallback_candidates=[
                file_item
                for file_item in files
                if isinstance(file_item, dict) and not _is_media_file_item(file_item)
            ],
        )
        if active_source_scope:
            metadata["active_source_scope"] = active_source_scope

        if (
            resolved_active_scope_files
            and len(resolved_active_scope_files) < len(active_scope_files)
        ):
            active_scope_files = resolved_active_scope_files
            active_inline_sources = _filter_inline_sources_for_selected_files(
                active_inline_sources, active_scope_files
            )
            active_retrieval_files = _filter_files_for_selected_files(
                active_retrieval_files, active_scope_files
            )

        if (
            not active_scope_ambiguous
            and len(active_scope_files) > 1
            and not _prompt_explicitly_requests_multiple_sources(prompt)
        ):
            anchored_active_files = _selected_source_anchor_matches(
                prompt, active_scope_files
            )
            if 0 < len(anchored_active_files) < len(active_scope_files):
                active_inline_sources = _filter_inline_sources_for_selected_files(
                    active_inline_sources, anchored_active_files
                )
                active_retrieval_files = _filter_files_for_selected_files(
                    active_retrieval_files, anchored_active_files
                )

        engine_selected_files = _build_selected_source_engine_candidates(
            active_scope_files=active_scope_files,
            active_retrieval_files=active_retrieval_files,
            active_inline_sources=active_inline_sources,
            user=user,
        )
        retrieval_engine_cutover = (
            _retrieval_engine_selected_source_lane_package(
                prompt=prompt,
                selected_files=[
                    item for item in engine_selected_files if isinstance(item, dict)
                ],
                active_source_scope=metadata.get("active_source_scope"),
                legacy_status="pending",
                legacy_terminal_reason="pending",
                legacy_sources=[],
                legacy_references=[],
                legacy_accepted_outputs=[],
            )
            or {}
            if not active_scope_ambiguous
            else {}
        )
        engine_authority_state = (
            (retrieval_engine_cutover.get("authority") or {}).get("state")
            if isinstance(retrieval_engine_cutover.get("authority"), dict)
            else ""
        )
        engine_contract = (
            retrieval_engine_cutover.get("contract")
            if isinstance(retrieval_engine_cutover.get("contract"), dict)
            else {}
        )
        engine_authority_bypasses_middleware = engine_authority_state in {
            "authoritative",
            "fail_closed",
            "diagnostics",
        }
        if engine_authority_bypasses_middleware:
            first_pass_strategy = _retrieval_engine_bypassed_first_pass_strategy(
                engine_contract=engine_contract,
                authority_state=engine_authority_state,
            )
            metadata["first_pass_retrieval_strategy"] = first_pass_strategy
            metadata["first_pass_profile_lock"] = {}
        else:
            first_pass_strategy = _first_pass_selected_source_strategy(
                prompt=prompt,
                body=body,
                metadata=metadata,
                retrieval_candidates=active_retrieval_files,
                active_source_scope=metadata.get("active_source_scope"),
            )
            existing_targeted_request = (
                metadata.get("selected_source_next_targeted_chunk_request")
                if isinstance(metadata, dict)
                else {}
            )
            if (
                first_pass_strategy.get("metadata_first_intent")
                and not first_pass_strategy.get("targeted_context_bounded")
                and not (
                    isinstance(existing_targeted_request, dict)
                    and existing_targeted_request
                )
            ):
                first_pass_inventory_context = await _build_first_pass_inventory_targeted_request(
                    query=prompt,
                    active_scope_files=active_scope_files,
                    strategy=first_pass_strategy,
                    request=request,
                    user=user,
                )
                if first_pass_inventory_context:
                    metadata["selected_source_next_targeted_chunk_request"] = {
                        "query": str(first_pass_inventory_context.get("query") or ""),
                        "source_ids": list(first_pass_inventory_context.get("source_ids") or []),
                        "required_anchors": list(
                            first_pass_inventory_context.get("required_anchors") or []
                        ),
                        "evidence_need": str(
                            first_pass_inventory_context.get("evidence_need") or ""
                        ),
                    }
                    metadata["selected_source_inventory_context"] = {
                        "inventory_count": int(
                            first_pass_inventory_context.get("inventory_count") or 0
                        ),
                        "shortlist_count": int(
                            first_pass_inventory_context.get("shortlist_count") or 0
                        ),
                        "ordering_basis": str(
                            first_pass_inventory_context.get("ordering_basis") or ""
                        ),
                        "source_ids": list(
                            first_pass_inventory_context.get("source_ids") or []
                        ),
                    }
                    first_pass_strategy = _first_pass_selected_source_strategy(
                        prompt=prompt,
                        body=body,
                        metadata=metadata,
                        retrieval_candidates=active_retrieval_files,
                        active_source_scope=metadata.get("active_source_scope"),
                    )
                    first_pass_strategy["reason_codes"] = list(
                        dict.fromkeys(
                            [
                                *(
                                    first_pass_strategy.get("reason_codes")
                                    if isinstance(first_pass_strategy.get("reason_codes"), list)
                                    else []
                                ),
                                "inventory_plan_targeted_context",
                            ]
                        )
                    )
            metadata["first_pass_retrieval_strategy"] = first_pass_strategy
            first_pass_profile_lock = _first_pass_profile_lock_incompatibility(
                metadata=metadata,
                strategy=first_pass_strategy,
            )
            if first_pass_profile_lock.get("incompatible"):
                first_pass_strategy["reason_codes"] = list(
                    dict.fromkeys(
                        [
                            *(
                                first_pass_strategy.get("reason_codes")
                                if isinstance(first_pass_strategy.get("reason_codes"), list)
                                else []
                            ),
                            *(
                                first_pass_profile_lock.get("reason_codes")
                                if isinstance(first_pass_profile_lock.get("reason_codes"), list)
                                else []
                            ),
                        ]
                    )
                )
            metadata["first_pass_profile_lock"] = first_pass_profile_lock

        async def ensure_queries(retrieval_candidates: list[dict]) -> list[str]:
            nonlocal queries_cache, query_status_emitted, retrieval_diagnostics

            if queries_cache is not None:
                return queries_cache

            queries_cache = []
            original_query = get_last_user_message(body["messages"])
            explicit_research = _is_explicit_research_retrieval(body)
            allow_query_variants = (
                explicit_research
                or _prompt_has_explicit_multi_part_retrieval_request(original_query)
            )
            all_full_context = bool(retrieval_candidates) and all(
                item.get("context") == "full" for item in retrieval_candidates
            )

            if retrieval_candidates and _retrieval_scope_is_ambiguous(
                original_query, retrieval_candidates
            ):
                _append_retrieval_diagnostic_once(
                    retrieval_diagnostics,
                    _safe_retrieval_diagnostic(
                        classification="diagnostics",
                        reason="ambiguous_retrieval_scope",
                        candidate_index=-1,
                        chunk_total=len(retrieval_candidates),
                    )
                )
            elif retrieval_candidates and not all_full_context and allow_query_variants:
                try:
                    queries_response = await generate_queries(
                        request,
                        {
                            "model": body["model"],
                            "messages": body["messages"],
                            "type": "retrieval",
                            "chat_id": body.get("metadata", {}).get("chat_id"),
                        },
                        user,
                    )
                    queries_response = queries_response["choices"][0]["message"][
                        "content"
                    ]

                    try:
                        bracket_start = queries_response.find("{")
                        bracket_end = queries_response.rfind("}") + 1

                        if bracket_start == -1 or bracket_end == -1:
                            raise Exception("No JSON object found in the response")

                        queries_response = queries_response[bracket_start:bracket_end]
                        queries_response = json.loads(queries_response)
                    except Exception:
                        queries_response = {"queries": [queries_response]}

                    queries_cache = _constrain_retrieval_queries(
                        original_query=original_query,
                        generated_queries=queries_response.get("queries", []),
                        retrieval_candidates=retrieval_candidates,
                        explicit_research=explicit_research,
                        active_source_scope=metadata.get("active_source_scope"),
                    )
                except Exception:
                    queries_cache = []

            if (
                retrieval_candidates
                and len(queries_cache) == 0
                and not _retrieval_scope_is_ambiguous(original_query, retrieval_candidates)
            ):
                queries_cache = _constrain_retrieval_queries(
                    original_query=original_query,
                    generated_queries=[],
                    retrieval_candidates=retrieval_candidates,
                    explicit_research=explicit_research,
                    active_source_scope=metadata.get("active_source_scope"),
                )

            if not query_status_emitted and retrieval_candidates:
                query_status_emitted = True
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "action": "queries_generated",
                            "queries": queries_cache,
                            "done": False,
                        },
                    }
                )

            return queries_cache

        async def retrieve_sources(retrieval_candidates: list[dict]) -> list[dict]:
            nonlocal performed_retrieval, retrieval_returned_candidates
            nonlocal retrieval_blocked_by_ambiguous_scope, retrieval_blocked_by_strategy
            nonlocal retrieval_diagnostics
            nonlocal retrieval_compatibility_hybrid_events

            if not retrieval_candidates:
                return []

            performed_retrieval = True
            original_query = get_last_user_message(body["messages"])
            if _retrieval_scope_is_ambiguous(original_query, retrieval_candidates):
                retrieval_blocked_by_ambiguous_scope = True
                _append_retrieval_diagnostic_once(
                    retrieval_diagnostics,
                    _safe_retrieval_diagnostic(
                        classification="diagnostics",
                        reason="ambiguous_retrieval_scope",
                        candidate_index=-1,
                        chunk_total=len(retrieval_candidates),
                    ),
                )
                return []

            if (
                first_pass_strategy.get("metadata_first_intent")
                and not first_pass_strategy.get("semantic_chunk_lookup_ok")
            ):
                retrieval_blocked_by_strategy = True
                _append_retrieval_diagnostic_once(
                    retrieval_diagnostics,
                    {
                        "kind": "retrieval_quality",
                        "classification": "no_evidence",
                        "reason": "metadata_first_targeted_evidence_required",
                        "candidate_index": -1,
                        "chunk_total": len(retrieval_candidates),
                        "usable_chunk_total": 0,
                        "outcome": "blocked",
                        "detail": {
                            "strategy_used": {
                                "evidence_need": str(
                                    first_pass_strategy.get("evidence_need") or "none"
                                ),
                                "retrieval_strategy": str(
                                    first_pass_strategy.get("retrieval_strategy")
                                    or "semantic_chunks"
                                ),
                                "semantic_chunk_lookup_ok": bool(
                                    first_pass_strategy.get("semantic_chunk_lookup_ok")
                                ),
                                "targeted_context_present": bool(
                                    first_pass_strategy.get("targeted_context_present")
                                ),
                                "targeted_context_bounded": bool(
                                    first_pass_strategy.get("targeted_context_bounded")
                                ),
                                "reason_codes": [
                                    str(value)
                                    for value in first_pass_strategy.get("reason_codes")
                                    or []
                                    if str(value).strip()
                                ],
                            }
                        },
                    },
                )
                return []

            ensure_retrieval_runtime(request.app)
            queries = await ensure_queries(retrieval_candidates)
            all_full_context = bool(retrieval_candidates) and all(
                item.get("context") == "full" for item in retrieval_candidates
            )
            if not all_full_context and not queries:
                return []

            try:
                retrieved_sources = await get_sources_from_items(
                    request=request,
                    items=retrieval_candidates,
                    queries=queries,
                    embedding_function=lambda query, prefix: request.app.state.EMBEDDING_FUNCTION(
                        query, prefix=prefix, user=user
                    ),
                    k=request.app.state.config.TOP_K,
                    reranking_function=(
                        (
                            lambda query, documents: request.app.state.RERANKING_FUNCTION(
                                query, documents, user=user
                            )
                        )
                        if request.app.state.RERANKING_FUNCTION
                        else None
                    ),
                    k_reranker=request.app.state.config.TOP_K_RERANKER,
                    r=request.app.state.config.RELEVANCE_THRESHOLD,
                    hybrid_bm25_weight=request.app.state.config.HYBRID_BM25_WEIGHT,
                    hybrid_search=request.app.state.config.ENABLE_RAG_HYBRID_SEARCH,
                    full_context=all_full_context
                    or request.app.state.config.RAG_FULL_CONTEXT,
                    user=user,
                )
                compatibility_provenance = getattr(
                    retrieved_sources, "compatibility_provenance", {}
                )
                if isinstance(compatibility_provenance, dict):
                    for event in compatibility_provenance.get("events", []) or []:
                        event_text = str(event or "").strip()
                        if (
                            event_text
                            and event_text not in retrieval_compatibility_hybrid_events
                        ):
                            retrieval_compatibility_hybrid_events.append(event_text)
                compatibility_diagnostics = getattr(
                    retrieved_sources, "compatibility_diagnostics", []
                )
                for diagnostic in compatibility_diagnostics or []:
                    if isinstance(diagnostic, dict):
                        _append_retrieval_diagnostic_once(
                            retrieval_diagnostics,
                            diagnostic,
                        )
                if retrieved_sources:
                    retrieval_returned_candidates = True
                return retrieved_sources
            except Exception as exc:
                log.exception(exc)
                timeout_types = (TimeoutError, asyncio.TimeoutError)
                _append_retrieval_diagnostic_once(
                    retrieval_diagnostics,
                    _safe_retrieval_diagnostic(
                        classification="diagnostics",
                        reason=(
                            "retrieval_timeout"
                            if isinstance(exc, timeout_types)
                            else "retrieval_provider_error"
                        ),
                        candidate_index=-1,
                        chunk_total=len(retrieval_candidates),
                        outcome=(
                            "timeout"
                            if isinstance(exc, timeout_types)
                            else "malformed"
                        ),
                    ),
                )
                return []

        if first_pass_profile_lock.get("incompatible"):
            performed_retrieval = True
            retrieval_blocked_by_profile_lock = True
            profile_lock_diagnostic = _safe_retrieval_diagnostic(
                classification="no_evidence",
                reason="selected_source_intent_profile_locked",
                candidate_index=-1,
                chunk_total=len(active_scope_files),
                outcome="blocked",
            )
            profile_lock_diagnostic["detail"] = {
                "non_promotion_reason": str(
                    first_pass_profile_lock.get("non_promotion_reason")
                    or "explicit_incompatible_profile"
                ),
                "classified_evidence_need": str(
                    first_pass_profile_lock.get("classified_evidence_need")
                    or "none"
                ),
                "resolved_execution_profile": str(
                    first_pass_profile_lock.get("resolved_execution_profile")
                    or "general"
                ),
            }
            _append_retrieval_diagnostic_once(
                retrieval_diagnostics,
                profile_lock_diagnostic,
            )
            active_sources = []
        elif active_scope_ambiguous:
            performed_retrieval = True
            retrieval_blocked_by_ambiguous_scope = True
            _append_retrieval_diagnostic_once(
                retrieval_diagnostics,
                _safe_retrieval_diagnostic(
                    classification="diagnostics",
                    reason="ambiguous_retrieval_scope",
                    candidate_index=-1,
                    chunk_total=len(active_scope_files),
                ),
            )
            active_sources: list[dict] = []
        else:
            active_sources = [*active_inline_sources]
            if engine_authority_state == "authoritative":
                performed_retrieval = True
                retrieval_returned_candidates = True
                active_sources.extend(
                    item for item in engine_contract.get("sources", []) if isinstance(item, dict)
                )
            elif engine_authority_state == "fail_closed":
                performed_retrieval = True
                retrieval_blocked_by_strategy = True
                active_sources = []
                for diagnostic in engine_contract.get("diagnostics", []):
                    if isinstance(diagnostic, dict):
                        _append_retrieval_diagnostic_once(
                            retrieval_diagnostics,
                            diagnostic,
                        )
            else:
                active_sources.extend(await retrieve_sources(active_retrieval_files))
            inventory_sources = (
                first_pass_inventory_context.get("inventory_sources")
                if isinstance(first_pass_inventory_context, dict)
                else []
            )
            if (
                first_pass_strategy.get("metadata_first_intent")
                and isinstance(inventory_sources, list)
                and inventory_sources
                and not _has_usable_sources(active_sources)
            ):
                active_sources.extend(
                    item for item in inventory_sources if isinstance(item, dict)
                )
                retrieval_returned_candidates = True
            sources.extend(active_sources)

            request_prior_attachments = _prompt_requests_prior_attachments(prompt)

            referenced_inline_sources: list[dict] = []
            referenced_retrieval_files: list[dict] = []
            remaining_inline_sources: list[dict] = []
            remaining_retrieval_files: list[dict] = []

            for file_item, inline_source in reference_inline_sources:
                if request_prior_attachments or _prompt_mentions_reference_file(prompt, file_item):
                    referenced_inline_sources.append(inline_source)
                else:
                    remaining_inline_sources.append(inline_source)

            for file_item in reference_retrieval_files:
                if request_prior_attachments or _prompt_mentions_reference_file(prompt, file_item):
                    referenced_retrieval_files.append(file_item)
                else:
                    remaining_retrieval_files.append(file_item)

            if referenced_inline_sources:
                sources.extend(referenced_inline_sources)

            if referenced_retrieval_files:
                sources.extend(await retrieve_sources(referenced_retrieval_files))

            if not _has_usable_sources(active_sources):
                if remaining_inline_sources:
                    sources.extend(remaining_inline_sources)
                if remaining_retrieval_files:
                    sources.extend(await retrieve_sources(remaining_retrieval_files))

        query_generation_diagnostics = list(retrieval_diagnostics)
        sources, gate_diagnostics = _gate_retrieval_sources(
            sources,
            active_source_scope=metadata.get("active_source_scope"),
            relevance_threshold=request.app.state.config.RELEVANCE_THRESHOLD,
        )
        retrieval_diagnostics = query_generation_diagnostics
        for diagnostic in gate_diagnostics:
            _append_retrieval_diagnostic_once(retrieval_diagnostics, diagnostic)

        if (
            performed_retrieval
            and not retrieval_returned_candidates
            and not retrieval_blocked_by_ambiguous_scope
            and not retrieval_blocked_by_strategy
            and not retrieval_blocked_by_profile_lock
        ):
            _append_retrieval_diagnostic_once(
                retrieval_diagnostics,
                _safe_retrieval_diagnostic(
                    classification="no_evidence",
                    reason="no_retrieval_candidates",
                    candidate_index=-1,
                ),
            )

        log.debug(f"rag_contexts:sources: {sources}")
        if retrieval_diagnostics:
            log.debug(f"rag_contexts:retrieval_diagnostics: {retrieval_diagnostics}")

        unique_ids = set()
        for source in sources or []:
            if not source or len(source.keys()) == 0:
                continue

            documents = source.get("document") or []
            metadatas = source.get("metadata") or []
            src_info = source.get("source") or {}

            for index, _ in enumerate(documents):
                source_metadata = metadatas[index] if index < len(metadatas) else None
                _id = (
                    (source_metadata or {}).get("source")
                    or (src_info or {}).get("id")
                    or "N/A"
                )
                unique_ids.add(_id)

        if performed_retrieval:
            sources_count = len(unique_ids)
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "action": "sources_retrieved",
                        "count": sources_count,
                        "hidden": sources_count == 0,
                        "done": True,
                    },
                }
            )

    observe_llm_event(
        "retrieval.first_pass",
        {
            "retrieval_attempted": performed_retrieval,
            "source_count": len(sources or []),
            "diagnostic_count": len(retrieval_diagnostics),
            "reason_codes": diagnostic_reason_codes(retrieval_diagnostics),
            "classification_counts": diagnostic_classification_counts(
                retrieval_diagnostics
            ),
            "strategy_used": first_pass_strategy if files else {},
            "active_source_scope": summarize_source_scope(
                metadata.get("active_source_scope")
            ),
            "no_evidence": performed_retrieval and not sources,
        },
    )

    prompt_text = _normalize_retrieval_query_text(
        _strip_attached_file_context(get_last_user_message(body["messages"]) or "")
    )
    engine_contract = (
        retrieval_engine_cutover.get("contract")
        if isinstance(retrieval_engine_cutover.get("contract"), dict)
        else {}
    )
    engine_authority = (
        (retrieval_engine_cutover.get("authority") or {}).get("state")
        if isinstance(retrieval_engine_cutover.get("authority"), dict)
        else ""
    )
    structured_terms: dict[str, list[str]] = {
        "document": [],
        "evidence_facets": [],
        "topic_terms": [],
        "requested_types": [],
        "date_basis": [],
        "type_basis": [],
        "topic_basis": [],
    }
    inventory_count = 0
    shortlist_count = 0
    if engine_authority in {"authoritative", "fail_closed", "diagnostics"}:
        references = [
            item for item in engine_contract.get("references", []) if isinstance(item, dict)
        ]
        accepted_outputs = [
            item
            for item in engine_contract.get("accepted_outputs", [])
            if isinstance(item, dict)
        ]
        sources = [
            item for item in engine_contract.get("sources", []) if isinstance(item, dict)
        ]
        for diagnostic in engine_contract.get("diagnostics", []):
            if isinstance(diagnostic, dict):
                _append_retrieval_diagnostic_once(retrieval_diagnostics, diagnostic)
        unsupported_claim_reason = ""
    else:
        targeted_request = (
            metadata.get("selected_source_next_targeted_chunk_request")
            if isinstance(metadata, dict)
            else {}
        )
        targeted_request = targeted_request if isinstance(targeted_request, dict) else {}
        targeted_source_ids = _list_from_optional_strings(targeted_request.get("source_ids"))
        if not targeted_source_ids:
            single_targeted_source_id = str(targeted_request.get("source_id") or "").strip()
            if single_targeted_source_id:
                targeted_source_ids = [single_targeted_source_id]
        required_anchors = _list_from_optional_strings(
            targeted_request.get("required_anchors")
        )
        structured_terms = _first_pass_structured_terms(
            _normalize_retrieval_query_text(targeted_request.get("query") or prompt_text)
        )
        inventory_count = int(
            (first_pass_inventory_context.get("inventory_count") or 0)
            if isinstance(first_pass_inventory_context, dict)
            else 0
        )
        shortlist_count = int(
            (first_pass_inventory_context.get("shortlist_count") or 0)
            if isinstance(first_pass_inventory_context, dict)
            else 0
        )
        if inventory_count <= 0:
            inventory_count = len(active_retrieval_files)
        if shortlist_count <= 0:
            shortlist_count = len(targeted_source_ids)
        required_facets = list(
            dict.fromkeys(
                [
                    *structured_terms.get("evidence_facets", []),
                    *structured_terms.get("topic_terms", []),
                    *structured_terms.get("requested_types", []),
                ]
            )
        )
        references, metadata_first_gate_diagnostics = _first_pass_selected_source_references(
            sources,
            strategy=first_pass_strategy,
            targeted_source_ids=targeted_source_ids,
            required_anchors=required_anchors,
            required_facets=required_facets,
            prompt=prompt_text,
            retrieval_round=1,
        )
        for diagnostic in metadata_first_gate_diagnostics:
            _append_retrieval_diagnostic_once(retrieval_diagnostics, diagnostic)
        for diagnostic in _first_pass_metadata_first_diagnostics(
            query=prompt_text,
            retrieval_round=1,
            strategy=first_pass_strategy,
            inventory_count=inventory_count,
            shortlist_count=shortlist_count,
            structured_terms=structured_terms,
        ):
            _append_retrieval_diagnostic_once(retrieval_diagnostics, diagnostic)
        unsupported_claim_reason = next(
            (
                str(item.get("reason") or "").strip()
                for item in retrieval_diagnostics
                if isinstance(item, dict)
                and str(item.get("reason") or "").strip().startswith("unsupported_")
            ),
            "",
        )
        if first_pass_strategy.get("metadata_first_intent") and unsupported_claim_reason:
            references = []
        accepted_outputs = _first_pass_selected_source_accepted_outputs(references)
    active_scope = metadata.get("active_source_scope")
    active_scope_state = (
        str(active_scope.get("status") or "").strip().lower()
        if isinstance(active_scope, dict)
        else "none"
    )
    status = (
        "success"
        if accepted_outputs
        else (
            "blocked"
            if (
                retrieval_blocked_by_ambiguous_scope
                or retrieval_blocked_by_strategy
                or retrieval_blocked_by_profile_lock
            )
            else ("no_evidence" if performed_retrieval else "blocked")
        )
    )
    terminal_reason = "success"
    if status == "blocked":
        if retrieval_blocked_by_profile_lock:
            terminal_reason = "selected_source_intent_profile_locked"
        elif retrieval_blocked_by_strategy:
            terminal_reason = "metadata_first_targeted_evidence_required"
        elif retrieval_blocked_by_ambiguous_scope:
            terminal_reason = "ambiguous_retrieval_scope"
        else:
            terminal_reason = "source_scope_blocked"
    elif status == "no_evidence":
        terminal_reason = "no_retrieval_candidates"
    if unsupported_claim_reason and status != "blocked":
        status = "no_evidence"
        terminal_reason = unsupported_claim_reason
    if engine_authority in {"authoritative", "fail_closed", "diagnostics"}:
        status = str(engine_contract.get("status") or status)
        terminal_reason = str(engine_contract.get("terminal_reason") or terminal_reason)
    metadata_first_diagnostics_only = bool(
        first_pass_strategy.get("metadata_first_intent")
        and status in {"blocked", "no_evidence"}
        and not accepted_outputs
        and not references
    )
    if metadata_first_diagnostics_only:
        # Keep first-pass metadata/listing turns diagnostics-only: no raw source
        # context should be returned for prompt injection or source cards.
        sources = []

    timeout_seconds = getattr(
        request.app.state.config,
        "SELECTED_SOURCE_RETRIEVAL_TIMEOUT_SECONDS",
        None,
    )
    try:
        timeout_value = float(timeout_seconds)
    except Exception:
        timeout_value = None
    retry_policy = {
        "max_retries": 0,
        "retries_attempted": 0,
        "retry_allowed": False,
        "timeout_seconds": timeout_value,
    }
    first_pass_runtime_mode = _retrieval_engine_runtime_mode(engine_authority)
    first_pass_contract = {
        "accepted_outputs": accepted_outputs,
        "references": references,
        "diagnostics": [item for item in retrieval_diagnostics if isinstance(item, dict)],
        "status": status,
        "authorization_context": {
            "active_source_scope_state": active_scope_state or "none",
            "active_source_scope": summarize_source_scope(active_scope),
        },
        "retry_policy": retry_policy,
        "terminal_reason": terminal_reason,
        "provenance": {
            "worker_kind": "selected_source_retrieval",
            "tool_name": "first_pass_selected_source_retrieval",
            "status": status,
            "terminal_reason": terminal_reason,
            "strategy_used": first_pass_strategy if files else {},
            "selected_source_runtime_mode": first_pass_runtime_mode,
            "engine_owned": first_pass_runtime_mode != "compatibility_fallback",
            "compatibility_fallback": first_pass_runtime_mode == "compatibility_fallback",
            "compatibility_boundary": (
                "legacy_selected_source_fallback_until_10_5"
                if first_pass_runtime_mode == "compatibility_fallback"
                else ""
            ),
            "compatibility_hybrid_events": list(
                retrieval_compatibility_hybrid_events
            ),
            "profile_lock": (
                first_pass_profile_lock
                if isinstance(first_pass_profile_lock, dict)
                and first_pass_profile_lock.get("incompatible")
                else {}
            ),
            "reason_codes": diagnostic_reason_codes(retrieval_diagnostics),
            "classification_counts": diagnostic_classification_counts(
                retrieval_diagnostics
            ),
            "retrieval_attempted": performed_retrieval,
            "no_evidence": performed_retrieval and not accepted_outputs,
            "compact_retrieval_provenance": {
                # Persist a bounded strategy summary for QA without raw provider payloads.
                "strategy": {
                    "evidence_need": str(
                        first_pass_strategy.get("evidence_need") or "none"
                    ),
                    "retrieval_strategy": str(
                        first_pass_strategy.get("retrieval_strategy")
                        or "semantic_chunks"
                    ),
                    "semantic_chunk_lookup_ok": bool(
                        first_pass_strategy.get("semantic_chunk_lookup_ok")
                    ),
                },
                "inventory_counts": {
                    "selected_inventory_count": int(max(inventory_count, 0)),
                    "scoped_inventory_count": int(
                        max(inventory_count, len(active_retrieval_files), 0)
                    ),
                    "shortlist_count": int(max(shortlist_count, 0)),
                },
                "accepted_counts": {
                    "reference_count": len(references),
                    "accepted_output_count": len(accepted_outputs),
                },
                "runtime_mode": first_pass_runtime_mode,
                "engine_owned": first_pass_runtime_mode != "compatibility_fallback",
                "engine_authority": engine_authority == "authoritative",
                "compatibility_fallback": first_pass_runtime_mode == "compatibility_fallback",
                "compatibility_boundary": (
                    "legacy_selected_source_fallback_until_10_5"
                    if first_pass_runtime_mode == "compatibility_fallback"
                    else ""
                ),
                "compatibility_hybrid_events": list(
                    retrieval_compatibility_hybrid_events
                ),
                "fallback_used": bool(
                    first_pass_strategy.get("metadata_first_intent")
                    and str(first_pass_strategy.get("retrieval_strategy") or "")
                    .strip()
                    .lower()
                    == "semantic_chunks"
                ),
                "basis": {
                    "date_basis": (
                        (
                            (structured_terms.get("date_basis") or [None])[0]
                            if (structured_terms.get("date_basis") or [None])
                            else None
                        )
                        or (
                            "structured_document_anchor"
                            if structured_terms.get("document")
                            else ""
                        )
                    ),
                    "type_basis": (
                        (
                            (structured_terms.get("type_basis") or [None])[0]
                            if (structured_terms.get("type_basis") or [None])
                            else None
                        )
                        or (
                            "structured_requested_types"
                            if structured_terms.get("requested_types")
                            else ""
                        )
                    ),
                    "topic_basis": (
                        (
                            (structured_terms.get("topic_basis") or [None])[0]
                            if (structured_terms.get("topic_basis") or [None])
                            else None
                        )
                        or (
                            "structured_topic_terms"
                            if structured_terms.get("topic_terms")
                            else ""
                        )
                    ),
                },
                "material_limitations": list(
                    dict.fromkeys(diagnostic_reason_codes(retrieval_diagnostics))
                ),
            },
        },
    }
    if engine_authority in {"authoritative", "fail_closed", "diagnostics"}:
        for key in (
            "accepted_outputs",
            "references",
            "diagnostics",
            "context_budget",
            "status",
            "authorization_context",
            "retry_policy",
            "terminal_reason",
            "provenance",
        ):
            if key in engine_contract:
                first_pass_contract[key] = engine_contract[key]
    retrieval_engine_observe = (
        retrieval_engine_cutover.get("observe")
        if isinstance(retrieval_engine_cutover.get("observe"), dict)
        else (
            _retrieval_engine_observe_selected_source_lane(
                prompt=prompt_text,
                selected_files=[
                    item
                    for item in [*active_scope_files, *active_retrieval_files]
                    if isinstance(item, dict)
                ],
                active_source_scope=metadata.get("active_source_scope"),
                legacy_status=first_pass_contract.get("status", status),
                legacy_terminal_reason=first_pass_contract.get(
                    "terminal_reason", terminal_reason
                ),
                legacy_sources=sources if isinstance(sources, list) else [],
                legacy_references=references,
                legacy_accepted_outputs=accepted_outputs,
            )
            if files
            else None
        )
    )
    if isinstance(retrieval_engine_observe, dict):
        counts = retrieval_engine_observe.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        retrieval_engine_observe["comparison"] = _retrieval_engine_observe_comparison(
            engine_status=str(retrieval_engine_observe.get("status") or ""),
            engine_terminal_reason=str(
                retrieval_engine_observe.get("terminal_reason") or ""
            ),
            engine_accepted_output_count=int(counts.get("accepted_output_count") or 0),
            engine_reference_count=int(counts.get("reference_count") or 0),
            legacy_status=str(first_pass_contract.get("status") or status),
            legacy_terminal_reason=str(
                first_pass_contract.get("terminal_reason") or terminal_reason
            ),
            legacy_sources=sources if isinstance(sources, list) else [],
            legacy_references=references,
            legacy_accepted_outputs=accepted_outputs,
        )
        retrieval_engine_observe = _retrieval_engine_observe_safe_value(
            retrieval_engine_observe
        )
    if retrieval_engine_observe:
        metadata["retrieval_engine_observe"] = retrieval_engine_observe
        plan_summary = retrieval_engine_observe.get("plan_summary")
        if isinstance(plan_summary, dict) and plan_summary:
            metadata["retrieval_engine_plan_summary"] = plan_summary

    return body, {
        "sources": sources,
        "retrieval_diagnostics": retrieval_diagnostics,
        "active_source_scope": metadata.get("active_source_scope"),
        "first_pass_retrieval_strategy": metadata.get("first_pass_retrieval_strategy"),
        "first_pass_profile_lock": metadata.get("first_pass_profile_lock"),
        "retrieval_attempted": performed_retrieval,
        "no_evidence": performed_retrieval and not sources,
        "retrieval_engine_observe": metadata.get("retrieval_engine_observe"),
        "retrieval_engine_plan_summary": metadata.get("retrieval_engine_plan_summary"),
        **first_pass_contract,
    }


def _retrieval_engine_observe_selected_source_lane(
    *,
    prompt: str,
    selected_files: list[dict],
    active_source_scope: Optional[dict],
    legacy_status: str,
    legacy_terminal_reason: str,
    legacy_sources: list[dict],
    legacy_references: list[dict],
    legacy_accepted_outputs: list[dict],
) -> Optional[dict[str, Any]]:
    package = _retrieval_engine_selected_source_lane_package(
        prompt=prompt,
        selected_files=selected_files,
        active_source_scope=active_source_scope,
        legacy_status=legacy_status,
        legacy_terminal_reason=legacy_terminal_reason,
        legacy_sources=legacy_sources,
        legacy_references=legacy_references,
        legacy_accepted_outputs=legacy_accepted_outputs,
    )
    observe = package.get("observe") if isinstance(package, dict) else None
    return observe if isinstance(observe, dict) else None


def _retrieval_engine_selected_source_lane_package(
    *,
    prompt: str,
    selected_files: list[dict],
    active_source_scope: Optional[dict],
    legacy_status: str,
    legacy_terminal_reason: str,
    legacy_sources: list[dict],
    legacy_references: list[dict],
    legacy_accepted_outputs: list[dict],
) -> Optional[dict[str, Any]]:
    """Run the selected-file text engine lane and keep authority separate from observe metadata."""

    if not selected_files:
        return None

    try:
        from open_webui.retrieval.engine_adapter import build_retrieval_engine_request
        from retrieval_engine import (
            InMemoryDocument,
            InMemoryExactTextConnector,
            InMemoryFullContextConnector,
            InMemoryKeywordConnector,
            RetrievalEngine,
        )
    except Exception as exc:
        observe = _retrieval_engine_observe_failure(
            code="retrieval_engine_observe_unavailable",
            reason="adapter_unavailable",
            exc=exc,
            legacy_status=legacy_status,
            legacy_terminal_reason=legacy_terminal_reason,
            legacy_sources=legacy_sources,
            legacy_references=legacy_references,
            legacy_accepted_outputs=legacy_accepted_outputs,
        )
        return {"observe": observe, "authority": {"state": "fallback", "reason": "adapter_unavailable"}}

    observed_files = _retrieval_engine_observe_file_descriptors(selected_files)
    selected_file_ids = tuple(
        str(file_item.get("id") or "").strip()
        for file_item in observed_files
        if str(file_item.get("id") or "").strip()
    )
    if not selected_file_ids:
        return None

    documents = tuple(
        InMemoryDocument(
            source_id=str(file_item["id"]),
            label=str(
                file_item.get("filename") or file_item.get("name") or file_item["id"]
            ),
            text=str((file_item.get("data") or {}).get("content") or ""),
            metadata={
                "canonical_text_available": True,
                "canonical_range_available": True,
            },
        )
        for file_item in observed_files
        if str((file_item.get("data") or {}).get("content") or "").strip()
    )
    if not documents:
        observe = _retrieval_engine_observe_unsupported(
            reason="unsupported_source_shape",
            legacy_status=legacy_status,
            legacy_terminal_reason=legacy_terminal_reason,
            legacy_sources=legacy_sources,
            legacy_references=legacy_references,
            legacy_accepted_outputs=legacy_accepted_outputs,
        )
        return {"observe": observe, "authority": {"state": "fallback", "reason": "unsupported_source_shape"}}

    try:
        adapter_result = build_retrieval_engine_request(
            query=prompt,
            selected_file_ids=selected_file_ids,
            selected_files=observed_files,
            active_source_scope=active_source_scope,
            authorization_generation="open_webui_selected_source_engine_v1",
            execution_context={
                "engine_version": "open_webui_selected_source_engine_v1",
                "ranking_policy": "selected_file_text_cutover",
                "budget_policy": "selected_source_first_pass",
                "return_policy": "accepted_evidence_only",
                "embedding_model_settings": {"provider": "in_memory_selected_file_text"},
                "reranker_model_settings": {"provider": "none"},
            },
        )
        connectors: list[Any] = list(adapter_result.connectors)
        connectors.extend(
            [
                InMemoryExactTextConnector(documents),
                InMemoryKeywordConnector(documents),
                InMemoryFullContextConnector(documents),
            ]
        )
        result = RetrievalEngine(connectors=tuple(connectors)).run(
            adapter_result.request
        )
    except Exception as exc:
        observe = _retrieval_engine_observe_failure(
            code="retrieval_engine_observe_failed",
            reason="engine_observe_error",
            exc=exc,
            legacy_status=legacy_status,
            legacy_terminal_reason=legacy_terminal_reason,
            legacy_sources=legacy_sources,
            legacy_references=legacy_references,
            legacy_accepted_outputs=legacy_accepted_outputs,
        )
        return {"observe": observe, "authority": {"state": "fallback", "reason": "engine_observe_error"}}

    diagnostics = [
        *_retrieval_engine_observe_diagnostics(adapter_result.diagnostics),
        *_retrieval_engine_observe_diagnostics(result.diagnostics),
    ]
    plan_summary = _retrieval_engine_observe_plan_summary(result.plan_summary)
    observe = _retrieval_engine_observe_from_result(
        result=result,
        diagnostics=diagnostics,
        plan_summary=plan_summary,
        legacy_status=legacy_status,
        legacy_terminal_reason=legacy_terminal_reason,
        legacy_sources=legacy_sources,
        legacy_references=legacy_references,
        legacy_accepted_outputs=legacy_accepted_outputs,
    )
    contract = _retrieval_engine_result_first_pass_contract(
        result=result,
        adapter_diagnostics=adapter_result.diagnostics,
        selected_files=observed_files,
        diagnostics=diagnostics,
        plan_summary=plan_summary,
    )
    authority_state = _retrieval_engine_authority_state(result)
    return {
        "observe": observe,
        "authority": {
            "state": authority_state,
            "reason": contract.get("terminal_reason") or result.terminal_reason or result.status,
        },
        "contract": contract,
    }


def _retrieval_engine_observe_from_result(
    *,
    result: Any,
    diagnostics: list[dict[str, Any]],
    plan_summary: Optional[dict[str, Any]],
    legacy_status: str,
    legacy_terminal_reason: str,
    legacy_sources: list[dict],
    legacy_references: list[dict],
    legacy_accepted_outputs: list[dict],
) -> dict[str, Any]:
    authority_state = _retrieval_engine_authority_state(result)
    observe = {
        "mode": "observe_parallel",
        "lane": "selected_file_text",
        "authority": (
            "retrieval_engine"
            if authority_state in {"authoritative", "fail_closed", "diagnostics"}
            else "legacy_selected_source"
        ),
        "status": str(getattr(result, "status", "") or ""),
        "terminal_reason": str(getattr(result, "terminal_reason", "") or "success"),
        "plan_summary": plan_summary,
        "counts": {
            "candidate_count": len(getattr(result, "candidates", ()) or ()),
            "evidence_bundle_count": len(getattr(result, "evidence_bundles", ()) or ()),
            "accepted_output_count": len(getattr(result, "accepted_outputs", ()) or ()),
            "reference_count": len(getattr(result, "references", ()) or ()),
            "diagnostic_count": len(diagnostics),
        },
        "comparison": _retrieval_engine_observe_comparison(
            engine_status=str(getattr(result, "status", "") or ""),
            engine_terminal_reason=str(
                getattr(result, "terminal_reason", "") or "success"
            ),
            engine_accepted_output_count=len(
                getattr(result, "accepted_outputs", ()) or ()
            ),
            engine_reference_count=len(getattr(result, "references", ()) or ()),
            legacy_status=legacy_status,
            legacy_terminal_reason=legacy_terminal_reason,
            legacy_sources=legacy_sources,
            legacy_references=legacy_references,
            legacy_accepted_outputs=legacy_accepted_outputs,
        ),
        "diagnostics": diagnostics[:12],
    }
    return _retrieval_engine_observe_safe_value(observe)


def _retrieval_engine_observe_unsupported(
    *,
    reason: str,
    legacy_status: str,
    legacy_terminal_reason: str,
    legacy_sources: list[dict],
    legacy_references: list[dict],
    legacy_accepted_outputs: list[dict],
) -> dict[str, Any]:
    observe = {
        "mode": "observe_parallel",
        "lane": "selected_file_text",
        "authority": "legacy_selected_source",
        "status": "no_evidence",
        "terminal_reason": reason,
        "plan_summary": None,
        "counts": {
            "candidate_count": 0,
            "evidence_bundle_count": 0,
            "accepted_output_count": 0,
            "reference_count": 0,
            "diagnostic_count": 1,
        },
        "comparison": _retrieval_engine_observe_comparison(
            engine_status="no_evidence",
            engine_terminal_reason=reason,
            engine_accepted_output_count=0,
            engine_reference_count=0,
            legacy_status=legacy_status,
            legacy_terminal_reason=legacy_terminal_reason,
            legacy_sources=legacy_sources,
            legacy_references=legacy_references,
            legacy_accepted_outputs=legacy_accepted_outputs,
        ),
        "diagnostics": [
            {
                "code": "retrieval_engine_unsupported_source_shape",
                "kind": "diagnostics",
                "severity": "info",
                "phase": "host_cutover",
                "details": {"reason": reason},
            }
        ],
    }
    return _retrieval_engine_observe_safe_value(observe)


def _retrieval_engine_observe_file_descriptors(
    selected_files: list[dict],
) -> list[dict]:
    observed: list[dict] = []
    seen: set[str] = set()
    for file_item in selected_files:
        if not isinstance(file_item, dict):
            continue
        file_id = str(file_item.get("id") or "").strip()
        if not file_id or file_id in seen:
            continue
        seen.add(file_id)
        meta = _retrieval_engine_observe_safe_mapping(file_item.get("meta"))
        content_type = (
            str(file_item.get("content_type") or meta.get("content_type") or "").strip()
            or None
        )
        descriptor: dict[str, Any] = {
            "id": file_id,
            "filename": str(
                file_item.get("filename") or file_item.get("name") or file_id
            ),
            "name": str(file_item.get("name") or file_item.get("filename") or file_id),
            "type": str(file_item.get("type") or "file"),
            "meta": meta,
        }
        if content_type:
            descriptor["content_type"] = content_type
        collection_name = str(file_item.get("collection_name") or "").strip()
        if collection_name:
            descriptor["collection_name"] = collection_name
        text_content = _retrieval_engine_observe_file_text(file_item)
        if text_content:
            descriptor["data"] = {"content": text_content}
        observed.append(descriptor)
    return observed


def _retrieval_engine_observe_file_text(file_item: dict) -> str:
    candidates: list[Any] = []
    data = file_item.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("content"))
    nested_file = file_item.get("file")
    if isinstance(nested_file, dict):
        nested_data = nested_file.get("data")
        if isinstance(nested_data, dict):
            candidates.append(nested_data.get("content"))
    candidates.extend([file_item.get("content"), file_item.get("text")])
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _retrieval_engine_observe_plan_summary(plan_summary: Any) -> Optional[dict[str, Any]]:
    if plan_summary is None:
        return None
    return {
        "evidence_shape": str(getattr(plan_summary, "evidence_shape", "") or ""),
        "required_capabilities": [
            str(getattr(capability, "value", capability))
            for capability in getattr(plan_summary, "required_capabilities", ()) or ()
        ],
        "optional_capabilities": [
            str(getattr(capability, "value", capability))
            for capability in getattr(plan_summary, "optional_capabilities", ()) or ()
        ],
        "source_ids": [
            str(source_id)
            for source_id in getattr(plan_summary, "source_ids", ()) or ()
            if str(source_id).strip()
        ],
        "source_count": int(getattr(plan_summary, "source_count", 0) or 0),
        "needs_metadata_inventory": bool(
            getattr(plan_summary, "needs_metadata_inventory", False)
        ),
        "needs_exact_anchor": bool(getattr(plan_summary, "needs_exact_anchor", False)),
        "needs_workbook_range": bool(
            getattr(plan_summary, "needs_workbook_range", False)
        ),
        "needs_multi_source_comparison": bool(
            getattr(plan_summary, "needs_multi_source_comparison", False)
        ),
        "needs_iterative_selected_source_retrieval": bool(
            getattr(plan_summary, "needs_iterative_selected_source_retrieval", False)
        ),
        "first_pass_evidence_state": (
            str(getattr(plan_summary, "first_pass_evidence_state", "") or "")
            or None
        ),
    }


def _retrieval_engine_observe_diagnostics(diagnostics: Any) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for diagnostic in diagnostics or ():
        if not diagnostic:
            continue
        compact.append(
            _retrieval_engine_observe_safe_value(
                {
                    "code": str(getattr(diagnostic, "code", "") or ""),
                    "kind": str(getattr(diagnostic, "kind", "") or ""),
                    "severity": str(getattr(diagnostic, "severity", "") or ""),
                    "phase": str(getattr(diagnostic, "phase", "") or ""),
                    "source_id": str(getattr(diagnostic, "source_id", "") or ""),
                    "candidate_id": str(getattr(diagnostic, "candidate_id", "") or ""),
                    "details": _retrieval_engine_observe_safe_mapping(
                        getattr(diagnostic, "details", {})
                    ),
                }
            )
        )
    return compact


def _retrieval_engine_observe_failure(
    *,
    code: str,
    reason: str,
    exc: Exception,
    legacy_status: str,
    legacy_terminal_reason: str,
    legacy_sources: list[dict],
    legacy_references: list[dict],
    legacy_accepted_outputs: list[dict],
) -> dict[str, Any]:
    observe = {
        "mode": "observe_parallel",
        "lane": "selected_file_text",
        "authority": "legacy_selected_source",
        "status": "error",
        "terminal_reason": reason,
        "plan_summary": None,
        "counts": {
            "candidate_count": 0,
            "evidence_bundle_count": 0,
            "accepted_output_count": 0,
            "reference_count": 0,
            "diagnostic_count": 1,
        },
        "comparison": _retrieval_engine_observe_comparison(
            engine_status="error",
            engine_terminal_reason=reason,
            engine_accepted_output_count=0,
            engine_reference_count=0,
            legacy_status=legacy_status,
            legacy_terminal_reason=legacy_terminal_reason,
            legacy_sources=legacy_sources,
            legacy_references=legacy_references,
            legacy_accepted_outputs=legacy_accepted_outputs,
        ),
        "diagnostics": [
            {
                "code": code,
                "kind": "diagnostics",
                "severity": "warning",
                "phase": "host_observe",
                "details": {"reason": reason, "exception_type": type(exc).__name__},
            }
        ],
    }
    return _retrieval_engine_observe_safe_value(observe)


def _retrieval_engine_observe_comparison(
    *,
    engine_status: str,
    engine_terminal_reason: str,
    engine_accepted_output_count: int,
    engine_reference_count: int,
    legacy_status: str,
    legacy_terminal_reason: str,
    legacy_sources: list[dict],
    legacy_references: list[dict],
    legacy_accepted_outputs: list[dict],
) -> dict[str, Any]:
    engine_is_authoritative = (
        str(engine_status or "").strip().lower() in {"success", "partial"}
        and int(engine_accepted_output_count) > 0
        and int(engine_reference_count) > 0
    )
    return {
        "user_visible_authority": (
            "retrieval_engine" if engine_is_authoritative else "legacy_selected_source"
        ),
        "engine_status": str(engine_status or ""),
        "engine_terminal_reason": str(engine_terminal_reason or ""),
        "engine_accepted_output_count": int(engine_accepted_output_count),
        "engine_reference_count": int(engine_reference_count),
        "legacy_status": str(legacy_status or ""),
        "legacy_terminal_reason": str(legacy_terminal_reason or ""),
        "legacy_source_count": len(legacy_sources or []),
        "legacy_reference_count": len(legacy_references or []),
        "legacy_accepted_output_count": len(legacy_accepted_outputs or []),
    }


def _is_media_file_item(file_item: Any) -> bool:
    if not isinstance(file_item, dict):
        return False

    file_type = str(file_item.get("type") or "").strip().lower()
    if file_type in {"image", "audio", "video"}:
        return True

    file_meta = file_item.get("meta")
    if not isinstance(file_meta, dict):
        file_meta = {}
    content_type = str(
        file_item.get("content_type")
        or file_meta.get("content_type")
        or ""
    ).strip().lower()
    return content_type.startswith(("image/", "audio/", "video/"))


def _is_image_file_item(file_item: Any) -> bool:
    if not isinstance(file_item, dict):
        return False

    file_type = str(file_item.get("type") or "").strip().lower()
    if file_type == "image":
        return True

    file_meta = file_item.get("meta")
    if not isinstance(file_meta, dict):
        file_meta = {}
    content_type = str(
        file_item.get("content_type")
        or file_meta.get("content_type")
        or ""
    ).strip().lower()
    return content_type.startswith("image/")


def _resolve_chat_file_url(file_item: Any) -> str:
    if not isinstance(file_item, dict):
        return ""

    for key in (
        "url",
        "bridge_url",
        "generated_file_url",
        "download_url",
        "downloadUrl",
    ):
        value = file_item.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or normalized.lower() in {"null", "undefined"}:
            continue
        if normalized.startswith(("http://", "https://", "/", "data:")):
            return normalized
        return f"/api/v1/files/{normalized}/content"

    file_info = file_item.get("file")
    nested_file_id = file_info.get("id") if isinstance(file_info, dict) else None
    for key in ("id", "file_id", "fileId", "bridge_file_id"):
        value = file_item.get(key)
        normalized = value.strip() if isinstance(value, str) else ""
        if normalized and normalized.lower() not in {"null", "undefined"}:
            return f"/api/v1/files/{normalized}/content"

    if isinstance(nested_file_id, str):
        normalized = nested_file_id.strip()
        if normalized and normalized.lower() not in {"null", "undefined"}:
            return f"/api/v1/files/{normalized}/content"

    return ""


def _should_prepare_chat_file(file_item: Any) -> bool:
    if not isinstance(file_item, dict):
        return False

    file_type = str(file_item.get("type", "file") or "file").strip().lower()
    if file_type in {"folder", "collection", "note", "chat"}:
        return False

    file_id = file_item.get("id")
    if not isinstance(file_id, str) or not file_id.strip():
        return False

    if _is_media_file_item(file_item):
        return False

    return True


def _should_retry_failed_chat_file_processing(file_data: Optional[dict]) -> bool:
    if not isinstance(file_data, dict):
        return False

    if str(file_data.get("content") or "").strip():
        return False

    if str(file_data.get("status") or "").strip().lower() != "failed":
        return False

    error_text = " ".join(
        str(file_data.get(key) or "").strip() for key in ("error", "retrieval_error")
    ).lower()
    if not error_text:
        return False

    retry_markers = (
        "no module named",
        "punkt_tab",
        "averaged_perceptron_tagger_eng",
    )
    return any(marker in error_text for marker in retry_markers)


def _should_retry_failed_chat_file_retrieval(
    file_data: Optional[dict],
    file_meta: Optional[dict],
) -> bool:
    if not isinstance(file_data, dict):
        return False

    if not str(file_data.get("content") or "").strip():
        return False

    if str(file_meta.get("collection_name") or "").strip():
        return False

    if str(file_data.get("retrieval_status") or "").strip().lower() != "failed":
        return False

    error_text = " ".join(
        str(file_data.get(key) or "").strip() for key in ("retrieval_error", "error")
    ).lower()
    if not error_text:
        return False

    retry_markers = (
        "local sentence-transformers embeddings are unavailable",
        "no module named 'sentence_transformers'",
        "no module named 'transformers'",
        "no module named 'torch'",
    )
    return any(marker in error_text for marker in retry_markers)


def _get_accessible_chat_file(file_id: str, user: UserModel, db: Any) -> Optional[File]:
    normalized_file_id = str(file_id or "").strip()
    if not normalized_file_id:
        return None

    if getattr(user, "role", None) == "admin":
        return Files.get_file_by_id(normalized_file_id, db=db)

    user_id = getattr(user, "id", None)
    if not user_id:
        return None

    return Files.get_file_by_id_and_user_id(normalized_file_id, user_id, db=db)


def _prepare_chat_file_sync(
    request: Request,
    file_item: dict,
    user: UserModel,
) -> tuple[dict, Optional[dict]]:
    prepared = dict(file_item)
    file_id = str(prepared.get("id") or "").strip()
    if not file_id:
        return prepared, None

    with SessionLocal() as db:
        file = _get_accessible_chat_file(file_id, user, db)
        if file is None:
            return prepared, None

        file_data = file.data or {}
        file_meta = file.meta or {}
        content = str(file_data.get("content") or "").strip()
        status = str(file_data.get("status") or "").strip().lower()
        collection_name = str(file_meta.get("collection_name") or "").strip()

        if content and status == "failed" and not collection_name:
            retrieval_error = str(
                file_data.get("retrieval_error")
                or file_data.get("error")
                or "Document content was extracted, but retrieval indexing failed."
            ).strip()
            Files.update_file_data_by_id(
                file.id,
                {
                    "status": "completed",
                    "error": None,
                    "retrieval_status": "failed",
                    "retrieval_error": retrieval_error,
                },
                db=db,
            )
            file = _get_accessible_chat_file(file_id, user, db)
            if file is not None:
                file_data = file.data or {}
                file_meta = file.meta or {}
                content = str(file_data.get("content") or "").strip()
                status = str(file_data.get("status") or "").strip().lower()

        if content and _should_retry_failed_chat_file_retrieval(file_data, file_meta):
            try:
                process_file(
                    request,
                    ProcessFileForm(file_id=file_id),
                    user=user,
                    db=db,
                )
                file = _get_accessible_chat_file(file_id, user, db)
                if file is not None:
                    file_data = file.data or {}
                    file_meta = file.meta or {}
                    content = str(file_data.get("content") or "").strip()
                    status = str(file_data.get("status") or "").strip().lower()
            except Exception as exc:
                log.warning(
                    "Failed to retry retrieval indexing for chat attachment %s: %s",
                    file_id,
                    exc,
                )

        if not content and (
            status not in {"processing", "uploading", "failed"}
            or _should_retry_failed_chat_file_processing(file_data)
        ):
            try:
                process_file(
                    request,
                    ProcessFileForm(file_id=file_id),
                    user=user,
                    db=db,
                )
                file = _get_accessible_chat_file(file_id, user, db)
                if file is not None:
                    file_data = file.data or {}
                    file_meta = file.meta or {}
                    content = str(file_data.get("content") or "").strip()
                    status = str(file_data.get("status") or "").strip().lower()
            except Exception as exc:
                log.warning("Failed to process chat attachment %s: %s", file_id, exc)

        prepared.setdefault("name", file.filename)
        if file_meta.get("content_type") and not prepared.get("content_type"):
            prepared["content_type"] = file_meta.get("content_type")
        if status:
            prepared["status"] = status

        inline_source = _build_inline_file_source(file, content)

    return prepared, inline_source


def _truncate_inline_file_content(content: str, max_chars: int = INLINE_FILE_SOURCE_MAX_CHARS) -> str:
    normalized = str(content or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 1]}…"


def _build_inline_file_source(file, content: str) -> Optional[dict]:
    normalized_content = _truncate_inline_file_content(content)
    if not normalized_content:
        return None

    filename = str(getattr(file, "filename", "") or "").strip() or "uploaded_file"
    file_id = str(getattr(file, "id", "") or "").strip()
    if not file_id:
        return None

    return {
        "source": {
            "id": file_id,
            "name": filename,
            "url": f"/api/v1/files/{file_id}/content",
            "type": "file",
        },
        "document": [normalized_content],
        "metadata": [
            {
                "source": file_id,
                "name": filename,
                "file_id": file_id,
            }
        ],
    }


def _selected_source_engine_file_content(file_item: dict, user: UserModel) -> str:
    text_content = _retrieval_engine_observe_file_text(file_item)
    if text_content:
        return _truncate_inline_file_content(text_content)

    file_id = str(file_item.get("id") or "").strip()
    if not file_id:
        return ""

    with SessionLocal() as db:
        file = _get_accessible_chat_file(file_id, user, db)
        if file is None:
            return ""
        return _truncate_inline_file_content(
            str((file.data or {}).get("content") or "")
        )


def _build_selected_source_engine_candidates(
    *,
    active_scope_files: list[dict],
    active_retrieval_files: list[dict],
    active_inline_sources: list[dict],
    user: UserModel,
) -> list[dict]:
    candidates: list[dict] = []
    seen_file_ids: set[str] = set()

    def append_candidate(file_item: dict) -> None:
        if not isinstance(file_item, dict):
            return

        file_id = str(file_item.get("id") or "").strip()
        if not file_id or file_id in seen_file_ids:
            return

        candidate = copy.deepcopy(file_item)
        text_content = _selected_source_engine_file_content(candidate, user)
        if text_content:
            data = dict(candidate.get("data") or {})
            data["content"] = text_content
            candidate["data"] = data

        candidates.append(candidate)
        seen_file_ids.add(file_id)

    for file_item in active_retrieval_files:
        append_candidate(file_item)

    for file_item in active_scope_files:
        if not any(
            _source_matches_file_item(source, file_item)
            for source in active_inline_sources
        ):
            continue
        append_candidate(file_item)

    return candidates


async def _prepare_chat_files_for_retrieval(
    request: Request,
    files: list[dict],
    user: UserModel,
) -> tuple[list[dict], list[dict], list[tuple[dict, dict]], list[dict], list[dict]]:
    prepared_files = []
    active_inline_sources: list[dict] = []
    reference_inline_sources: list[tuple[dict, dict]] = []
    active_retrieval_files: list[dict] = []
    reference_retrieval_files: list[dict] = []
    for file_item in files:
        if not _should_prepare_chat_file(file_item):
            prepared_file = _apply_adaptive_focus_metadata(
                file_item,
                origin=(
                    str(file_item.get("focus_origin") or "").strip()
                    or _ADAPTIVE_FOCUS_DERIVED
                ),
                tier=(
                    _normalize_adaptive_focus_tier(file_item.get("focus_tier"))
                    or _ADAPTIVE_FOCUS_REFERENCE
                ),
            )
            prepared_files.append(prepared_file)
            if _is_media_file_item(prepared_file):
                continue
            if _is_active_focus_file(prepared_file):
                active_retrieval_files.append(prepared_file)
            else:
                reference_retrieval_files.append(prepared_file)
            continue

        prepared_file, inline_source = await asyncio.to_thread(
            _prepare_chat_file_sync, request, file_item, user
        )
        prepared_files.append(prepared_file)
        if _is_media_file_item(prepared_file):
            continue
        if inline_source:
            if _is_active_focus_file(prepared_file):
                active_inline_sources.append(inline_source)
            else:
                reference_inline_sources.append((prepared_file, inline_source))
        else:
            if _is_active_focus_file(prepared_file):
                active_retrieval_files.append(prepared_file)
            else:
                reference_retrieval_files.append(prepared_file)

    return (
        prepared_files,
        active_inline_sources,
        reference_inline_sources,
        active_retrieval_files,
        reference_retrieval_files,
    )


def apply_params_to_form_data(form_data, model):
    params = form_data.pop("params", {})
    custom_params = params.pop("custom_params", {})

    open_webui_params = {
        "stream_response": bool,
        "stream_delta_chunk_size": int,
        "function_calling": str,
        "reasoning_tags": list,
        "system": str,
    }

    for key in list(params.keys()):
        if key in open_webui_params:
            del params[key]

    if custom_params:
        # Attempt to parse custom_params if they are strings
        for key, value in custom_params.items():
            if isinstance(value, str):
                try:
                    # Attempt to parse the string as JSON
                    custom_params[key] = json.loads(value)
                except json.JSONDecodeError:
                    # If it fails, keep the original string
                    pass

        # If custom_params are provided, merge them into params
        params = deep_update(params, custom_params)

    if model.get("owned_by") == "ollama":
        # Ollama specific parameters
        form_data["options"] = params
    else:
        if isinstance(params, dict):
            for key, value in params.items():
                if value is not None:
                    form_data[key] = value

        if "logit_bias" in params and params["logit_bias"] is not None:
            try:
                logit_bias = convert_logit_bias_input_to_json(params["logit_bias"])

                if logit_bias:
                    form_data["logit_bias"] = json.loads(logit_bias)
            except Exception as e:
                log.exception(f"Error parsing logit_bias: {e}")

    return form_data


async def convert_url_images_to_base64(form_data):
    messages = form_data.get("messages", [])

    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue

        new_content = []

        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                new_content.append(item)
                continue

            image_url = item.get("image_url", {}).get("url")
            if not isinstance(image_url, str) or not image_url.strip():
                log.debug("Skipping image_url item without a valid URL.")
                continue

            image_url = image_url.strip()
            if image_url.startswith("data:image/"):
                new_content.append(item)
                continue

            try:
                base64_data = await asyncio.to_thread(
                    get_image_base64_from_url, image_url
                )
                if base64_data:
                    new_content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": base64_data},
                        }
                    )
                else:
                    log.debug(
                        "Skipping base64 image_url conversion for %s: empty result.",
                        image_url,
                    )
                    new_content.append(item)
            except Exception as e:
                log.debug(f"Error converting image URL to base64: {e}")
                new_content.append(item)

        message["content"] = new_content

    return form_data


def load_messages_from_db(chat_id: str, message_id: str) -> Optional[list[dict]]:
    """
    Load the message chain from DB up to message_id,
    keeping the fields needed for prompt reconstruction.
    """
    messages_map = Chats.get_messages_map_by_chat_id(chat_id)
    if not messages_map:
        return None

    db_messages = get_message_list(messages_map, message_id)
    if not db_messages:
        return None

    messages = [
        {
            k: v
            for k, v in msg.items()
            if k
            in (
                "role",
                "content",
                "output",
                "files",
                "status",
                "status_history",
                "statusHistory",
            )
        }
        for msg in db_messages
    ]
    for message in messages:
        if message.get("role") != "assistant":
            continue

        output = message.get("output")
        fallback_content = message.get("content", "")
        if isinstance(output, list):
            normalized_output = _normalize_output_for_chat(output)
            message["output"] = normalized_output
            message["content"] = _serialize_output_for_chat_content(
                normalized_output,
                fallback_content=(
                    fallback_content if isinstance(fallback_content, str) else ""
                ),
            )
            continue

    if not any(
        message.get("role") == "user"
        and any(_is_image_file_item(file_item) for file_item in message.get("files", []))
        for message in messages
    ):
        target_message = messages_map.get(message_id, {})
        target_timestamp = (
            int(target_message.get("timestamp"))
            if isinstance(target_message.get("timestamp"), (int, float))
            else None
        )
        orphan_image_files = [
            {**file_item, "url": _resolve_chat_file_url(file_item)}
            for file_item in Chats.get_orphan_message_files_by_chat_id(
                chat_id, before_timestamp=target_timestamp
            )
            if _is_image_file_item(file_item) and _resolve_chat_file_url(file_item)
        ]
        if orphan_image_files:
            target_user_index = next(
                (
                    index
                    for index in range(len(messages) - 1, -1, -1)
                    if messages[index].get("role") == "user"
                ),
                None,
            )
            if target_user_index is None:
                messages.append(
                    {
                        "role": "user",
                        "content": "",
                        "files": orphan_image_files,
                    }
                )
            else:
                existing_files = messages[target_user_index].get("files", [])
                merged_files = []
                seen_refs: set[str] = set()
                for file_item in [
                    *(existing_files if isinstance(existing_files, list) else []),
                    *orphan_image_files,
                ]:
                    if not isinstance(file_item, dict):
                        continue
                    file_ref = (
                        _resolve_chat_file_url(file_item)
                        or str(file_item.get("id") or "").strip()
                    )
                    if file_ref and file_ref in seen_refs:
                        continue
                    if file_ref:
                        seen_refs.add(file_ref)
                    merged_files.append(file_item)

                messages[target_user_index] = {
                    **messages[target_user_index],
                    "files": merged_files,
                }

    return messages


def process_messages_with_output(messages: list[dict]) -> list[dict]:
    """
    Process messages with OR-aligned output items for LLM consumption.

    For assistant messages with 'output' field, produces properly formatted
    OpenAI-style messages (tool_calls + tool results). Internal chat metadata is
    stripped before the provider-facing payload is sent.
    """
    processed = []

    for message in messages:
        if message.get("role") == "assistant" and message.get("output"):
            # Use output items for clean OpenAI-format messages
            normalized_output = _normalize_output_for_chat(message["output"])
            output_messages = convert_output_to_messages(normalized_output, raw=True)
            if output_messages:
                processed.extend(output_messages)
                continue

        # Strip internal metadata before adding (LLM shouldn't see it)
        clean_message = {
            k: v
            for k, v in message.items()
            if k not in {"output", "status", "statusHistory", "status_history"}
        }
        if clean_message.get("role") == "assistant":
            clean_message["content"] = _sanitize_assistant_message_content(
                clean_message.get("content")
            )
        processed.append(clean_message)

    return processed


async def process_chat_payload(request, form_data, user, metadata, model):
    # Pipeline Inlet -> Filter Inlet -> Chat Memory -> Chat Web Search -> Chat Image Generation
    # -> Chat Code Interpreter (Form Data Update) -> (Default) Chat Tools Function Calling
    # -> Chat Files

    form_data = apply_params_to_form_data(form_data, model)
    log.debug(f"form_data: {form_data}")

    # Load messages from DB when available — DB preserves structured 'output' items
    # which the frontend strips, causing tool calls to be merged into content.
    chat_id = metadata.get("chat_id")
    parent_message_id = metadata.get("parent_message_id")

    if chat_id and parent_message_id and not chat_id.startswith("local:"):
        db_messages = load_messages_from_db(chat_id, parent_message_id)
        if db_messages:
            system_message = get_system_message(form_data.get("messages", []))
            form_data["messages"] = (
                [system_message, *db_messages] if system_message else db_messages
            )

            # Inject image files into content as image_url parts (mirrors frontend logic)
            for message in form_data["messages"]:
                image_files = [
                    f
                    for f in message.get("files", [])
                    if _is_image_file_item(f)
                ]
                if message.get("role") == "user" and image_files:
                    text_content = message.get("content", "")
                    if isinstance(text_content, str):
                        image_parts = [
                            {
                                "type": "image_url",
                                "image_url": {"url": file_url},
                            }
                            for f in image_files
                            if (file_url := _resolve_chat_file_url(f))
                        ]
                        if not image_parts:
                            message.pop("files", None)
                            continue
                        message["content"] = [
                            *(
                                [{"type": "text", "text": text_content}]
                                if text_content
                                else []
                            ),
                            *image_parts,
                        ]
                # Strip files field — it's been incorporated into content
                message.pop("files", None)

    # Process messages with OR-aligned output items for clean LLM messages
    form_data["messages"] = process_messages_with_output(form_data.get("messages", []))

    system_message = get_system_message(form_data.get("messages", []))
    if system_message:  # Chat Controls/User Settings
        try:
            form_data = apply_system_prompt_to_body(
                system_message.get("content"), form_data, metadata, user, replace=True
            )  # Required to handle system prompt variables
        except:
            pass

    chat_id = metadata.get("chat_id")
    chat_meta: dict[str, Any] = {}
    session_user_facts: list[dict[str, str]] = []
    if chat_id and isinstance(chat_id, str) and not chat_id.startswith("local:"):
        chat = Chats.get_chat_by_id_and_user_id(chat_id, user.id)
        if chat:
            chat_meta = dict(chat.meta or {})

    stored_session_user_facts = normalize_session_user_facts(
        chat_meta.get("session_user_facts", [])
    )
    extracted_session_user_facts = extract_session_user_facts(form_data.get("messages"))
    session_user_facts = (
        extracted_session_user_facts or stored_session_user_facts
    )

    if (
        chat_id
        and isinstance(chat_id, str)
        and not chat_id.startswith("local:")
        and session_user_facts != stored_session_user_facts
    ):
        session_user_facts_updated_at = int(time.time())
        Chats.update_chat_meta_by_id(
            chat_id,
            {
                "session_user_facts": session_user_facts,
                "session_user_facts_updated_at": session_user_facts_updated_at,
            },
        )
        chat_meta = {
            **chat_meta,
            "session_user_facts": session_user_facts,
            "session_user_facts_updated_at": session_user_facts_updated_at,
        }

    session_user_memory_prompt = build_session_user_memory_prompt(
        form_data.get("messages"),
        stored_facts=session_user_facts,
    )
    if session_user_memory_prompt:
        form_data["messages"] = add_or_update_system_message(
            session_user_memory_prompt,
            form_data["messages"],
            append=True,
        )

    form_data = await convert_url_images_to_base64(form_data)

    event_emitter = get_event_emitter(metadata)
    event_caller = get_event_call(metadata)

    extra_params = {
        "__event_emitter__": event_emitter,
        "__event_call__": event_caller,
        "__user__": user.model_dump() if isinstance(user, UserModel) else {},
        "__metadata__": metadata,
        "__oauth_token__": await get_system_oauth_token(request, user),
        "__request__": request,
        "__model__": model,
        "__chat_id__": metadata.get("chat_id"),
        "__message_id__": metadata.get("message_id"),
    }
    client_capabilities = _resolve_client_capabilities(request, user, metadata)
    extra_params["__client_capabilities__"] = client_capabilities
    # Initialize events to store additional event to be sent to the client
    # Initialize contexts and citation
    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    task_model_id = get_task_model_id(
        form_data["model"],
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    events = []
    sources = []
    retrieval_diagnostics: list[dict[str, Any]] = []
    retrieval_no_evidence = False

    current_turn_files = _apply_adaptive_focus_metadata_to_items(
        form_data.get("files", []),
        origin=_ADAPTIVE_FOCUS_CURRENT_TURN,
        tier=_ADAPTIVE_FOCUS_ACTIVE,
    )
    if current_turn_files:
        form_data["files"] = current_turn_files

    # Folder "Project" handling
    # Check if the request has chat_id and is inside of a folder
    # Uses lightweight column query — only fetches folder_id, not the full chat JSON blob
    chat_id = metadata.get("chat_id", None)
    if chat_id and user:
        folder_id = Chats.get_chat_folder_id(chat_id, user.id)
        if folder_id:
            folder = Folders.get_folder_by_id_and_user_id(folder_id, user.id)

            if folder and folder.data:
                if "system_prompt" in folder.data:
                    form_data = apply_system_prompt_to_body(
                        folder.data["system_prompt"], form_data, metadata, user
                    )
                if "files" in folder.data:
                    folder_files = _apply_adaptive_focus_metadata_to_items(
                        folder.data["files"],
                        origin=_ADAPTIVE_FOCUS_FOLDER,
                        tier=_ADAPTIVE_FOCUS_REFERENCE,
                    )
                    if metadata.get("params", {}).get("function_calling") != "native":
                        form_data["files"] = _dedupe_file_context_items(
                            [*form_data.get("files", []), *folder_files]
                        )
                    else:
                        # Native FC: skip RAG injection, builtin tools
                        # will read folder knowledge from metadata.
                        metadata["folder_knowledge"] = folder_files

    # Model "Knowledge" handling
    user_message = get_last_user_message(form_data["messages"])
    model_knowledge = model.get("info", {}).get("meta", {}).get("knowledge", False)

    if (
        model_knowledge
        and metadata.get("params", {}).get("function_calling") != "native"
        and event_emitter
    ):
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "knowledge_search",
                    "query": user_message,
                    "done": False,
                },
            }
        )

        knowledge_files = []
        for item in model_knowledge:
            if item.get("collection_name"):
                knowledge_files.append(
                    {
                        "id": item.get("collection_name"),
                        "name": item.get("name"),
                        "legacy": True,
                    }
                )
            elif item.get("collection_names"):
                knowledge_files.append(
                    {
                        "name": item.get("name"),
                        "type": "collection",
                        "collection_names": item.get("collection_names"),
                        "legacy": True,
                    }
                )
            else:
                knowledge_files.append(item)

        knowledge_files = _apply_adaptive_focus_metadata_to_items(
            knowledge_files,
            origin=_ADAPTIVE_FOCUS_MODEL,
            tier=_ADAPTIVE_FOCUS_REFERENCE,
        )

        form_data["files"] = _dedupe_file_context_items(
            [*form_data.get("files", []), *knowledge_files]
        )

    variables = form_data.pop("variables", None)

    # Process the form_data through the pipeline
    try:
        form_data = await process_pipeline_inlet_filter(
            request, form_data, user, models
        )
    except Exception as e:
        raise e

    try:
        filter_ids = get_sorted_filter_ids(
            request, model, metadata.get("filter_ids", [])
        )
        filter_functions = Functions.get_functions_by_ids(filter_ids)

        form_data, flags = await process_filter_functions(
            request=request,
            filter_functions=filter_functions,
            filter_type="inlet",
            form_data=form_data,
            extra_params=extra_params,
        )
    except Exception as e:
        raise Exception(f"{e}")

    features = form_data.pop("features", None) or {}
    extra_params["__features__"] = features
    if features:
        if "voice" in features and features["voice"]:
            if request.app.state.config.VOICE_MODE_PROMPT_TEMPLATE != None:
                if request.app.state.config.VOICE_MODE_PROMPT_TEMPLATE != "":
                    template = request.app.state.config.VOICE_MODE_PROMPT_TEMPLATE
                else:
                    template = DEFAULT_VOICE_MODE_PROMPT_TEMPLATE

                form_data["messages"] = add_or_update_system_message(
                    template,
                    form_data["messages"],
                )

    client_capabilities_prompt = _build_client_capabilities_system_prompt(
        client_capabilities
    )
    if client_capabilities_prompt:
        form_data["messages"] = add_or_update_system_message(
            client_capabilities_prompt,
            form_data["messages"],
            append=True,
        )

    if "memory" in features and features["memory"]:
        # Skip forced memory injection when native FC is enabled - model can use memory tools
        if metadata.get("params", {}).get("function_calling") != "native":
            form_data = await chat_memory_handler(
                request, form_data, extra_params, user
            )

    if "web_search" in features and features["web_search"]:
        # Skip forced RAG web search when native FC is enabled - model can use web_search tool
        if metadata.get("params", {}).get("function_calling") != "native":
            form_data = await chat_web_search_handler(
                request, form_data, extra_params, user
            )

    if "image_generation" in features and features["image_generation"]:
        # Skip forced image generation when native FC is enabled - model can use generate_image tool
        if metadata.get("params", {}).get("function_calling") != "native":
            form_data = await chat_image_generation_handler(
                request, form_data, extra_params, user
            )

    if "code_interpreter" in features and features["code_interpreter"]:
        engine = getattr(
            request.app.state.config, "CODE_INTERPRETER_ENGINE", "pyodide"
        )

        # Skip XML-tag prompt injection when native FC is enabled —
        # execute_code will be injected as a builtin tool instead
        if metadata.get("params", {}).get("function_calling") != "native":
            prompt = (
                request.app.state.config.CODE_INTERPRETER_PROMPT_TEMPLATE
                if request.app.state.config.CODE_INTERPRETER_PROMPT_TEMPLATE != ""
                else DEFAULT_CODE_INTERPRETER_PROMPT
            )

            # Append filesystem awareness only for pyodide engine
            if engine != "jupyter":
                prompt += CODE_INTERPRETER_PYODIDE_PROMPT

            form_data["messages"] = add_or_update_user_message(
                prompt,
                form_data["messages"],
            )
        else:
            # Native FC: tool docstring can't be dynamic, so inject
            # filesystem context into messages for pyodide engine
            if engine != "jupyter":
                form_data["messages"] = add_or_update_user_message(
                    CODE_INTERPRETER_PYODIDE_PROMPT,
                    form_data["messages"],
                )

    def _extract_skill_routing_text(content: Any) -> str:
        if isinstance(content, str):
            return content

        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in {"text", "input_text", "output_text"}:
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())

        return "\n".join(parts)

    def _extract_latest_user_message_text(messages: Any) -> str:
        if not isinstance(messages, list):
            return ""

        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = _extract_skill_routing_text(message.get("content"))
            if text.strip():
                return text.strip()

        return ""

    def _infer_document_skill_ids(messages: Any, available_skill_ids: set[str]) -> set[str]:
        latest_user_text = _extract_latest_user_message_text(messages)
        if not latest_user_text:
            return set()

        action_pattern = re.compile(
            r"(导出|生成|创建|制作|输出|转换|转成|转为|整理成|另存为|保存为|下载|make|generate|create|export|convert|save|download|render|write)",
            flags=re.IGNORECASE,
        )
        if not action_pattern.search(latest_user_text):
            return set()

        inferred_skill_ids: set[str] = set()
        intent_rules = {
            "minimax-pdf": re.compile(r"(\bpdf\b|pdf格式|pdf文件)", flags=re.IGNORECASE),
            "minimax-docx": re.compile(
                r"(\bdocx\b|\bword\b|word文档|word 文档|word格式|docx格式)",
                flags=re.IGNORECASE,
            ),
            "minimax-xlsx": re.compile(
                r"(\bxlsx\b|\bexcel\b|excel表|excel 文件|工作簿|电子表格|xlsx格式)",
                flags=re.IGNORECASE,
            ),
            "pptx-generator": re.compile(
                r"(\bpptx\b|\bppt\b|\bpowerpoint\b|演示文稿|幻灯片|ppt格式|pptx格式)",
                flags=re.IGNORECASE,
            ),
        }

        for skill_id, format_pattern in intent_rules.items():
            if skill_id not in available_skill_ids:
                continue
            if format_pattern.search(latest_user_text):
                inferred_skill_ids.add(skill_id)

        return inferred_skill_ids

    def _infer_meta_skill_ids(messages: Any, available_skill_ids: set[str]) -> set[str]:
        latest_user_text = _extract_latest_user_message_text(messages)
        if not latest_user_text or "skill-creator" not in available_skill_ids:
            return set()

        creation_patterns = (
            re.compile(
                r"((创建|新建|设计|编写|制作|起草|生成|修改|更新|重构|固化|沉淀).{0,8}(技能|skill))",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"((技能|skill).{0,8}(创建|新建|设计|编写|制作|起草|生成|修改|更新|重构))",
                flags=re.IGNORECASE,
            ),
            re.compile(r"\b(skill draft|meta skill|create skill|update skill)\b", flags=re.IGNORECASE),
        )

        if any(pattern.search(latest_user_text) for pattern in creation_patterns):
            return {"skill-creator"}

        return set()

    def _build_inferred_document_skill_prompt(skill_ids: set[str]) -> str:
        if not skill_ids:
            return ""

        notes: list[str] = []
        for skill_id in sorted(skill_ids):
            note = str(DOCUMENT_SKILL_PHASE_NOTES.get(skill_id) or "").strip()
            if note:
                notes.append(f"- {note}")

        if not notes:
            return ""

        return (
            "【文档路由提示】用户本轮明确要求生成或导出可下载文档。"
            "优先调用下面对应的文档工具；如果本轮没有返回标准文件引用，"
            "不要声称文件已经生成成功或已经可下载。\n"
            + "\n".join(notes)
        )

    def _build_dynamic_skill_loading_prompt(skill_count: int) -> str:
        if skill_count <= 0:
            return ""

        return (
            "【技能动态加载】当前工作区存在可按需调用的技能。"
            "当你怀疑某个技能能帮助当前任务时，先调用 `list_skills` 查找相关技能，"
            "再调用 `view_skill` 读取完整说明，然后按技能要求执行；"
            "不要在未读取技能正文前臆测技能细节。"
        )

    def _infer_referenced_skill_ids(
        latest_user_text: str, indexed_skill_map: dict[str, Any]
    ) -> set[str]:
        normalized_text = str(latest_user_text or "").strip().lower()
        if not normalized_text:
            return set()

        matched_skill_ids: set[str] = set()
        for skill_id, skill in indexed_skill_map.items():
            candidates = []
            for value in (skill_id, getattr(skill, "name", None)):
                if not isinstance(value, str):
                    continue
                candidate = value.strip()
                if candidate:
                    candidates.append(candidate)

            for candidate in candidates:
                normalized_candidate = candidate.lower()
                if len(normalized_candidate) < 3 and normalized_candidate.isascii():
                    continue
                if normalized_candidate in normalized_text:
                    matched_skill_ids.add(skill_id)
                    break

        return matched_skill_ids

    tool_ids = form_data.pop("tool_ids", None)
    terminal_id = form_data.pop("terminal_id", None)
    files = form_data.pop("files", None)

    from open_webui.models.resource_installations import ResourceInstallations

    session_tool_ids: list[str] = []
    session_skill_ids: set[str] = set()
    if chat_id and isinstance(chat_id, str) and not chat_id.startswith("local:"):
        session_tool_ids = list(chat_meta.get("session_tool_ids", []) or [])
        session_skill_ids = set(chat_meta.get("session_skill_ids", []) or [])

    installed_tool_ids = list(
        ResourceInstallations.get_installed_resource_ids(user.id, "tool")
    )
    tool_ids = filter_hidden_tool_ids(
        list(dict.fromkeys([*(tool_ids or []), *session_tool_ids, *installed_tool_ids]))
    )

    # Caller-provided OpenAI-style tools take precedence over server-side
    # tool resolution (tool_ids, MCP servers, builtin tools).
    payload_tools = form_data.get("tools", None)
    builtin_tools_enabled = (
        model.get("info", {}).get("meta", {}).get("capabilities") or {}
    ).get("builtin_tools", True)
    native_function_calling = metadata.get("params", {}).get("function_calling") == "native"

    # Skills
    user_skill_ids = set(
        filter_hidden_skill_ids(
            [
                *(form_data.pop("skill_ids", None) or []),
                *session_skill_ids,
                *ResourceInstallations.get_installed_resource_ids(user.id, "skill"),
            ]
        )
    )
    model_skill_ids = set(
        filter_hidden_skill_ids(model.get("info", {}).get("meta", {}).get("skillIds", []))
    )

    indexed_skills = {}
    inferred_document_skill_ids: set[str] = set()
    inferred_meta_skill_ids: set[str] = set()
    dynamic_skill_ids: set[str] = set()
    latest_user_text = _extract_latest_user_message_text(form_data.get("messages"))
    if user_skill_ids or model_skill_ids or latest_user_text or (
        native_function_calling and builtin_tools_enabled
    ):
        from open_webui.models.skills import Skills as SkillsModel

        indexed_skills = {
            skill.id: skill
            for skill in filter_visible_skills(
                SkillsModel.get_skills(), user, require_active=True
            )
            if is_catalog_runtime_activatable(
                getattr(skill, "meta", None), getattr(skill, "access_grants", [])
            )
        }
        referenced_skill_ids = _infer_referenced_skill_ids(
            latest_user_text,
            indexed_skills,
        )
        if referenced_skill_ids:
            user_skill_ids |= referenced_skill_ids
        dynamic_skill_ids = set(indexed_skills.keys()) - user_skill_ids
        if latest_user_text:
            inferred_document_skill_ids = _infer_document_skill_ids(
                form_data.get("messages"), set(indexed_skills.keys())
            )
            inferred_meta_skill_ids = _infer_meta_skill_ids(
                form_data.get("messages"), set(indexed_skills.keys())
            )
            model_skill_ids |= inferred_document_skill_ids | inferred_meta_skill_ids

    all_skill_ids = set(filter_hidden_skill_ids(user_skill_ids | model_skill_ids))
    available_skills = []
    if all_skill_ids and indexed_skills:
        available_skills = [
            indexed_skills[skill_id]
            for skill_id in all_skill_ids
            if skill_id in indexed_skills
        ]

        skill_descriptions = ""
        for skill in available_skills:
            if skill.id in user_skill_ids:
                # User-selected: inject full content
                form_data["messages"] = add_or_update_system_message(
                    f'<skill name="{skill.name}">\n{skill.content}\n</skill>',
                    form_data["messages"],
                    append=True,
                )
            else:
                # Model-attached: name+description only
                skill_descriptions += f"<skill>\n<name>{skill.name}</name>\n<description>{skill.description or ''}</description>\n</skill>\n"

        if skill_descriptions:
            form_data["messages"] = add_or_update_system_message(
                f"<available_skills>\n{skill_descriptions}</available_skills>",
                form_data["messages"],
                append=True,
            )

        inferred_skill_prompt = _build_inferred_document_skill_prompt(
            inferred_document_skill_ids
        )
        if inferred_skill_prompt:
            form_data["messages"] = add_or_update_system_message(
                inferred_skill_prompt,
                form_data["messages"],
                append=True,
            )

    dynamic_skill_loading_prompt = ""
    if native_function_calling and builtin_tools_enabled:
        dynamic_skill_loading_prompt = _build_dynamic_skill_loading_prompt(
            len(dynamic_skill_ids)
        )
        if dynamic_skill_loading_prompt:
            form_data["messages"] = add_or_update_system_message(
                dynamic_skill_loading_prompt,
                form_data["messages"],
                append=True,
            )

    prompt = get_last_user_message(form_data["messages"])
    # TODO: re-enable URL extraction from prompt
    # urls = []
    # if prompt and len(prompt or "") < 500 and (not files or len(files) == 0):
    #     urls = extract_urls(prompt)

    if isinstance(files, list):
        expanded_files: list[dict] = []

        for file_item in files:
            if not isinstance(file_item, dict):
                continue

            if file_item.get("type", "file") == "folder":
                folder_id = file_item.get("id", None)
                if folder_id:
                    folder = Folders.get_folder_by_id_and_user_id(folder_id, user.id)
                    if folder and folder.data and "files" in folder.data:
                        expanded_files.extend(
                            _apply_adaptive_focus_metadata_to_items(
                                folder.data["files"],
                                origin=(
                                    str(file_item.get("focus_origin") or "").strip()
                                    or _ADAPTIVE_FOCUS_DERIVED
                                ),
                                tier=(
                                    _normalize_adaptive_focus_tier(
                                        file_item.get("focus_tier")
                                    )
                                    or _ADAPTIVE_FOCUS_REFERENCE
                                ),
                            )
                        )
                        continue

            expanded_files.append(file_item)

        files = _dedupe_file_context_items(
            [
                _apply_adaptive_focus_metadata(
                    file_item,
                    origin=(
                        str(file_item.get("focus_origin") or "").strip()
                        or _ADAPTIVE_FOCUS_DERIVED
                    ),
                    tier=(
                        _normalize_adaptive_focus_tier(file_item.get("focus_tier"))
                        or _ADAPTIVE_FOCUS_REFERENCE
                    ),
                )
                for file_item in expanded_files
                if isinstance(file_item, dict)
            ]
        )

        # Drop invalid/incomplete file entries (e.g. null id/url from in-flight uploads).
        sanitized_files = []
        for file_item in files:
            if not isinstance(file_item, dict):
                continue

            file_type = file_item.get("type", "file")
            if file_type == "folder":
                folder_id = file_item.get("id")
                if (
                    isinstance(folder_id, str)
                    and folder_id.strip()
                    and folder_id.strip().lower() != "null"
                ):
                    sanitized_files.append(file_item)
                continue

            status = str(file_item.get("status", "")).strip().lower()
            if status == "uploading":
                continue

            file_id = file_item.get("id")
            file_url = file_item.get("url")

            has_valid_id = (
                isinstance(file_id, str)
                and file_id.strip()
                and file_id.strip().lower() != "null"
            )
            has_valid_url = (
                isinstance(file_url, str)
                and file_url.strip()
                and file_url.strip().lower() != "null"
            )
            has_inline_content = (
                isinstance(file_item.get("content"), str)
                and file_item.get("content").strip() != ""
            )

            if has_valid_id or has_valid_url or has_inline_content:
                sanitized_files.append(file_item)

        files = sanitized_files

    provider_file_ids = []
    if files:
        for file_item in files:
            file_id = file_item.get("id")
            if isinstance(file_id, str) and file_id:
                provider_file_ids.append(file_id)
        # Preserve order while removing duplicates
        provider_file_ids = list(dict.fromkeys(provider_file_ids))

    metadata = {
        **metadata,
        "tool_ids": tool_ids,
        "terminal_id": terminal_id,
        "files": files,
    }
    if native_function_calling and builtin_tools_enabled and dynamic_skill_ids:
        metadata[DEEPAGENT_RUNTIME_SKILL_IDS_METADATA_KEY] = sorted(dynamic_skill_ids)
    else:
        metadata.pop(DEEPAGENT_RUNTIME_SKILL_IDS_METADATA_KEY, None)
    metadata["deepagent_runtime_tools"] = build_deepagent_runtime_tool_snapshot(
        tool_ids,
        user,
        files=files,
        metadata=metadata,
        model_knowledge=model_knowledge if isinstance(model_knowledge, list) else [],
    )
    form_data["metadata"] = metadata

    # Keep explicit file refs in outbound payload for OpenAI-compatible providers.
    if files:
        form_data["files"] = files
        form_data["attachments"] = files
    if provider_file_ids:
        form_data["file_ids"] = provider_file_ids

    # When the caller provides an explicit OpenAI-style `tools` array in the
    # request body, skip all server-side tool resolution and pass the caller's
    # tools through to the model unchanged.
    if not payload_tools:
        # Server side tools
        tool_ids = metadata.get("tool_ids", None)
        # Client side tools
        direct_tool_servers = metadata.get("tool_servers", None)

        log.debug(f"{tool_ids=}")
        log.debug(f"{direct_tool_servers=}")

        tools_dict = {}

        mcp_clients = {}
        mcp_tools_dict = {}

        if tool_ids:
            for tool_id in tool_ids:
                if tool_id.startswith("server:mcp:"):
                    try:
                        server_id = tool_id[len("server:mcp:") :]

                        mcp_server_connection = None
                        for (
                            server_connection
                        ) in request.app.state.config.TOOL_SERVER_CONNECTIONS:
                            if (
                                server_connection.get("type", "") == "mcp"
                                and server_connection.get("info", {}).get("id")
                                == server_id
                            ):
                                mcp_server_connection = server_connection
                                break

                        if not mcp_server_connection:
                            log.error(f"MCP server with id {server_id} not found")
                            continue

                        # Check access control for MCP server
                        if not has_connection_access(user, mcp_server_connection):
                            log.warning(
                                f"Access denied to MCP server {server_id} for user {user.id}"
                            )
                            continue

                        auth_type = mcp_server_connection.get("auth_type", "")
                        headers = {}
                        if auth_type == "bearer":
                            headers["Authorization"] = (
                                f"Bearer {mcp_server_connection.get('key', '')}"
                            )
                        elif auth_type == "none":
                            # No authentication
                            pass
                        elif auth_type == "session":
                            headers["Authorization"] = (
                                f"Bearer {request.state.token.credentials}"
                            )
                        elif auth_type == "system_oauth":
                            oauth_token = extra_params.get("__oauth_token__", None)
                            if oauth_token:
                                headers["Authorization"] = (
                                    f"Bearer {oauth_token.get('access_token', '')}"
                                )
                        elif auth_type == "oauth_2.1":
                            try:
                                splits = server_id.split(":")
                                server_id = splits[-1] if len(splits) > 1 else server_id

                                oauth_token = await request.app.state.oauth_client_manager.get_oauth_token(
                                    user.id, f"mcp:{server_id}"
                                )

                                if oauth_token:
                                    headers["Authorization"] = (
                                        f"Bearer {oauth_token.get('access_token', '')}"
                                    )
                            except Exception as e:
                                log.error(f"Error getting OAuth token: {e}")
                                oauth_token = None

                        connection_headers = mcp_server_connection.get("headers", None)
                        if connection_headers and isinstance(connection_headers, dict):
                            for key, value in connection_headers.items():
                                headers[key] = value

                        # Add user info headers if enabled
                        if ENABLE_FORWARD_USER_INFO_HEADERS and user:
                            headers = include_user_info_headers(headers, user)
                            if metadata and metadata.get("chat_id"):
                                headers[FORWARD_SESSION_INFO_HEADER_CHAT_ID] = (
                                    metadata.get("chat_id")
                                )
                            if metadata and metadata.get("message_id"):
                                headers[FORWARD_SESSION_INFO_HEADER_MESSAGE_ID] = (
                                    metadata.get("message_id")
                                )

                        mcp_clients[server_id] = MCPClient()
                        await mcp_clients[server_id].connect(
                            url=mcp_server_connection.get("url", ""),
                            headers=headers if headers else None,
                        )

                        function_name_filter_list = mcp_server_connection.get(
                            "config", {}
                        ).get("function_name_filter_list", "")

                        if isinstance(function_name_filter_list, str):
                            function_name_filter_list = function_name_filter_list.split(
                                ","
                            )

                        tool_specs = await mcp_clients[server_id].list_tool_specs()
                        for tool_spec in tool_specs:

                            def make_tool_function(client, function_name):
                                async def tool_function(**kwargs):
                                    return await client.call_tool(
                                        function_name,
                                        function_args=kwargs,
                                    )

                                return tool_function

                            if function_name_filter_list:
                                if not is_string_allowed(
                                    tool_spec["name"], function_name_filter_list
                                ):
                                    # Skip this function
                                    continue

                            tool_function = make_tool_function(
                                mcp_clients[server_id], tool_spec["name"]
                            )

                            mcp_tools_dict[f"{server_id}_{tool_spec['name']}"] = {
                                "spec": {
                                    **tool_spec,
                                    "name": f"{server_id}_{tool_spec['name']}",
                                },
                                "callable": tool_function,
                                "type": "mcp",
                                "client": mcp_clients[server_id],
                                "direct": False,
                            }
                    except Exception as e:
                        log.debug(e)
                        if event_emitter:
                            await event_emitter(
                                {
                                    "type": "chat:message:error",
                                    "data": {
                                        "error": {
                                            "content": f"Failed to connect to MCP server '{server_id}'"
                                        }
                                    },
                                }
                            )
                        continue

            tools_dict = await get_tools(
                request,
                tool_ids,
                user,
                {
                    **extra_params,
                    "__model__": models[task_model_id],
                    "__messages__": form_data["messages"],
                    "__files__": metadata.get("files", []),
                },
            )

            if mcp_tools_dict:
                tools_dict = {**tools_dict, **mcp_tools_dict}

        # Resolve terminal tools if terminal_id is set (outside tool_ids check
        # so system terminals work even when no other tools are selected)
        if terminal_id:
            try:
                terminal_tools = await get_terminal_tools(
                    request,
                    terminal_id,
                    user,
                    extra_params,
                )
                if terminal_tools:
                    tools_dict = {**tools_dict, **terminal_tools}
            except Exception as e:
                log.exception(e)

        if direct_tool_servers:
            for tool_server in direct_tool_servers:
                tool_specs = tool_server.pop("specs", [])

                for tool in tool_specs:
                    tools_dict[tool["name"]] = {
                        "spec": tool,
                        "direct": True,
                        "server": tool_server,
                    }

        if mcp_clients:
            metadata["mcp_clients"] = mcp_clients

        # Inject builtin tools for native function calling based on enabled features and model capability
        # Check if builtin_tools capability is enabled for this model (defaults to True if not specified)
        if (
            native_function_calling
            and builtin_tools_enabled
        ):
            # Add file context to user messages
            chat_id = metadata.get("chat_id")
            form_data["messages"] = add_file_context(
                form_data.get("messages", []),
                chat_id,
                user,
                form_data.get("files", []),
                metadata.get("parent_message_id"),
            )
            builtin_tools = get_builtin_tools(
                request,
                {
                    **extra_params,
                    "__event_emitter__": event_emitter,
                    "__skill_ids__": sorted(dynamic_skill_ids),
                },
                features,
                model,
            )
            for name, tool_dict in builtin_tools.items():
                if name not in tools_dict:
                    tools_dict[name] = tool_dict

        if tools_dict:
            if metadata.get("params", {}).get("function_calling") == "native":
                # If the function calling is native, then call the tools function calling handler
                metadata["tools"] = tools_dict
                form_data["tools"] = [
                    {"type": "function", "function": tool.get("spec", {})}
                    for tool in tools_dict.values()
                ]
            else:
                # If the function calling is not native, then call the tools function calling handler
                try:
                    form_data, flags = await chat_completion_tools_handler(
                        request, form_data, extra_params, user, models, tools_dict
                    )
                    sources.extend(flags.get("sources", []))
                except Exception as e:
                    log.exception(e)

    # Check if file context extraction is enabled for this model (default True)
    file_context_enabled = (
        model.get("info", {}).get("meta", {}).get("capabilities") or {}
    ).get("file_context", True)

    if file_context_enabled:
        try:
            form_data, flags = await chat_completion_files_handler(
                request, form_data, extra_params, user
            )
            file_handler_sources = (
                flags.get("sources") if isinstance(flags.get("sources"), list) else []
            )
            for key in (
                "references",
                "accepted_outputs",
                "context_budget",
                "status",
                "terminal_reason",
                "provenance",
                "authorization_context",
                "retry_policy",
                "retrieval_attempted",
                "no_evidence",
                "first_pass_retrieval_strategy",
                "first_pass_profile_lock",
            ):
                if key in flags:
                    metadata[key] = copy.deepcopy(flags.get(key))
            diagnostics = flags.get("retrieval_diagnostics", [])
            if isinstance(diagnostics, list):
                retrieval_diagnostics.extend(
                    item for item in diagnostics if isinstance(item, dict)
                )
            active_source_scope = flags.get("active_source_scope")
            if isinstance(active_source_scope, dict) and active_source_scope:
                metadata["active_source_scope"] = active_source_scope
            if _is_selected_source_metadata_first_diagnostics_only(flags):
                file_handler_sources = []
                retrieval_no_evidence = True
            sources.extend(file_handler_sources)
            retrieval_no_evidence = retrieval_no_evidence or bool(flags.get("no_evidence"))
        except Exception as e:
            log.exception(e)

    # Save the pre-RAG message state so the native tool call loop can
    # restore to the true original (before file-source injection) rather
    # than a snapshot that already has the RAG template baked in.
    system_message = get_system_message(form_data["messages"])
    metadata["system_prompt"] = (
        get_content_from_message(system_message) if system_message else None
    )
    metadata["user_prompt"] = get_last_user_message(form_data["messages"])
    metadata["sources"] = sources[:] if sources else []
    _merge_reference_sidecar_into_metadata(
        metadata,
        sources=sources,
        diagnostics=retrieval_diagnostics,
    )
    if Chats._should_clear_reference_aliases(
        metadata=metadata,
        diagnostics=retrieval_diagnostics,
        accepted_references=metadata.get("canonical_references"),
    ):
        sources = []
        retrieval_no_evidence = True
        metadata["sources"] = []
        metadata.pop("references", None)
        metadata.pop("citations", None)
        metadata.pop("documents", None)
    metadata["deepagent_runtime_tools"] = build_deepagent_runtime_tool_snapshot(
        tool_ids,
        user,
        files=metadata.get("files") if isinstance(metadata.get("files"), list) else [],
        metadata=metadata,
        model_knowledge=model_knowledge if isinstance(model_knowledge, list) else [],
    )
    form_data["metadata"] = metadata

    defer_source_context_to_research_tool = (
        bool(sources)
        and _is_explicit_research_retrieval(form_data)
        and _has_selected_source_retrieval_runtime_tool(metadata)
    )

    # If context is not empty, insert it into the messages. Research-mode
    # selected-source turns keep first-pass results as metadata/seed state so
    # the agent can perform scoped second-pass retrieval without prompt crowding.
    if sources and prompt and not defer_source_context_to_research_tool:
        form_data["messages"] = apply_source_context_to_messages(
            request, form_data["messages"], sources, prompt
        )
    elif retrieval_no_evidence:
        form_data["messages"] = _append_no_evidence_guard(form_data["messages"])
        if _selected_source_requires_stable_limitation_response(metadata):
            form_data["messages"] = _append_selected_source_limitation_guard(
                form_data["messages"],
                terminal_reason=str(metadata.get("terminal_reason") or ""),
                active_source_scope=(
                    metadata.get("active_source_scope")
                    if isinstance(metadata.get("active_source_scope"), dict)
                    else None
                ),
            )

    # If there are citations, add them to the data_items
    sources = [
        source
        for source in sources
        if source.get("source", {}).get("name", "")
        or source.get("source", {}).get("id", "")
    ]

    if len(sources) > 0 and not defer_source_context_to_research_tool:
        events.append({"sources": sources})

    if model_knowledge and event_emitter:
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "knowledge_search",
                    "query": user_message,
                    "done": True,
                    "hidden": True,
                },
            }
        )

    return form_data, metadata, events


def get_event_emitter_and_caller(metadata):
    event_emitter = None
    event_caller = None
    if (
        "session_id" in metadata
        and metadata["session_id"]
        and "chat_id" in metadata
        and metadata["chat_id"]
        and "message_id" in metadata
        and metadata["message_id"]
    ):
        event_emitter = get_event_emitter(metadata)
        event_caller = get_event_call(metadata)
    return event_emitter, event_caller


def build_chat_response_context(
    request, form_data, user, model, metadata, tasks, events
):
    event_emitter, event_caller = get_event_emitter_and_caller(metadata)
    return {
        "request": request,
        "form_data": form_data,
        "user": user,
        "model": model,
        "metadata": metadata,
        "tasks": tasks,
        "events": events,
        "event_emitter": event_emitter,
        "event_caller": event_caller,
    }


def get_response_data(response):
    if isinstance(response, list) and len(response) == 1:
        # If the response is a single-item list, unwrap it #17213
        response = response[0]

    if isinstance(response, JSONResponse):
        if isinstance(response.body, bytes):
            try:
                response_data = json.loads(response.body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                response_data = {"error": {"detail": "Invalid JSON response"}}
        else:
            response_data = response
    elif isinstance(response, dict):
        response_data = response
    else:
        response_data = None

    return response, response_data


def merge_events_into_response(response_data, events):
    if events and isinstance(events, list):
        extra_response = {}
        for event in events:
            if isinstance(event, dict):
                extra_response.update(event)
            else:
                extra_response[event] = True

        return {
            **extra_response,
            **response_data,
        }
    return response_data


def build_response_object(response, response_data):
    if isinstance(response, dict):
        return response_data
    if isinstance(response, JSONResponse):
        return JSONResponse(
            content=response_data,
            headers=response.headers,
            status_code=response.status_code,
        )
    return response


async def get_system_oauth_token(request, user):
    oauth_token = None
    try:
        if request.cookies.get("oauth_session_id", None):
            oauth_token = await request.app.state.oauth_manager.get_oauth_token(
                user.id,
                request.cookies.get("oauth_session_id", None),
            )
    except Exception as e:
        log.error(f"Error getting OAuth token: {e}")
    return oauth_token


async def background_tasks_handler(ctx):
    request = ctx["request"]
    form_data = ctx["form_data"]
    user = ctx["user"]
    metadata = ctx["metadata"]
    tasks = ctx["tasks"]
    event_emitter = ctx["event_emitter"]

    message = None
    messages = []

    if "chat_id" in metadata and not metadata["chat_id"].startswith("local:"):
        messages_map = Chats.get_messages_map_by_chat_id(metadata["chat_id"])
        message = messages_map.get(metadata["message_id"]) if messages_map else None

        message_list = get_message_list(messages_map, metadata["message_id"])

        # Remove details tags and files from the messages.
        # as get_message_list creates a new list, it does not affect
        # the original messages outside of this handler

        messages = []
        for message in message_list:
            content = message.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        content = item["text"]
                        break

            if isinstance(content, str):
                content = re.sub(
                    r"<details\b[^>]*>.*?<\/details>|!\[.*?\]\(.*?\)",
                    "",
                    content,
                    flags=re.S | re.I,
                ).strip()

            messages.append(
                {
                    **message,
                    "role": message.get(
                        "role", "assistant"
                    ),  # Safe fallback for missing role
                    "content": content,
                }
            )
    else:
        # Local temp chat, get the model and message from the form_data
        message = get_last_user_message_item(form_data.get("messages", []))
        messages = form_data.get("messages", [])
        if message:
            message["model"] = form_data.get("model")

    if message and "model" in message:
        if tasks and messages:
            if (
                TASKS.FOLLOW_UP_GENERATION in tasks
                and tasks[TASKS.FOLLOW_UP_GENERATION]
            ):
                res = await generate_follow_ups(
                    request,
                    {
                        "model": message["model"],
                        "messages": messages,
                        "message_id": metadata["message_id"],
                        "chat_id": metadata["chat_id"],
                    },
                    user,
                )

                if res and isinstance(res, dict):
                    if len(res.get("choices", [])) == 1:
                        response_message = res.get("choices", [])[0].get("message", {})

                        follow_ups_string = response_message.get(
                            "content"
                        ) or response_message.get("reasoning_content", "")
                    else:
                        follow_ups_string = ""

                    follow_ups_string = follow_ups_string[
                        follow_ups_string.find("{") : follow_ups_string.rfind("}") + 1
                    ]

                    try:
                        follow_ups = json.loads(follow_ups_string).get("follow_ups", [])
                        await event_emitter(
                            {
                                "type": "chat:message:follow_ups",
                                "data": {
                                    "follow_ups": follow_ups,
                                },
                            }
                        )

                        if not metadata.get("chat_id", "").startswith("local:"):
                            Chats.upsert_message_to_chat_by_id_and_message_id(
                                metadata["chat_id"],
                                metadata["message_id"],
                                {
                                    "followUps": follow_ups,
                                },
                            )

                    except Exception as e:
                        pass

            if not metadata.get("chat_id", "").startswith(
                "local:"
            ):  # Only update titles and tags for non-temp chats
                if TASKS.TITLE_GENERATION in tasks:
                    title = None
                    if tasks[TASKS.TITLE_GENERATION]:
                        res = await generate_title(
                            request,
                            {
                                "model": message["model"],
                                "messages": messages,
                                "chat_id": metadata["chat_id"],
                            },
                            user,
                        )

                        if isinstance(res, dict):
                            title = resolve_generated_chat_title(res, messages)
                            if title:
                                Chats.update_chat_title_by_id(metadata["chat_id"], title)

                                await event_emitter(
                                    {
                                        "type": "chat:title",
                                        "data": title,
                                    }
                                )

                    if title is None and len(messages) == 2:
                        title = build_fallback_chat_title(messages)
                        if title:
                            Chats.update_chat_title_by_id(metadata["chat_id"], title)

                            await event_emitter(
                                {
                                    "type": "chat:title",
                                    "data": title,
                                }
                            )

                if TASKS.TAGS_GENERATION in tasks and tasks[TASKS.TAGS_GENERATION]:
                    res = await generate_chat_tags(
                        request,
                        {
                            "model": message["model"],
                            "messages": messages,
                            "chat_id": metadata["chat_id"],
                        },
                        user,
                    )

                    if res and isinstance(res, dict):
                        if len(res.get("choices", [])) == 1:
                            response_message = res.get("choices", [])[0].get(
                                "message", {}
                            )

                            tags_string = response_message.get(
                                "content"
                            ) or response_message.get("reasoning_content", "")
                        else:
                            tags_string = ""

                        tags_string = tags_string[
                            tags_string.find("{") : tags_string.rfind("}") + 1
                        ]

                        try:
                            tags = json.loads(tags_string).get("tags", [])
                            Chats.update_chat_tags_by_id(
                                metadata["chat_id"], tags, user
                            )

                            await event_emitter(
                                {
                                    "type": "chat:tags",
                                    "data": tags,
                                }
                            )
                        except Exception as e:
                            pass


async def _run_background_tasks_detached(ctx):
    try:
        await background_tasks_handler(ctx)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Detached chat background tasks failed")


def schedule_background_tasks(ctx):
    asyncio.create_task(_run_background_tasks_detached(ctx))


async def non_streaming_chat_response_handler(response, ctx):
    request = ctx["request"]

    user = ctx["user"]
    metadata = ctx["metadata"]
    events = ctx["events"]

    event_emitter = ctx["event_emitter"]

    response, response_data = get_response_data(response)
    if response_data is None:
        return response

    if event_emitter:
        try:
            if "error" in response_data:
                error = response_data.get("error")

                if isinstance(error, dict):
                    error = error.get("detail", error)
                else:
                    error = str(error)

                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {
                        "error": {"content": error},
                    },
                )
                if isinstance(error, str) or isinstance(error, dict):
                    await event_emitter(
                        {
                            "type": "chat:message:error",
                            "data": {"error": {"content": error}},
                        }
                    )

            if "selected_model_id" in response_data:
                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {
                        "selectedModelId": response_data["selected_model_id"],
                    },
                )

            choices = response_data.get("choices", [])
            if choices and isinstance(choices[0].get("message"), dict):
                message = response_data["choices"][0]["message"]
                content = message.get("content")
                bridge_generated_files = []
                bridge_embeds = _extract_embeds_from_choices([{"message": message}])
                message_files = None

                if content:
                    content, bridge_generated_files = _collect_bridge_generated_files_from_content(
                        request,
                        content,
                        metadata,
                        user,
                    )
                    message["content"] = content

                for file_item in _extract_generated_files_from_choices([{"message": message}]):
                    ref_key = _generated_file_ref_key(file_item)
                    if ref_key and any(
                        _generated_file_ref_key(existing) == ref_key
                        for existing in bridge_generated_files
                    ):
                        continue
                    bridge_generated_files.append(file_item)

                if bridge_generated_files:
                    bridge_generated_files = _materialize_generated_files(
                        request,
                        bridge_generated_files,
                        metadata,
                        user,
                    )
                    message_metadata = message.get("metadata")
                    if not isinstance(message_metadata, dict):
                        message_metadata = {}
                        message["metadata"] = message_metadata
                    message_metadata["generated_files"] = bridge_generated_files

                if bridge_generated_files:
                    message_files = Chats.add_message_files_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        bridge_generated_files,
                    )
                    if not isinstance(message_files, list) or not message_files:
                        message_files = bridge_generated_files
                    await event_emitter(
                        {
                            "type": "files",
                            "data": {
                                "files": (
                                    message_files
                                    if isinstance(message_files, list)
                                    else bridge_generated_files
                                )
                            },
                        }
                    )

                if bridge_embeds:
                    message_metadata = message.get("metadata")
                    if not isinstance(message_metadata, dict):
                        message_metadata = {}
                        message["metadata"] = message_metadata
                    message_metadata["embeds"] = bridge_embeds
                    await event_emitter(
                        {
                            "type": "embeds",
                            "data": {
                                "embeds": bridge_embeds,
                            },
                        }
                    )

                await event_emitter(
                    {
                        "type": "chat:completion",
                        "data": response_data,
                    }
                )

                # Use output from backend if provided (OR-compliant backends),
                # otherwise generate from response content when available.
                response_output = response_data.get("output")
                if response_output is None and isinstance(message, dict):
                    response_output = message.get("output")
                if response_output is None and content:
                    response_output = [
                        {
                            "type": "message",
                            "id": output_id("msg"),
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        }
                    ]
                if response_output is None:
                    response_output = []

                response_output, response_payload, output_generated_files = (
                    _stabilize_output_generated_files(
                        request,
                        response_output or [],
                        metadata,
                        user,
                    )
                )
                combined_generated_files = _merge_generated_file_entries(
                    bridge_generated_files,
                    output_generated_files,
                )
                output_embeds = _collect_embeds_from_output_items(response_output)
                combined_embeds = _merge_embed_entries(
                    bridge_embeds,
                    output_embeds,
                    _extract_embeds_from_choices([{"message": message}]),
                )
                if combined_generated_files:
                    message_metadata = message.get("metadata")
                    if not isinstance(message_metadata, dict):
                        message_metadata = {}
                        message["metadata"] = message_metadata
                    message_metadata["generated_files"] = combined_generated_files

                    message_files = Chats.add_message_files_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        combined_generated_files,
                    )
                    if not isinstance(message_files, list) or not message_files:
                        message_files = combined_generated_files
                if combined_embeds:
                    message_metadata = message.get("metadata")
                    if not isinstance(message_metadata, dict):
                        message_metadata = {}
                        message["metadata"] = message_metadata
                    message_metadata["embeds"] = combined_embeds

                content = (
                    response_payload.get("content", "")
                    or _fallback_answer_for_completed_tool_only_output(response_output)
                    or ""
                )
                message["content"] = content
                retrieval_generated_files, retrieval_embeds = (
                    _select_retrieval_source_visual_payloads(
                        metadata,
                        content,
                        request.app.state.config,
                    )
                )
                if retrieval_generated_files:
                    combined_generated_files = _merge_generated_file_entries(
                        combined_generated_files,
                        retrieval_generated_files,
                    )
                    message_metadata = message.get("metadata")
                    if not isinstance(message_metadata, dict):
                        message_metadata = {}
                        message["metadata"] = message_metadata
                    message_metadata["generated_files"] = combined_generated_files
                    message_files = Chats.add_message_files_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        combined_generated_files,
                    )
                    if not isinstance(message_files, list) or not message_files:
                        message_files = combined_generated_files
                if retrieval_embeds:
                    combined_embeds = _merge_embed_entries(
                        combined_embeds,
                        retrieval_embeds,
                    )
                if combined_embeds:
                    message_metadata = message.get("metadata")
                    if not isinstance(message_metadata, dict):
                        message_metadata = {}
                        message["metadata"] = message_metadata
                    message_metadata["embeds"] = combined_embeds

                title = Chats.get_chat_title_by_id(metadata["chat_id"])
                _merge_reference_sidecar_into_metadata(
                    metadata,
                    sources=response_data.get("sources"),
                    tool_outputs=response_output,
                    legacy_tool_sources=True,
                )
                reference_metadata = _build_assistant_reference_persistence_metadata(
                    metadata,
                    message_metadata=message.get("metadata")
                    if isinstance(message, dict)
                    else None,
                    tool_outputs=response_output,
                )
                completion_metadata = {}
                if combined_generated_files:
                    completion_metadata["generated_files"] = combined_generated_files
                if combined_embeds:
                    completion_metadata["embeds"] = combined_embeds
                completion_metadata.update(reference_metadata)
                completion_metadata = _enforce_selected_source_metadata_first_final_consistency(
                    completion_metadata=completion_metadata,
                    metadata=metadata,
                )
                content, response_output = _apply_selected_source_diagnostics_only_content_guard(
                    content=content or "",
                    output=response_output,
                    metadata=metadata,
                    completion_metadata=completion_metadata,
                )
                message["content"] = content
                response_data["output"] = response_output
                persisted_sources = _completion_sources_for_persistence(
                    completion_metadata
                )
                suppress_completion_references = Chats._should_suppress_canonical_references(
                    metadata=completion_metadata,
                    diagnostics=completion_metadata.get("retrieval_diagnostics"),
                )
                if suppress_completion_references:
                    persisted_sources = []
                else:
                    persisted_sources = _merge_persisted_and_response_sources(
                        persisted_sources,
                        response_data.get("sources"),
                        completion_metadata=completion_metadata,
                    )
                persisted_sources = _filter_uncited_no_evidence_source_cards(
                    persisted_sources,
                    content=content or "",
                    metadata=completion_metadata,
                )
                if persisted_sources:
                    completion_metadata["canonical_references"] = persisted_sources
                    completion_metadata["reference_cards"] = Chats.build_reference_cards(
                        persisted_sources
                    )
                else:
                    completion_metadata.pop("canonical_references", None)
                    completion_metadata.pop("reference_cards", None)
                completion_metadata = _enforce_selected_source_metadata_first_final_consistency(
                    completion_metadata=completion_metadata,
                    metadata=metadata,
                )
                content, response_output = _apply_selected_source_diagnostics_only_content_guard(
                    content=content or "",
                    output=response_output,
                    metadata=metadata,
                    completion_metadata=completion_metadata,
                )
                message["content"] = content
                response_data["output"] = response_output
                if _selected_source_metadata_first_diagnostics_lane(
                    {**metadata, **completion_metadata}
                ):
                    persisted_sources = []
                    completion_metadata.pop("canonical_references", None)
                    completion_metadata.pop("reference_cards", None)

                if persisted_sources:
                    response_data["sources"] = persisted_sources
                else:
                    response_data.pop("sources", None)

                await event_emitter(
                    {
                        "type": "chat:completion",
                        "data": {
                            "done": True,
                            "content": content or "",
                            "output": response_output or [],
                            **(
                                {"files": message_files}
                                if isinstance(message_files, list) and message_files
                                else {}
                            ),
                            **(
                                {"sources": persisted_sources}
                                if persisted_sources
                                else {}
                            ),
                            **(
                                {"metadata": completion_metadata}
                                if completion_metadata
                                else {}
                            ),
                            **(
                                {"embeds": combined_embeds}
                                if combined_embeds
                                else {}
                            ),
                            "title": title,
                        },
                    }
                )

                # Save message in the database
                usage = normalize_usage(response_data.get("usage", {}) or {})

                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {
                        "role": "assistant",
                        "content": content or "",
                        "output": response_output or [],
                        **(
                            {"files": message_files}
                            if isinstance(message_files, list) and message_files
                            else {}
                        ),
                        **(
                            {"sources": persisted_sources}
                            if persisted_sources
                            else {}
                        ),
                        **({"embeds": combined_embeds} if combined_embeds else {}),
                        **({"usage": usage} if usage else {}),
                        **(
                            {"metadata": completion_metadata}
                            if completion_metadata
                            else {}
                        ),
                    },
                )

                # Send a webhook notification if the user is not active
                if not Users.is_user_active(user.id):
                    webhook_url = Users.get_user_webhook_url_by_id(user.id)
                    if webhook_url:
                        await post_webhook(
                            request.app.state.WEBUI_NAME,
                            webhook_url,
                            f"{title} - {request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}\n\n{content or ''}",
                            {
                                "action": "chat",
                                "message": content or "",
                                "title": title,
                                "url": f"{request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}",
                            },
                        )

                schedule_background_tasks(ctx)

            response = build_response_object(
                response, merge_events_into_response(response_data, events)
            )
        except Exception as e:
            log.debug(f"Error occurred while processing request: {e}")
            pass

        return response

    choices = response_data.get("choices", []) if isinstance(response_data, dict) else []
    if choices and isinstance(choices[0].get("message"), dict):
        message = choices[0]["message"]
        content = str(message.get("content") or "")
        response_output = response_data.get("output")
        if response_output is None:
            response_output = message.get("output")
        if response_output is None and content:
            response_output = [
                {
                    "type": "message",
                    "id": output_id("msg"),
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            ]
        if response_output is None:
            response_output = []
        response_output, response_payload = _build_chat_completion_payload(
            response_output,
            fallback_content=content,
        )
        content = response_payload.get("content") or _fallback_answer_for_completed_tool_only_output(
            response_output
        )
        message["content"] = content

        _merge_reference_sidecar_into_metadata(
            metadata,
            sources=response_data.get("sources"),
            tool_outputs=response_output,
            legacy_tool_sources=True,
        )
        reference_metadata = _build_assistant_reference_persistence_metadata(
            metadata,
            message_metadata=message.get("metadata"),
            tool_outputs=response_output,
        )
        completion_metadata = dict(reference_metadata)
        completion_metadata = _enforce_selected_source_metadata_first_final_consistency(
            completion_metadata=completion_metadata,
            metadata=metadata,
        )
        content, response_output = _apply_selected_source_diagnostics_only_content_guard(
            content=content or "",
            output=response_output,
            metadata=metadata,
            completion_metadata=completion_metadata,
        )
        message["content"] = content
        response_data["output"] = response_output
        persisted_sources = _completion_sources_for_persistence(completion_metadata)
        suppress_completion_references = Chats._should_suppress_canonical_references(
            metadata=completion_metadata,
            diagnostics=completion_metadata.get("retrieval_diagnostics"),
        )
        if suppress_completion_references:
            persisted_sources = []
        else:
            persisted_sources = _merge_persisted_and_response_sources(
                persisted_sources,
                response_data.get("sources"),
                completion_metadata=completion_metadata,
            )
        persisted_sources = _filter_uncited_no_evidence_source_cards(
            persisted_sources,
            content=content,
            metadata=completion_metadata,
        )
        if persisted_sources:
            completion_metadata["canonical_references"] = persisted_sources
            completion_metadata["reference_cards"] = Chats.build_reference_cards(
                persisted_sources
            )
            response_data["sources"] = persisted_sources
        else:
            completion_metadata.pop("canonical_references", None)
            completion_metadata.pop("reference_cards", None)
            response_data.pop("sources", None)
        completion_metadata = _enforce_selected_source_metadata_first_final_consistency(
            completion_metadata=completion_metadata,
            metadata=metadata,
        )
        content, response_output = _apply_selected_source_diagnostics_only_content_guard(
            content=content or "",
            output=response_output,
            metadata=metadata,
            completion_metadata=completion_metadata,
        )
        message["content"] = content
        response_data["output"] = response_output
        if _selected_source_metadata_first_diagnostics_lane(
            {**metadata, **completion_metadata}
        ):
            completion_metadata.pop("canonical_references", None)
            completion_metadata.pop("reference_cards", None)
            response_data.pop("sources", None)
            persisted_sources = []
        if completion_metadata:
            message_metadata = message.get("metadata")
            if isinstance(message_metadata, dict):
                message_metadata.update(completion_metadata)
            else:
                message["metadata"] = completion_metadata

        _persist_assistant_completion_message(
            metadata=metadata,
            content=content,
            output=response_output,
            sources=persisted_sources,
            usage=normalize_usage(response_data.get("usage", {}) or {}),
            completion_metadata=completion_metadata,
        )

        response = build_response_object(response, merge_events_into_response(response_data, events))
        return response

    if isinstance(response, dict):
        response = merge_events_into_response(response_data, events)

    return response


async def streaming_chat_response_handler(response, ctx):
    request = ctx["request"]

    form_data = ctx["form_data"]

    user = ctx["user"]
    model = ctx["model"]

    metadata = ctx["metadata"]
    events = ctx["events"]

    event_emitter = ctx["event_emitter"]
    event_caller = ctx["event_caller"]

    extra_params = {
        "__event_emitter__": event_emitter,
        "__event_call__": event_caller,
        "__user__": user.model_dump() if isinstance(user, UserModel) else {},
        "__metadata__": metadata,
        "__oauth_token__": await get_system_oauth_token(request, user),
        "__request__": request,
        "__model__": model,
    }

    filter_functions = [
        Functions.get_function_by_id(filter_id)
        for filter_id in get_sorted_filter_ids(
            request, model, metadata.get("filter_ids", [])
        )
    ]

    # Standard streaming response handler
    if event_emitter and event_caller:
        task_id = str(uuid4())  # Create a unique task ID.
        model_id = form_data.get("model", "")

        # Handle as a background task
        async def response_handler(response, events):
            def tag_output_handler(content_type, tags, output):
                """
                Detect special tags (reasoning, solution, code_interpreter) in streaming
                content and create corresponding OR-aligned output items directly.
                Operates on output items instead of content_blocks.

                Uses the text from the output items themselves for tag detection,
                eliminating state divergence between accumulated content and items.
                """
                end_flag = False

                def extract_attributes(tag_content):
                    """Extract attributes from a tag if they exist."""
                    attributes = {}
                    if not tag_content:
                        return attributes
                    matches = re.findall(r'(\w+)\s*=\s*"([^"]+)"', tag_content)
                    for key, value in matches:
                        attributes[key] = value
                    return attributes

                def get_last_text(out):
                    """Get text from last message item, or empty string."""
                    if out and out[-1].get("type") == "message":
                        parts = out[-1].get("content", [])
                        if parts and parts[-1].get("type") == "output_text":
                            return parts[-1].get("text", "")
                    return ""

                def set_last_text(out, text):
                    """Set text on last message item's output_text."""
                    if out and out[-1].get("type") == "message":
                        parts = out[-1].get("content", [])
                        if parts and parts[-1].get("type") == "output_text":
                            parts[-1]["text"] = text

                # Map content_type to output item type
                output_type_map = {
                    "reasoning": "reasoning",
                    "solution": "message",  # solution tags just produce text
                    "code_interpreter": "open_webui:code_interpreter",
                }
                output_item_type = output_type_map.get(content_type, content_type)

                last_type = output[-1].get("type", "") if output else ""

                if last_type == "message":
                    # Use the output item's own text for tag detection
                    item_text = get_last_text(output)
                    for start_tag, end_tag in tags:

                        start_tag_pattern = rf"{re.escape(start_tag)}"
                        if start_tag.startswith("<") and start_tag.endswith(">"):
                            start_tag_pattern = (
                                rf"<{re.escape(start_tag[1:-1])}(\s.*?)?>"
                            )

                        match = re.search(start_tag_pattern, item_text)
                        if match:
                            try:
                                attr_content = match.group(1) if match.group(1) else ""
                            except:
                                attr_content = ""

                            attributes = extract_attributes(attr_content)

                            before_tag = item_text[: match.start()]
                            after_tag = item_text[match.end() :]

                            # Keep only text before the tag in the message
                            set_last_text(output, before_tag)

                            if not before_tag.strip():
                                # Remove empty message item
                                if output and output[-1].get("type") == "message":
                                    output.pop()

                            # Append the new output item
                            if output_item_type == "reasoning":
                                output.append(
                                    {
                                        "type": "reasoning",
                                        "id": output_id("r"),
                                        "status": "in_progress",
                                        "start_tag": start_tag,
                                        "end_tag": end_tag,
                                        "attributes": attributes,
                                        "content": [],
                                        "summary": None,
                                        "started_at": time.time(),
                                    }
                                )
                            elif output_item_type == "open_webui:code_interpreter":
                                output.append(
                                    {
                                        "type": "open_webui:code_interpreter",
                                        "id": output_id("ci"),
                                        "status": "in_progress",
                                        "start_tag": start_tag,
                                        "end_tag": end_tag,
                                        "attributes": attributes,
                                        "lang": attributes.get("lang", "python"),
                                        "code": "",
                                        "output": None,
                                        "started_at": time.time(),
                                    }
                                )
                            else:
                                # solution or other text-producing tag
                                output.append(
                                    {
                                        "type": "message",
                                        "id": output_id("msg"),
                                        "status": "in_progress",
                                        "role": "assistant",
                                        "content": [
                                            {"type": "output_text", "text": ""}
                                        ],
                                        "_tag_type": content_type,
                                        "start_tag": start_tag,
                                        "end_tag": end_tag,
                                        "attributes": attributes,
                                        "started_at": time.time(),
                                    }
                                )

                            if after_tag:
                                # Set the after_tag content on the new item
                                if output_item_type == "reasoning":
                                    output[-1]["content"] = [
                                        {"type": "output_text", "text": after_tag}
                                    ]
                                elif output_item_type == "open_webui:code_interpreter":
                                    output[-1]["code"] = after_tag
                                else:
                                    set_last_text(output, after_tag)

                                _, recursive_end = tag_output_handler(
                                    content_type, tags, output
                                )
                                if recursive_end:
                                    end_flag = True

                            break

                elif (
                    (last_type == "reasoning" and content_type == "reasoning")
                    or (
                        last_type == "open_webui:code_interpreter"
                        and content_type == "code_interpreter"
                    )
                    or (
                        last_type == "message"
                        and output[-1].get("_tag_type") == content_type
                    )
                ):
                    item = output[-1]
                    start_tag = item.get("start_tag", "")
                    end_tag = item.get("end_tag", "")

                    end_tag_pattern = rf"{re.escape(end_tag)}"

                    # Get the block content from the item itself
                    if last_type == "reasoning":
                        parts = item.get("content", [])
                        block_content = ""
                        if parts and parts[-1].get("type") == "output_text":
                            block_content = parts[-1].get("text", "")
                    elif last_type == "open_webui:code_interpreter":
                        block_content = item.get("code", "")
                    else:
                        block_content = get_last_text(output)

                    if re.search(end_tag_pattern, block_content):
                        end_flag = True

                        # Strip start and end tags from content
                        start_tag_pattern = rf"{re.escape(start_tag)}"
                        if start_tag.startswith("<") and start_tag.endswith(">"):
                            start_tag_pattern = (
                                rf"<{re.escape(start_tag[1:-1])}(\s.*?)?>"
                            )
                        block_content = re.sub(
                            start_tag_pattern, "", block_content
                        ).strip()

                        end_tag_regex = re.compile(end_tag_pattern, re.DOTALL)
                        split_content = end_tag_regex.split(block_content, maxsplit=1)

                        block_content = (
                            split_content[0].strip() if split_content else ""
                        )
                        leftover_content = (
                            split_content[1].strip() if len(split_content) > 1 else ""
                        )

                        if block_content:
                            # Update the item with final content
                            if last_type == "reasoning":
                                item["content"] = [
                                    {"type": "output_text", "text": block_content}
                                ]
                                item["ended_at"] = time.time()
                                item["duration"] = int(
                                    item["ended_at"] - item["started_at"]
                                )
                                item["status"] = "completed"
                            elif last_type == "open_webui:code_interpreter":
                                item["code"] = block_content
                                item["ended_at"] = time.time()
                                item["duration"] = int(
                                    item["ended_at"] - item["started_at"]
                                )
                            else:
                                set_last_text(output, block_content)
                                item["ended_at"] = time.time()

                            # Reset by appending a new message item for leftover
                            output.append(
                                {
                                    "type": "message",
                                    "id": output_id("msg"),
                                    "status": "in_progress",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": leftover_content,
                                        }
                                    ],
                                }
                            )
                        else:
                            # Remove the block if content is empty
                            output.pop()
                            output.append(
                                {
                                    "type": "message",
                                    "id": output_id("msg"),
                                    "status": "in_progress",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": leftover_content,
                                        }
                                    ],
                                }
                            )

                return output, end_flag

            message = Chats.get_message_by_id_and_message_id(
                metadata["chat_id"], metadata["message_id"]
            )

            tool_calls = []

            last_assistant_message = None
            try:
                if form_data["messages"][-1]["role"] == "assistant":
                    last_assistant_message = get_last_assistant_message(
                        form_data["messages"]
                    )
            except Exception as e:
                pass

            content = (
                message.get("content", "")
                if message
                else last_assistant_message if last_assistant_message else ""
            )

            # Initialize output: use existing from message if continuing, else create new
            existing_output = message.get("output") if message else None
            if existing_output:
                output = _normalize_output_for_chat(existing_output)
                content = _serialize_output_for_chat_content(
                    output,
                    fallback_content=content,
                )
            else:
                # Only create an initial message item if there is content to initialize with
                if content:
                    output = [
                        {
                            "type": "message",
                            "id": output_id("msg"),
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        }
                    ]
                else:
                    output = []

            accumulated_embeds = _merge_embed_entries(
                message.get("embeds", []) if isinstance(message, dict) else []
            )
            usage = None
            stream_done = False
            has_visible_status_event = any(
                isinstance(event, dict)
                and event.get("type") == "status"
                and not bool((event.get("data") or {}).get("hidden"))
                for event in events
            )
            waiting_status_emitted = bool(metadata.get("waiting_status_active"))
            waiting_status_cleared = False

            reasoning_tags_param = metadata.get("params", {}).get("reasoning_tags")
            DETECT_REASONING_TAGS = reasoning_tags_param is not False
            DETECT_CODE_INTERPRETER = metadata.get("features", {}).get(
                "code_interpreter", False
            )

            reasoning_tags = []
            if DETECT_REASONING_TAGS:
                if (
                    isinstance(reasoning_tags_param, list)
                    and len(reasoning_tags_param) == 2
                ):
                    reasoning_tags = [
                        (reasoning_tags_param[0], reasoning_tags_param[1])
                    ]
                else:
                    reasoning_tags = DEFAULT_REASONING_TAGS

            reference_seed_metadata = _build_assistant_reference_seed_metadata(
                metadata
            )

            async def emit_waiting_status_if_needed():
                nonlocal waiting_status_emitted
                if waiting_status_emitted or has_visible_status_event or content or output:
                    return
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "action": "chat",
                            "description": "处理中",
                            "done": False,
                        },
                    }
                )
                waiting_status_emitted = True
                metadata["waiting_status_active"] = True

            async def clear_waiting_status():
                nonlocal waiting_status_cleared
                if not waiting_status_emitted or waiting_status_cleared:
                    return
                waiting_status_cleared = True
                metadata["waiting_status_active"] = False
                await event_emitter(
                    {
                        "type": "status",
                        "data": {
                            "action": "chat",
                            "description": "处理中",
                            "done": True,
                            "hidden": True,
                        },
                    }
                )

            try:
                if reference_seed_metadata:
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {"metadata": reference_seed_metadata},
                    )

                await emit_waiting_status_if_needed()
                for event in events:
                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": event,
                        }
                    )

                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            **event,
                        },
                    )

                async def stream_body_handler(response, form_data):
                    nonlocal content
                    nonlocal usage
                    nonlocal output
                    nonlocal stream_done
                    nonlocal accumulated_embeds

                    response_tool_calls = []

                    delta_count = 0
                    delta_chunk_size = max(
                        CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE,
                        int(
                            metadata.get("params", {}).get("stream_delta_chunk_size")
                            or 1
                        ),
                    )
                    last_delta_data = None
                    emitted_generated_file_refs: set[str] = set()
                    emitted_embed_refs: set[str] = set()

                    async def apply_bridge_output_events(raw_events: Any) -> bool:
                        nonlocal output
                        nonlocal delta_count
                        nonlocal last_delta_data
                        events = _normalize_bridge_output_events(raw_events)
                        if not events:
                            return False
                        applied_any = False
                        for event in events:
                            if not isinstance(event, dict):
                                continue
                            applied_any = True
                            if str(event.get("type", "")).startswith("response."):
                                event_payload = event
                            else:
                                event_payload = {
                                    "type": "response.output_item.added",
                                    "item": event,
                                }
                            output, response_metadata = handle_responses_streaming_event(
                                event_payload, output
                            )
                            output, data = _build_chat_completion_payload(
                                output,
                                fallback_content=content,
                            )
                            if response_metadata:
                                data.update(response_metadata)
                            last_delta_data = data
                            delta_count += 1
                            # Bridge-owned tool/process events should stay visibly ordered.
                            await clear_waiting_status()
                            await flush_pending_delta_data(1)
                        return applied_any

                    async def flush_pending_delta_data(threshold: int = 0):
                        nonlocal delta_count
                        nonlocal last_delta_data

                        if delta_count >= threshold and last_delta_data:
                            await event_emitter(
                                {
                                    "type": "chat:completion",
                                    "data": last_delta_data,
                                }
                            )
                            delta_count = 0
                            last_delta_data = None

                    stream_content_type = str(
                        response.headers.get("Content-Type", "") or ""
                    ).lower()

                    def build_sse_data_payload(event_lines: list[str]) -> Optional[str]:
                        data_lines = []
                        for line in event_lines:
                            if not line or line.startswith(":"):
                                continue
                            if ":" in line:
                                field, value = line.split(":", 1)
                                if value.startswith(" "):
                                    value = value[1:]
                            else:
                                field = line
                                value = ""
                            if field == "data":
                                data_lines.append(value)

                        if not data_lines:
                            return None

                        return "\n".join(data_lines)

                    async def iter_stream_payloads():
                        buffer = ""
                        is_ndjson_stream = (
                            "application/x-ndjson" in stream_content_type
                        )
                        event_lines: list[str] = []

                        async for chunk in response.body_iterator:
                            text = (
                                chunk.decode("utf-8", "replace")
                                if isinstance(chunk, bytes)
                                else str(chunk)
                            )
                            if not text:
                                continue

                            text = text.replace("\r\n", "\n").replace("\r", "\n")
                            buffer += text

                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                if is_ndjson_stream:
                                    line = line.strip()
                                    if line:
                                        yield line
                                    continue

                                if line == "":
                                    data_payload = build_sse_data_payload(event_lines)
                                    if data_payload is not None:
                                        yield data_payload
                                    event_lines = []
                                    continue

                                event_lines.append(line)

                        if is_ndjson_stream:
                            trailing_line = buffer.strip()
                            if trailing_line:
                                yield trailing_line
                            return

                        if buffer:
                            event_lines.append(buffer)
                        if event_lines:
                            data_payload = build_sse_data_payload(event_lines)
                            if data_payload is not None:
                                yield data_payload

                    async for data_payload in iter_stream_payloads():
                        data_payload_stripped = data_payload.strip()
                        if data_payload_stripped == "[DONE]":
                            stream_done = True
                            continue

                        try:
                            data = json.loads(data_payload)

                            data, _ = await process_filter_functions(
                                request=request,
                                filter_functions=filter_functions,
                                filter_type="stream",
                                form_data=data,
                                extra_params={"__body__": form_data, **extra_params},
                            )

                            if data:
                                if "event" in data and not getattr(
                                    request.state, "direct", False
                                ):
                                    await event_emitter(data.get("event", {}))

                                if "selected_model_id" in data:
                                    model_id = data["selected_model_id"]
                                    Chats.upsert_message_to_chat_by_id_and_message_id(
                                        metadata["chat_id"],
                                        metadata["message_id"],
                                        {
                                            "selectedModelId": model_id,
                                        },
                                    )
                                    await event_emitter(
                                        {
                                            "type": "chat:completion",
                                            "data": data,
                                        }
                                    )
                                # Check for Responses API events (type field starts with "response.")
                                elif data.get("type", "").startswith("response."):
                                    output, response_metadata = (
                                        handle_responses_streaming_event(data, output)
                                    )

                                    output, processed_data = (
                                        _build_chat_completion_payload(
                                            output,
                                            fallback_content=content,
                                        )
                                    )

                                    # print(data)
                                    # print(processed_data)

                                    # Merge any metadata (usage, done, etc.)
                                    if response_metadata:
                                        processed_data.update(response_metadata)

                                    if processed_data.get("content") or processed_data.get("output"):
                                        await clear_waiting_status()
                                    await event_emitter(
                                        {
                                            "type": "chat:completion",
                                            "data": processed_data,
                                        }
                                    )
                                    continue
                                else:
                                    sources = data.get("sources", None)
                                    if isinstance(sources, list):
                                        for source in sources:
                                            if isinstance(source, dict):
                                                await event_emitter(
                                                    {
                                                        "type": "source",
                                                        "data": source,
                                                    }
                                                )
                                    elif isinstance(sources, dict):
                                        await event_emitter(
                                            {
                                                "type": "source",
                                                "data": sources,
                                            }
                                        )

                                    choices = data.get("choices", [])

                                    # Normalize usage data to standard format
                                    raw_usage = data.get("usage", {}) or {}
                                    raw_usage.update(
                                        data.get("timings", {})
                                    )  # llama.cpp
                                    if raw_usage:
                                        usage = normalize_usage(raw_usage)
                                        await event_emitter(
                                            {
                                                "type": "chat:completion",
                                                "data": {
                                                    "usage": usage,
                                                },
                                            }
                                        )

                                    if not choices:
                                        error = data.get("error", {})
                                        if error:
                                            await clear_waiting_status()
                                            await event_emitter(
                                                {
                                                    "type": "chat:completion",
                                                    "data": {
                                                        "error": error,
                                                    },
                                                }
                                            )
                                        continue

                                    delta = choices[0].get("delta", {})
                                    delta_generated_files = (
                                        _extract_generated_files_from_choices(
                                            [{"delta": delta}]
                                        )
                                    )
                                    delta_embeds = _extract_embeds_from_choices(
                                        [{"delta": delta}]
                                    )
                                    new_generated_files = []
                                    if delta_generated_files:
                                        for file_item in delta_generated_files:
                                            ref_key = _generated_file_ref_key(file_item)
                                            if ref_key and ref_key in emitted_generated_file_refs:
                                                continue
                                            if ref_key:
                                                emitted_generated_file_refs.add(ref_key)
                                            new_generated_files.append(file_item)

                                        if new_generated_files:
                                            new_generated_files = _materialize_generated_files(
                                                request,
                                                new_generated_files,
                                                metadata,
                                                user,
                                            )
                                            delta_metadata = delta.get("metadata")
                                            if isinstance(delta_metadata, dict):
                                                delta_metadata["generated_files"] = (
                                                    new_generated_files
                                                )

                                    if delta_embeds:
                                        normalized_embeds = []
                                        for embed in _merge_embed_entries(delta_embeds):
                                            if embed in emitted_embed_refs:
                                                continue
                                            emitted_embed_refs.add(embed)
                                            normalized_embeds.append(embed)
                                        if not normalized_embeds:
                                            normalized_embeds = []
                                        if normalized_embeds:
                                            accumulated_embeds = _merge_embed_entries(
                                                accumulated_embeds,
                                                normalized_embeds,
                                            )
                                        delta_metadata = delta.get("metadata")
                                        if isinstance(delta_metadata, dict):
                                            delta_metadata["embeds"] = normalized_embeds
                                        if normalized_embeds:
                                            await clear_waiting_status()
                                            await event_emitter(
                                                {
                                                    "type": "embeds",
                                                    "data": {
                                                        "embeds": normalized_embeds,
                                                    },
                                                }
                                            )

                                    if new_generated_files:
                                        await clear_waiting_status()
                                        output = _attach_generated_files_to_output(
                                            output,
                                            new_generated_files,
                                        )
                                        message_files = Chats.add_message_files_by_id_and_message_id(
                                            metadata["chat_id"],
                                            metadata["message_id"],
                                            new_generated_files,
                                        )
                                        if (
                                            not isinstance(message_files, list)
                                            or not message_files
                                        ):
                                            message_files = new_generated_files
                                        await event_emitter(
                                            {
                                                "type": "files",
                                                "data": {
                                                    "files": (
                                                        message_files
                                                        if isinstance(message_files, list)
                                                        else new_generated_files
                                                    )
                                                },
                                            }
                                        )

                                    delta_metadata = delta.get("metadata")
                                    bridge_events_seen = False
                                    if isinstance(delta_metadata, dict):
                                        raw_bridge_events = (
                                            delta_metadata.get("bridge_output_events")
                                            or delta_metadata.get("bridgeOutputEvents")
                                        )
                                        if raw_bridge_events:
                                            bridge_events_seen = await apply_bridge_output_events(
                                                raw_bridge_events
                                            )
                                            if last_delta_data:
                                                data = last_delta_data

                                    # Handle delta annotations
                                    annotations = delta.get("annotations")
                                    if annotations:
                                        for annotation in annotations:
                                            if (
                                                annotation.get("type") == "url_citation"
                                                and "url_citation" in annotation
                                            ):
                                                url_citation = annotation[
                                                    "url_citation"
                                                ]

                                                url = url_citation.get("url", "")
                                                title = url_citation.get("title", url)

                                                await event_emitter(
                                                    {
                                                        "type": "source",
                                                        "data": {
                                                            "source": {
                                                                "name": title,
                                                                "url": url,
                                                            },
                                                            "document": [title],
                                                            "metadata": [
                                                                {
                                                                    "source": url,
                                                                    "name": title,
                                                                }
                                                            ],
                                                        },
                                                    }
                                                )

                                    delta_tool_calls = delta.get("tool_calls", None)
                                    if delta_tool_calls:
                                        for delta_tool_call in delta_tool_calls:
                                            tool_call_index = delta_tool_call.get(
                                                "index"
                                            )

                                            if tool_call_index is not None:
                                                # Check if the tool call already exists
                                                current_response_tool_call = None
                                                for (
                                                    response_tool_call
                                                ) in response_tool_calls:
                                                    if (
                                                        response_tool_call.get("index")
                                                        == tool_call_index
                                                    ):
                                                        current_response_tool_call = (
                                                            response_tool_call
                                                        )
                                                        break

                                                if current_response_tool_call is None:
                                                    # Add the new tool call
                                                    delta_tool_call.setdefault(
                                                        "function", {}
                                                    )
                                                    delta_tool_call[
                                                        "function"
                                                    ].setdefault("name", "")
                                                    delta_tool_call[
                                                        "function"
                                                    ].setdefault("arguments", "")
                                                    response_tool_calls.append(
                                                        delta_tool_call
                                                    )
                                                else:
                                                    # Update the existing tool call
                                                    delta_name = delta_tool_call.get(
                                                        "function", {}
                                                    ).get("name")
                                                    delta_arguments = (
                                                        delta_tool_call.get(
                                                            "function", {}
                                                        ).get("arguments")
                                                    )

                                                    if delta_name:
                                                        current_response_tool_call[
                                                            "function"
                                                        ]["name"] = delta_name

                                                    if delta_arguments:
                                                        current_response_tool_call[
                                                            "function"
                                                        ][
                                                            "arguments"
                                                        ] += delta_arguments

                                        # Emit pending tool calls in real-time
                                        if response_tool_calls:
                                            # Flush any pending text first
                                            await clear_waiting_status()
                                            await flush_pending_delta_data()
                                            output = (
                                                strip_leading_message_output_before_tool_call(
                                                    output
                                                )
                                            )

                                            # Build pending function_call output items for display
                                            pending_fc_items = []
                                            for tc in response_tool_calls:
                                                call_id = tc.get("id", "")
                                                func = tc.get("function", {})
                                                pending_fc_items.append(
                                                    {
                                                        "type": "function_call",
                                                        "id": call_id
                                                        or output_id("fc"),
                                                        "call_id": call_id,
                                                        "name": func.get("name", ""),
                                                        "arguments": func.get(
                                                            "arguments", "{}"
                                                        ),
                                                        "status": "in_progress",
                                                    }
                                                )
                                            pending_output = (
                                                strip_leading_message_output_before_tool_call(
                                                    output + pending_fc_items
                                                )
                                            )
                                            await event_emitter(
                                                {
                                                    "type": "chat:completion",
                                                    "data": _build_chat_completion_payload(
                                                        pending_output,
                                                        fallback_content=content,
                                                    )[1],
                                                }
                                            )

                                    image_urls = get_image_urls(
                                        delta.get("images", []), request, metadata, user
                                    )
                                    if image_urls:
                                        await clear_waiting_status()
                                        image_file_list = [
                                            {"type": "image", "url": url}
                                            for url in image_urls
                                        ]
                                        message_files = Chats.add_message_files_by_id_and_message_id(
                                            metadata["chat_id"],
                                            metadata["message_id"],
                                            image_file_list,
                                        )
                                        if message_files is None:
                                            message_files = image_file_list

                                        await event_emitter(
                                            {
                                                "type": "files",
                                                "data": {"files": message_files},
                                            }
                                        )

                                    value = delta.get("content")
                                    if isinstance(value, str):
                                        value = _strip_bridge_details_blocks(value)

                                    reasoning_content = (
                                        delta.get("reasoning_content")
                                        or delta.get("reasoning")
                                        or delta.get("thinking")
                                    )
                                    if reasoning_content:
                                        await clear_waiting_status()
                                        if (
                                            not output
                                            or output[-1].get("type") != "reasoning"
                                        ):
                                            reasoning_item = {
                                                "type": "reasoning",
                                                "id": output_id("r"),
                                                "status": "in_progress",
                                                "start_tag": "<think>",
                                                "end_tag": "</think>",
                                                "attributes": {
                                                    "type": "reasoning_content"
                                                },
                                                "content": [],
                                                "summary": None,
                                                "started_at": time.time(),
                                            }
                                            output.append(reasoning_item)
                                        else:
                                            reasoning_item = output[-1]

                                        # Append to reasoning content
                                        parts = reasoning_item.get("content", [])
                                        if (
                                            parts
                                            and parts[-1].get("type") == "output_text"
                                        ):
                                            parts[-1]["text"] += reasoning_content
                                        else:
                                            reasoning_item["content"] = [
                                                {
                                                    "type": "output_text",
                                                    "text": reasoning_content,
                                                }
                                            ]

                                        output, data = _build_chat_completion_payload(
                                            output,
                                            fallback_content=content,
                                        )

                                    if value:
                                        await clear_waiting_status()
                                        if (
                                            output
                                            and output[-1].get("type") == "reasoning"
                                            and output[-1]
                                            .get("attributes", {})
                                            .get("type")
                                            == "reasoning_content"
                                        ):
                                            reasoning_item = output[-1]
                                            reasoning_item["ended_at"] = time.time()
                                            reasoning_item["duration"] = int(
                                                reasoning_item["ended_at"]
                                                - reasoning_item["started_at"]
                                            )
                                            reasoning_item["status"] = "completed"

                                            output.append(
                                                {
                                                    "type": "message",
                                                    "id": output_id("msg"),
                                                    "status": "in_progress",
                                                    "role": "assistant",
                                                    "content": [
                                                        {
                                                            "type": "output_text",
                                                            "text": "",
                                                        }
                                                    ],
                                                }
                                            )

                                        if ENABLE_CHAT_RESPONSE_BASE64_IMAGE_URL_CONVERSION:
                                            value = convert_markdown_base64_images(
                                                request,
                                                value,
                                                {
                                                    "chat_id": metadata.get(
                                                        "chat_id", None
                                                    ),
                                                    "message_id": metadata.get(
                                                        "message_id", None
                                                    ),
                                                },
                                                user,
                                            )

                                        content = f"{content}{value}"

                                        # Check if we're inside a tag-based block
                                        # (reasoning, code_interpreter, or solution).
                                        # If so, append to the existing in-progress
                                        # item instead of creating a new message —
                                        # otherwise tag_output_handler re-detects the
                                        # start tag on every chunk and fragments the
                                        # output.
                                        last_item = output[-1] if output else None
                                        last_item_type = (
                                            last_item.get("type", "")
                                            if last_item
                                            else ""
                                        )
                                        inside_tag_block = (
                                            last_item is not None
                                            and last_item.get("status") == "in_progress"
                                            and last_item.get("attributes", {}).get(
                                                "type"
                                            )
                                            != "reasoning_content"
                                            and (
                                                last_item_type == "reasoning"
                                                or last_item_type
                                                == "open_webui:code_interpreter"
                                                or (
                                                    last_item_type == "message"
                                                    and last_item.get("_tag_type")
                                                    is not None
                                                )
                                            )
                                        )

                                        if inside_tag_block:
                                            # Append to the existing tag-based item
                                            if (
                                                last_item_type
                                                == "open_webui:code_interpreter"
                                            ):
                                                last_item["code"] = (
                                                    last_item.get("code", "") + value
                                                )
                                            elif last_item_type == "reasoning":
                                                parts = last_item.get("content", [])
                                                if (
                                                    parts
                                                    and parts[-1].get("type")
                                                    == "output_text"
                                                ):
                                                    parts[-1]["text"] += value
                                                else:
                                                    last_item["content"] = [
                                                        {
                                                            "type": "output_text",
                                                            "text": value,
                                                        }
                                                    ]
                                            else:
                                                # solution or other _tag_type message
                                                msg_parts = last_item.get("content", [])
                                                if (
                                                    msg_parts
                                                    and msg_parts[-1].get("type")
                                                    == "output_text"
                                                ):
                                                    msg_parts[-1]["text"] += value
                                                else:
                                                    last_item["content"] = [
                                                        {
                                                            "type": "output_text",
                                                            "text": value,
                                                        }
                                                    ]
                                        else:
                                            if (
                                                not output
                                                or output[-1].get("type") != "message"
                                            ):
                                                output.append(
                                                    {
                                                        "type": "message",
                                                        "id": output_id("msg"),
                                                        "status": "in_progress",
                                                        "role": "assistant",
                                                        "content": [
                                                            {
                                                                "type": "output_text",
                                                                "text": "",
                                                            }
                                                        ],
                                                    }
                                                )

                                            # Append value to last message item's text
                                            msg_parts = output[-1].get("content", [])
                                            if (
                                                msg_parts
                                                and msg_parts[-1].get("type")
                                                == "output_text"
                                            ):
                                                msg_parts[-1]["text"] += value
                                            else:
                                                output[-1]["content"] = [
                                                    {
                                                        "type": "output_text",
                                                        "text": value,
                                                    }
                                                ]

                                        if DETECT_REASONING_TAGS and not bridge_events_seen:
                                            output, _ = tag_output_handler(
                                                "reasoning",
                                                reasoning_tags,
                                                output,
                                            )

                                            output, _ = tag_output_handler(
                                                "solution",
                                                DEFAULT_SOLUTION_TAGS,
                                                output,
                                            )

                                        if DETECT_CODE_INTERPRETER and not bridge_events_seen:
                                            output, end = tag_output_handler(
                                                "code_interpreter",
                                                DEFAULT_CODE_INTERPRETER_TAGS,
                                                output,
                                            )

                                            if end:
                                                break

                                        if ENABLE_REALTIME_CHAT_SAVE:
                                            # Save message in the database
                                            output, persisted_data = _build_chat_completion_payload(
                                                output,
                                                fallback_content=content,
                                            )
                                            Chats.upsert_message_to_chat_by_id_and_message_id(
                                                metadata["chat_id"],
                                                metadata["message_id"],
                                                {
                                                    "role": "assistant",
                                                    **persisted_data,
                                                },
                                            )
                                        else:
                                            output, data = _build_chat_completion_payload(
                                                output,
                                                fallback_content=content,
                                            )

                                if delta:
                                    delta_count += 1
                                    last_delta_data = data
                                    if delta_count >= delta_chunk_size:
                                        await flush_pending_delta_data(delta_chunk_size)
                                else:
                                    await event_emitter(
                                        {
                                            "type": "chat:completion",
                                            "data": data,
                                        }
                                    )
                        except Exception as e:
                            log.debug(f"Error: {e}")
                            continue
                    await flush_pending_delta_data()

                    if output:
                        # Clean up the last message item
                        if output[-1].get("type") == "message":
                            parts = output[-1].get("content", [])
                            if parts and parts[-1].get("type") == "output_text":
                                parts[-1]["text"] = parts[-1]["text"].strip()

                                if not parts[-1]["text"]:
                                    output.pop()

                                    if not output:
                                        output.append(
                                            {
                                                "type": "message",
                                                "id": output_id("msg"),
                                                "status": "in_progress",
                                                "role": "assistant",
                                                "content": [
                                                    {"type": "output_text", "text": ""}
                                                ],
                                            }
                                        )

                        if output[-1].get("type") == "reasoning":
                            reasoning_item = output[-1]
                            if reasoning_item.get("ended_at") is None:
                                reasoning_item["ended_at"] = time.time()
                                reasoning_item["duration"] = int(
                                    reasoning_item["ended_at"]
                                    - reasoning_item["started_at"]
                                )
                                reasoning_item["status"] = "completed"

                    if response_tool_calls:
                        tool_calls.append(_split_tool_calls(response_tool_calls))

                    if response.background:
                        await response.background()

                await stream_body_handler(response, form_data)

                tool_call_retries = 0
                tool_call_sources = []  # Track citation sources from tool results
                all_tool_call_sources = []  # Accumulated sources across all iterations
                user_message = get_last_user_message(form_data["messages"])

                # Check if citations are enabled for this model
                citations_enabled = (
                    model.get("info", {}).get("meta", {}).get("capabilities") or {}
                ).get("citations", True)

                # Use the pre-RAG system content captured before the
                # initial file-source injection in process_chat_payload.
                # This ensures restore truly undoes the RAG template.
                original_system_content = metadata.get("system_prompt")
                if original_system_content is None:
                    original_system_message = get_system_message(form_data["messages"])
                    original_system_content = (
                        get_content_from_message(original_system_message)
                        if original_system_message
                        else None
                    )

                while len(tool_calls) > 0 and (
                    CHAT_RESPONSE_MAX_TOOL_CALL_RETRIES <= 0
                    or tool_call_retries < CHAT_RESPONSE_MAX_TOOL_CALL_RETRIES
                ):

                    tool_call_retries += 1

                    response_tool_calls = tool_calls.pop(0)

                    # Append function_call items for each tool call
                    for tc in response_tool_calls:
                        call_id = tc.get("id", "")
                        func = tc.get("function", {})
                        output.append(
                            {
                                "type": "function_call",
                                "id": call_id or output_id("fc"),
                                "call_id": call_id,
                                "name": func.get("name", ""),
                                "arguments": func.get("arguments", "{}"),
                                "status": "in_progress",
                            }
                        )
                    output = strip_leading_message_output_before_tool_call(output)

                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": _build_chat_completion_payload(
                                output,
                                fallback_content=content,
                            )[1],
                        }
                    )

                    tools = metadata.get("tools", {})

                    results = []

                    for tool_call in response_tool_calls:
                        tool_call_id = tool_call.get("id", "")
                        tool_function_name = tool_call.get("function", {}).get(
                            "name", ""
                        )
                        tool_args = tool_call.get("function", {}).get("arguments", "{}")

                        tool_function_params = {}
                        if tool_args and tool_args.strip():
                            try:
                                # json.loads cannot be used because some models do not produce valid JSON
                                tool_function_params = ast.literal_eval(tool_args)
                            except Exception as e:
                                log.debug(e)
                                # Fallback to JSON parsing
                                try:
                                    tool_function_params = json.loads(tool_args)
                                except Exception as e:
                                    log.error(
                                        f"Error parsing tool call arguments: {tool_args}"
                                    )
                                    results.append(
                                        {
                                            "tool_call_id": tool_call_id,
                                            "content": f"Error: Tool call arguments could not be parsed. The model generated malformed or incomplete JSON for `{tool_function_name}`. Please try again.",
                                        }
                                    )
                                    continue

                        # Ensure arguments are valid JSON for downstream LLM integrations
                        log.debug(
                            f"Parsed args from {tool_args} to {tool_function_params}"
                        )
                        tool_call.setdefault("function", {})["arguments"] = json.dumps(
                            tool_function_params
                        )

                        tool_result = None
                        tool = None
                        tool_type = None
                        direct_tool = False

                        if tool_function_name in tools:
                            tool = tools[tool_function_name]
                            spec = tool.get("spec", {})

                            tool_type = tool.get("type", "")
                            direct_tool = tool.get("direct", False)

                            try:
                                allowed_params = (
                                    spec.get("parameters", {})
                                    .get("properties", {})
                                    .keys()
                                )

                                tool_function_params = {
                                    k: v
                                    for k, v in tool_function_params.items()
                                    if k in allowed_params
                                }

                                if direct_tool:
                                    tool_result = await event_caller(
                                        {
                                            "type": "execute:tool",
                                            "data": {
                                                "id": str(uuid4()),
                                                "name": tool_function_name,
                                                "params": tool_function_params,
                                                "server": tool.get("server", {}),
                                                "session_id": metadata.get(
                                                    "session_id", None
                                                ),
                                            },
                                        }
                                    )

                                else:
                                    tool_function = get_updated_tool_function(
                                        function=tool["callable"],
                                        extra_params={
                                            "__messages__": form_data.get(
                                                "messages", []
                                            ),
                                            "__files__": metadata.get("files", []),
                                        },
                                    )

                                    tool_result = await tool_function(
                                        **tool_function_params
                                    )

                            except Exception as e:
                                tool_result = str(e)

                        tool_result, tool_result_files, tool_result_embeds = (
                            process_tool_result(
                                request,
                                tool_function_name,
                                tool_result,
                                tool_type,
                                direct_tool,
                                metadata,
                                user,
                            )
                        )

                        await terminal_event_handler(
                            tool_function_name,
                            tool_function_params,
                            tool_result,
                            event_emitter,
                        )

                        # Extract citation sources from tool results
                        if (
                            citations_enabled
                            and tool_function_name
                            in [
                                "search_web",
                                "fetch_url",
                                "view_knowledge_file",
                                "query_knowledge_files",
                                "query_selected_knowledge_files",
                                "read_selected_file",
                            ]
                            and tool_result
                        ):
                            try:
                                citation_sources = get_citation_source_from_tool_result(
                                    tool_name=tool_function_name,
                                    tool_params=tool_function_params,
                                    tool_result=tool_result,
                                    tool_id=tool.get("tool_id", "") if tool else "",
                                )
                                tool_call_sources.extend(citation_sources)
                            except Exception as e:
                                log.exception(f"Error extracting citation source: {e}")

                        results.append(
                            {
                                "tool_call_id": tool_call_id,
                                "content": str(tool_result) if tool_result else "",
                                **(
                                    {"files": tool_result_files}
                                    if tool_result_files
                                    else {}
                                ),
                                **(
                                    {"embeds": tool_result_embeds}
                                    if tool_result_embeds
                                    else {}
                                ),
                            }
                        )

                    # Update function_call statuses and append function_call_output items
                    for tc in response_tool_calls:
                        call_id = tc.get("id", "")
                        # Mark function_call as completed
                        for item in output:
                            if (
                                item.get("type") == "function_call"
                                and item.get("call_id") == call_id
                            ):
                                item["status"] = "completed"
                                # Update arguments with parsed/sanitized version
                                item["arguments"] = tc.get("function", {}).get(
                                    "arguments", "{}"
                                )
                                break

                    for result in results:
                        output.append(
                            {
                                "type": "function_call_output",
                                "id": output_id("fco"),
                                "call_id": result.get("tool_call_id", ""),
                                "output": [
                                    {
                                        "type": "input_text",
                                        "text": result.get("content", ""),
                                    }
                                ],
                                "status": "completed",
                                **(
                                    {"files": result.get("files")}
                                    if result.get("files")
                                    else {}
                                ),
                                **(
                                    {"embeds": result.get("embeds")}
                                    if result.get("embeds")
                                    else {}
                                ),
                            }
                        )

                    # Append a new empty message item for the next response
                    output.append(
                        {
                            "type": "message",
                            "id": output_id("msg"),
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": ""}],
                        }
                    )

                    # Emit citation sources to the frontend for display
                    if citations_enabled:
                        for source in tool_call_sources:
                            await event_emitter({"type": "source", "data": source})

                        # Apply tool source context to messages for the model.
                        # Restoring to pre-RAG original prevents duplicating
                        # the RAG template across file and tool sources.
                        all_tool_call_sources.extend(tool_call_sources)
                        if all_tool_call_sources and user_message:
                            # Restore pre-RAG message state before re-applying
                            # to prevent RAG template duplication.
                            original_user_message = (
                                metadata.get("user_prompt") or user_message
                            )
                            set_last_user_message_content(
                                original_user_message,
                                form_data["messages"],
                            )
                            replace_system_message_content(
                                original_system_content or "",
                                form_data["messages"],
                            )

                            # Build context: file sources with content,
                            # tool sources as citation markers only.
                            source_ids = {}
                            source_context = get_source_context(
                                metadata.get("sources", []), source_ids
                            ) + get_source_context(
                                all_tool_call_sources,
                                source_ids,
                                include_content=False,
                            )
                            source_context = source_context.strip()
                            if source_context:
                                rag_content = rag_template(
                                    request.app.state.config.RAG_TEMPLATE,
                                    source_context,
                                    user_message,
                                )
                                if RAG_SYSTEM_CONTEXT:
                                    form_data["messages"] = (
                                        add_or_update_system_message(
                                            rag_content,
                                            form_data["messages"],
                                            append=True,
                                        )
                                    )
                                else:
                                    form_data["messages"] = add_or_update_user_message(
                                        rag_content,
                                        form_data["messages"],
                                        append=False,
                                    )
                        tool_call_sources.clear()

                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": _build_chat_completion_payload(
                                output,
                                fallback_content=content,
                            )[1],
                        }
                    )

                    try:
                        new_form_data = {
                            **form_data,
                            "model": model_id,
                            "stream": True,
                            "messages": [
                                *form_data["messages"],
                                *convert_output_to_messages(output, raw=True),
                            ],
                        }

                        res = await generate_chat_completion(
                            request,
                            new_form_data,
                            user,
                            bypass_system_prompt=True,
                        )

                        if isinstance(res, StreamingResponse):
                            await stream_body_handler(res, new_form_data)
                        else:
                            break
                    except Exception as e:
                        log.debug(e)
                        break

                if DETECT_CODE_INTERPRETER:
                    MAX_RETRIES = 5
                    retries = 0

                    while (
                        output
                        and output[-1].get("type") == "open_webui:code_interpreter"
                        and retries < MAX_RETRIES
                    ):

                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": _build_chat_completion_payload(
                                    output,
                                    fallback_content=content,
                                )[1],
                            }
                        )

                        retries += 1
                        log.debug(f"Attempt count: {retries}")

                        ci_item = output[-1]
                        ci_output = ""
                        try:
                            if ci_item.get("attributes", {}).get("type") == "code":
                                code = ci_item.get("code", "")
                                # Sanitize code (strips ANSI codes and markdown fences)
                                code = sanitize_code(code)

                                if CODE_INTERPRETER_BLOCKED_MODULES:
                                    blocking_code = textwrap.dedent(f"""
                                        import builtins
    
                                        BLOCKED_MODULES = {CODE_INTERPRETER_BLOCKED_MODULES}
    
                                        _real_import = builtins.__import__
                                        def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
                                            if name.split('.')[0] in BLOCKED_MODULES:
                                                importer_name = globals.get('__name__') if globals else None
                                                if importer_name == '__main__':
                                                    raise ImportError(
                                                        f"Direct import of module {{name}} is restricted."
                                                    )
                                            return _real_import(name, globals, locals, fromlist, level)
    
                                        builtins.__import__ = restricted_import
                                    """)
                                    code = blocking_code + "\n" + code

                                if (
                                    request.app.state.config.CODE_INTERPRETER_ENGINE
                                    == "pyodide"
                                ):
                                    ci_output = await event_caller(
                                        {
                                            "type": "execute:python",
                                            "data": {
                                                "id": str(uuid4()),
                                                "code": code,
                                                "session_id": metadata.get(
                                                    "session_id", None
                                                ),
                                                "files": metadata.get("files", []),
                                            },
                                        }
                                    )
                                elif (
                                    request.app.state.config.CODE_INTERPRETER_ENGINE
                                    == "jupyter"
                                ):
                                    ci_output = await execute_code_jupyter(
                                        request.app.state.config.CODE_INTERPRETER_JUPYTER_URL,
                                        code,
                                        (
                                            request.app.state.config.CODE_INTERPRETER_JUPYTER_AUTH_TOKEN
                                            if request.app.state.config.CODE_INTERPRETER_JUPYTER_AUTH
                                            == "token"
                                            else None
                                        ),
                                        (
                                            request.app.state.config.CODE_INTERPRETER_JUPYTER_AUTH_PASSWORD
                                            if request.app.state.config.CODE_INTERPRETER_JUPYTER_AUTH
                                            == "password"
                                            else None
                                        ),
                                        request.app.state.config.CODE_INTERPRETER_JUPYTER_TIMEOUT,
                                    )
                                else:
                                    ci_output = {
                                        "stdout": "Code interpreter engine not configured."
                                    }

                                log.debug(f"Code interpreter output: {ci_output}")

                                if isinstance(ci_output, dict):
                                    stdout = ci_output.get("stdout", "")

                                    if isinstance(stdout, str):
                                        stdoutLines = stdout.split("\n")
                                        for idx, line in enumerate(stdoutLines):

                                            if "data:image/png;base64" in line:
                                                image_url = get_image_url_from_base64(
                                                    request,
                                                    line,
                                                    metadata,
                                                    user,
                                                )
                                                if image_url:
                                                    stdoutLines[idx] = (
                                                        f"![Output Image]({image_url})"
                                                    )

                                        ci_output["stdout"] = "\n".join(stdoutLines)

                                    result = ci_output.get("result", "")

                                    if isinstance(result, str):
                                        resultLines = result.split("\n")
                                        for idx, line in enumerate(resultLines):
                                            if "data:image/png;base64" in line:
                                                image_url = get_image_url_from_base64(
                                                    request,
                                                    line,
                                                    metadata,
                                                    user,
                                                )
                                                resultLines[idx] = (
                                                    f"![Output Image]({image_url})"
                                                )
                                        ci_output["result"] = "\n".join(resultLines)
                        except Exception as e:
                            ci_output = str(e)

                        ci_item["output"] = ci_output
                        ci_item["status"] = "completed"

                        output.append(
                            {
                                "type": "message",
                                "id": output_id("msg"),
                                "status": "in_progress",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": ""}],
                            }
                        )

                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": _build_chat_completion_payload(
                                    output,
                                    fallback_content=content,
                                )[1],
                            }
                        )

                        try:
                            new_form_data = {
                                **form_data,
                                "model": model_id,
                                "stream": True,
                                "messages": [
                                    *form_data["messages"],
                                    *convert_output_to_messages(output, raw=True),
                                ],
                            }

                            res = await generate_chat_completion(
                                request,
                                new_form_data,
                                user,
                                bypass_system_prompt=True,
                            )

                            if isinstance(res, StreamingResponse):
                                await stream_body_handler(res, new_form_data)
                            else:
                                break
                        except Exception as e:
                            log.debug(e)
                            break

                if stream_done:
                    for item in output:
                        if item.get("status") != "in_progress":
                            continue
                        item_type = item.get("type")
                        if item_type in {"message", "reasoning"}:
                            item["status"] = "completed"
                            if item_type == "reasoning" and item.get("ended_at") is None:
                                ended_at = time.time()
                                started_at = item.get("started_at") or ended_at
                                item["ended_at"] = ended_at
                                item["duration"] = int(ended_at - started_at)
                        elif (
                            item_type == "open_webui:code_interpreter"
                            and item.get("output") is not None
                        ):
                            item["status"] = "completed"

                output = strip_leading_message_output_before_tool_call(output)

                message_files = None
                output, final_payload, stabilized_generated_files = _stabilize_output_generated_files(
                    request,
                    output,
                    metadata,
                    user,
                )
                combined_generated_files = _merge_generated_file_entries(
                    stabilized_generated_files
                )
                output_embeds = _collect_embeds_from_output_items(output)
                combined_embeds = _merge_embed_entries(
                    accumulated_embeds,
                    output_embeds,
                )
                retrieval_generated_files, retrieval_embeds = (
                    _select_retrieval_source_visual_payloads(
                        metadata,
                        final_payload.get("content", ""),
                        request.app.state.config,
                    )
                )
                if retrieval_generated_files:
                    combined_generated_files = _merge_generated_file_entries(
                        combined_generated_files,
                        retrieval_generated_files,
                    )
                if retrieval_embeds:
                    combined_embeds = _merge_embed_entries(
                        combined_embeds,
                        retrieval_embeds,
                    )
                if combined_generated_files:
                    message_files = Chats.add_message_files_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        combined_generated_files,
                    )
                    if not isinstance(message_files, list) or not message_files:
                        message_files = combined_generated_files

                title = Chats.get_chat_title_by_id(metadata["chat_id"])
                output, final_payload = _build_chat_completion_payload(output, fallback_content=content)
                if not final_payload.get("content"):
                    final_payload["content"] = _fallback_answer_for_completed_tool_only_output(
                        output
                    )
                _merge_reference_sidecar_into_metadata(
                    metadata,
                    tool_outputs=output,
                    legacy_tool_sources=True,
                )
                reference_metadata = _build_assistant_reference_persistence_metadata(
                    metadata,
                    message_metadata=reference_seed_metadata,
                    tool_outputs=output,
                )
                completion_metadata = {}
                if isinstance(message_files, list) and message_files:
                    completion_metadata["generated_files"] = message_files
                if combined_embeds:
                    completion_metadata["embeds"] = combined_embeds
                completion_metadata.update(reference_metadata)
                completion_metadata = _enforce_selected_source_metadata_first_final_consistency(
                    completion_metadata=completion_metadata,
                    metadata=metadata,
                )
                guarded_content, output = _apply_selected_source_diagnostics_only_content_guard(
                    content=final_payload.get("content", ""),
                    output=output,
                    metadata=metadata,
                    completion_metadata=completion_metadata,
                )
                final_payload["content"] = guarded_content
                persisted_sources = _completion_sources_for_persistence(
                    completion_metadata
                )
                persisted_sources = _filter_uncited_no_evidence_source_cards(
                    persisted_sources,
                    content=final_payload.get("content", ""),
                    metadata=completion_metadata,
                )
                if persisted_sources:
                    completion_metadata["canonical_references"] = persisted_sources
                    completion_metadata["reference_cards"] = Chats.build_reference_cards(
                        persisted_sources
                    )
                else:
                    completion_metadata.pop("canonical_references", None)
                    completion_metadata.pop("reference_cards", None)
                completion_metadata = _enforce_selected_source_metadata_first_final_consistency(
                    completion_metadata=completion_metadata,
                    metadata=metadata,
                )
                guarded_content, output = _apply_selected_source_diagnostics_only_content_guard(
                    content=final_payload.get("content", ""),
                    output=output,
                    metadata=metadata,
                    completion_metadata=completion_metadata,
                )
                final_payload["content"] = guarded_content
                if _selected_source_metadata_first_diagnostics_lane(
                    {**metadata, **completion_metadata}
                ):
                    persisted_sources = []
                    completion_metadata.pop("canonical_references", None)
                    completion_metadata.pop("reference_cards", None)

                data = {
                    "done": True,
                    "content": final_payload["content"],
                    "output": output,
                    **(
                        {"files": message_files}
                        if isinstance(message_files, list) and message_files
                        else {}
                    ),
                    **(
                        {"sources": persisted_sources}
                        if persisted_sources
                        else {}
                    ),
                    **(
                        {"metadata": completion_metadata}
                        if completion_metadata
                        else {}
                    ),
                    **({"embeds": combined_embeds} if combined_embeds else {}),
                    "title": title,
                }

                if not ENABLE_REALTIME_CHAT_SAVE:
                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "role": "assistant",
                            "content": final_payload["content"],
                            "output": output,
                            **(
                                {"files": message_files}
                                if isinstance(message_files, list) and message_files
                                else {}
                            ),
                            **(
                                {"sources": persisted_sources}
                                if persisted_sources
                                else {}
                            ),
                            **({"embeds": combined_embeds} if combined_embeds else {}),
                            **({"usage": usage} if usage else {}),
                            **(
                                {"metadata": completion_metadata}
                                if completion_metadata
                                else {}
                            ),
                        },
                    )
                else:
                    final_update = {
                        **({"usage": usage} if usage else {}),
                        **(
                            {"sources": persisted_sources}
                            if persisted_sources
                            else {}
                        ),
                        **(
                            {"metadata": completion_metadata}
                            if completion_metadata
                            else {}
                        ),
                    }
                    if final_update:
                        Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata["chat_id"],
                            metadata["message_id"],
                            final_update,
                        )

                # Send a webhook notification if the user is not active
                if not Users.is_user_active(user.id):
                    webhook_url = Users.get_user_webhook_url_by_id(user.id)
                    if webhook_url:
                        await post_webhook(
                            request.app.state.WEBUI_NAME,
                            webhook_url,
                            f"{title} - {request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}\n\n{final_payload['content']}",
                            {
                                "action": "chat",
                                "message": final_payload["content"],
                                "title": title,
                                "url": f"{request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}",
                            },
                        )

                await event_emitter(
                    {
                        "type": "chat:completion",
                        "data": data,
                    }
                )

                if output and _output_contains_proxy_generated_files(output):
                    asyncio.create_task(
                        _finalize_bridge_generated_files_from_output(
                            request,
                            copy.deepcopy(output),
                            dict(metadata),
                            user,
                        )
                    )
                schedule_background_tasks(ctx)
            except asyncio.CancelledError:
                log.warning("Task was cancelled!")
                await event_emitter({"type": "chat:tasks:cancel"})

                if not ENABLE_REALTIME_CHAT_SAVE:
                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "role": "assistant",
                            **_build_chat_completion_payload(
                                output,
                                fallback_content=content,
                            )[1],
                        },
                    )

            if response.background is not None:
                await response.background()

        return await response_handler(response, events)

    else:
        # Fallback to the original response
        persist_fallback_completion = (
            not event_emitter
            and not event_caller
            and _is_persistable_chat_target(metadata)
        )

        async def stream_wrapper(original_generator, events):
            def wrap_item(item):
                return f"data: {item}\n\n"

            stream_content_type = str(response.headers.get("Content-Type", "") or "").lower()
            is_ndjson_stream = "application/x-ndjson" in stream_content_type
            payload_buffer = ""
            event_lines: list[str] = []
            fallback_output: list[dict] = []
            fallback_content = ""
            fallback_sources: list[dict] = []
            fallback_usage: dict[str, Any] = {}
            fallback_seen_payload = False
            fallback_stream_done = False

            def build_sse_data_payload(lines: list[str]) -> Optional[str]:
                data_lines: list[str] = []
                for line in lines:
                    if not line or line.startswith(":"):
                        continue
                    if ":" in line:
                        field, value = line.split(":", 1)
                        if value.startswith(" "):
                            value = value[1:]
                    else:
                        field = line
                        value = ""
                    if field == "data":
                        data_lines.append(value)
                if not data_lines:
                    return None
                return "\n".join(data_lines)

            def consume_stream_payloads(chunk_text: str) -> list[str]:
                nonlocal payload_buffer, event_lines
                payloads: list[str] = []
                normalized = chunk_text.replace("\r\n", "\n").replace("\r", "\n")
                payload_buffer += normalized

                while "\n" in payload_buffer:
                    line, payload_buffer = payload_buffer.split("\n", 1)
                    if is_ndjson_stream:
                        line = line.strip()
                        if line:
                            payloads.append(line)
                        continue

                    if line == "":
                        payload = build_sse_data_payload(event_lines)
                        if payload is not None:
                            payloads.append(payload)
                        event_lines = []
                        continue

                    event_lines.append(line)

                return payloads

            def flush_stream_payloads() -> list[str]:
                nonlocal payload_buffer, event_lines
                if is_ndjson_stream:
                    trailing = payload_buffer.strip()
                    payload_buffer = ""
                    return [trailing] if trailing else []

                if payload_buffer:
                    event_lines.append(payload_buffer)
                    payload_buffer = ""

                if not event_lines:
                    return []

                payload = build_sse_data_payload(event_lines)
                event_lines = []
                return [payload] if payload is not None else []

            def capture_fallback_stream_payload(payload_text: str) -> None:
                nonlocal fallback_output, fallback_content, fallback_sources
                nonlocal fallback_usage, fallback_seen_payload, fallback_stream_done

                stripped = payload_text.strip()
                if not stripped:
                    return
                if stripped == "[DONE]":
                    fallback_stream_done = True
                    return

                try:
                    data = json.loads(payload_text)
                except Exception:
                    return
                if not isinstance(data, dict):
                    return

                fallback_seen_payload = True

                if data.get("type", "").startswith("response."):
                    fallback_output, response_metadata = handle_responses_streaming_event(
                        data, fallback_output
                    )
                    if isinstance(response_metadata, dict):
                        usage = response_metadata.get("usage")
                        if isinstance(usage, dict) and usage:
                            fallback_usage = normalize_usage(usage)
                    return

                raw_usage = data.get("usage", {}) or {}
                if isinstance(data.get("timings"), dict):
                    raw_usage.update(data.get("timings", {}))
                if raw_usage:
                    fallback_usage = normalize_usage(raw_usage)

                sources = data.get("sources")
                if isinstance(sources, list):
                    fallback_sources = Chats._merge_message_sources(fallback_sources, sources)
                elif isinstance(sources, dict):
                    fallback_sources = Chats._merge_message_sources(fallback_sources, [sources])

                output_payload = data.get("output")
                if isinstance(output_payload, list):
                    fallback_output = output_payload

                choices = data.get("choices", [])
                if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
                    return

                delta = choices[0].get("delta")
                if isinstance(delta, dict):
                    delta_content = delta.get("content")
                    if isinstance(delta_content, str) and delta_content:
                        fallback_content = (
                            f"{fallback_content}{_strip_bridge_details_blocks(delta_content)}"
                        )

                message = choices[0].get("message")
                if not isinstance(message, dict):
                    return

                if isinstance(message.get("output"), list):
                    fallback_output = message.get("output")
                message_content = message.get("content")
                if isinstance(message_content, str) and message_content.strip():
                    fallback_content = message_content

            for event in events:
                event, _ = await process_filter_functions(
                    request=request,
                    filter_functions=filter_functions,
                    filter_type="stream",
                    form_data=event,
                    extra_params=extra_params,
                )

                if event:
                    yield wrap_item(json.dumps(event))

            async for data in original_generator:
                data, _ = await process_filter_functions(
                    request=request,
                    filter_functions=filter_functions,
                    filter_type="stream",
                    form_data=data,
                    extra_params=extra_params,
                )

                if data:
                    if persist_fallback_completion:
                        chunk_text = (
                            data.decode("utf-8", "replace")
                            if isinstance(data, bytes)
                            else str(data)
                        )
                        for payload in consume_stream_payloads(chunk_text):
                            capture_fallback_stream_payload(payload)
                    yield data

            if persist_fallback_completion:
                for payload in flush_stream_payloads():
                    capture_fallback_stream_payload(payload)

                if fallback_seen_payload or fallback_stream_done:
                    fallback_output, fallback_payload = _build_chat_completion_payload(
                        fallback_output,
                        fallback_content=fallback_content,
                    )
                    fallback_content_value = (
                        fallback_payload.get("content")
                        or _fallback_answer_for_completed_tool_only_output(fallback_output)
                        or ""
                    )

                    _merge_reference_sidecar_into_metadata(
                        metadata,
                        sources=fallback_sources,
                        tool_outputs=fallback_output,
                        legacy_tool_sources=True,
                    )
                    completion_metadata = _build_assistant_reference_persistence_metadata(
                        metadata,
                        tool_outputs=fallback_output,
                    )
                    completion_metadata = (
                        _enforce_selected_source_metadata_first_final_consistency(
                            completion_metadata=completion_metadata,
                            metadata=metadata,
                        )
                    )
                    fallback_content_value, fallback_output = (
                        _apply_selected_source_diagnostics_only_content_guard(
                            content=fallback_content_value,
                            output=fallback_output,
                            metadata=metadata,
                            completion_metadata=completion_metadata,
                        )
                    )
                    persisted_sources = _completion_sources_for_persistence(
                        completion_metadata
                    )
                    suppress_completion_references = (
                        Chats._should_suppress_canonical_references(
                            metadata=completion_metadata,
                            diagnostics=completion_metadata.get("retrieval_diagnostics"),
                        )
                    )
                    if suppress_completion_references:
                        persisted_sources = []
                    else:
                        persisted_sources = _merge_persisted_and_response_sources(
                            persisted_sources,
                            fallback_sources,
                            completion_metadata=completion_metadata,
                        )

                    persisted_sources = _filter_uncited_no_evidence_source_cards(
                        persisted_sources,
                        content=fallback_content_value,
                        metadata=completion_metadata,
                    )
                    if persisted_sources:
                        completion_metadata["canonical_references"] = persisted_sources
                        completion_metadata["reference_cards"] = Chats.build_reference_cards(
                            persisted_sources
                        )
                    else:
                        completion_metadata.pop("canonical_references", None)
                        completion_metadata.pop("reference_cards", None)
                    completion_metadata = (
                        _enforce_selected_source_metadata_first_final_consistency(
                            completion_metadata=completion_metadata,
                            metadata=metadata,
                        )
                    )
                    fallback_content_value, fallback_output = (
                        _apply_selected_source_diagnostics_only_content_guard(
                            content=fallback_content_value,
                            output=fallback_output,
                            metadata=metadata,
                            completion_metadata=completion_metadata,
                        )
                    )
                    if _selected_source_metadata_first_diagnostics_lane(
                        {**metadata, **completion_metadata}
                    ):
                        persisted_sources = []
                        completion_metadata.pop("canonical_references", None)
                        completion_metadata.pop("reference_cards", None)

                    _persist_assistant_completion_message(
                        metadata=metadata,
                        content=fallback_content_value,
                        output=fallback_output,
                        sources=persisted_sources,
                        usage=fallback_usage,
                        completion_metadata=completion_metadata,
                    )

        return StreamingResponse(
            stream_wrapper(response.body_iterator, events),
            headers=dict(response.headers),
            background=response.background,
        )


async def process_chat_response(response, ctx):
    # Non-streaming response
    if not isinstance(response, StreamingResponse):
        return await non_streaming_chat_response_handler(response, ctx)

    # Non standard response
    if not any(
        content_type in response.headers["Content-Type"]
        for content_type in ["text/event-stream", "application/x-ndjson"]
    ):
        return response

    # Streaming response
    return await streaming_chat_response_handler(response, ctx)
