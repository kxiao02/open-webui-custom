import inspect
import logging
import re
import inspect
import aiohttp
import asyncio
import yaml
import json
import hashlib

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from typing import (
    Any,
    Awaitable,
    Callable,
    get_type_hints,
    get_args,
    get_origin,
    Dict,
    List,
    Tuple,
    Union,
    Optional,
    Type,
)
from functools import update_wrapper, partial


from fastapi import Request
from pydantic import BaseModel, Field, create_model

from langchain_core.utils.function_calling import (
    convert_to_openai_function as convert_pydantic_model_to_openai_function_spec,
)


from open_webui.utils.misc import is_string_allowed
from open_webui.models.tools import Tools
from open_webui.models.users import UserModel
from open_webui.models.groups import Groups
from open_webui.models.access_grants import AccessGrants
from open_webui.utils.catalog import get_user_group_ids, is_tool_catalog_visible
from open_webui.utils.plugin import load_tool_module_by_id, replace_imports
from open_webui.utils.access_control import has_access, has_connection_access
from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL, ENABLE_KNOWLEDGE
from open_webui.env import (
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA,
    AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL,
    ENABLE_FORWARD_USER_INFO_HEADERS,
    FORWARD_SESSION_INFO_HEADER_CHAT_ID,
    FORWARD_SESSION_INFO_HEADER_MESSAGE_ID,
)
from open_webui.utils.headers import include_user_info_headers
from open_webui.tools.builtin import (
    search_web,
    fetch_url,
    generate_image,
    edit_image,
    execute_code,
    search_memories,
    add_memory,
    replace_memory_content,
    delete_memory,
    list_memories,
    get_current_timestamp,
    calculate_timestamp,
    search_notes,
    search_chats,
    search_channels,
    search_channel_messages,
    view_note,
    view_chat,
    view_channel_message,
    view_channel_thread,
    replace_note_content,
    write_note,
    list_skills,
    list_knowledge_bases,
    search_knowledge_bases,
    query_knowledge_bases,
    search_knowledge_files,
    query_knowledge_files,
    view_file,
    view_knowledge_file,
    view_skill,
)

import copy

log = logging.getLogger(__name__)


DEEPAGENT_RUNTIME_TOOL_SCHEMA_VERSION = 1
DEEPAGENT_RUNTIME_TOOL_PREFIX = "owu__"
DEEPAGENT_BUILTIN_RETRIEVAL_TOOL_ID = "builtin:retrieval"
DEEPAGENT_BUILTIN_SKILLS_TOOL_ID = "builtin:skills"
DEEPAGENT_RUNTIME_SKILL_IDS_METADATA_KEY = "deepagent_runtime_skill_ids"
DEEPAGENT_RETRIEVAL_MAX_SOURCES = 6
DEEPAGENT_RETRIEVAL_MAX_CHUNKS = 10
DEEPAGENT_RETRIEVAL_MAX_CHARS_PER_CHUNK = 900
DEEPAGENT_RETRIEVAL_MAX_TOTAL_CHARS = 6000
DEEPAGENT_READ_MAX_CHARS_PER_CHUNK = 1200
DEEPAGENT_READ_MAX_TOTAL_CHARS = 8000


def _selected_retrieval_normalized_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _selected_retrieval_ascii_terms(query: str) -> list[str]:
    terms: list[str] = []
    for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", query or ""):
        normalized = term.strip().lower()
        if normalized and normalized not in terms:
            terms.append(normalized)
    return terms


def _selected_retrieval_cjk_phrases(query: str) -> list[str]:
    phrases: list[str] = []
    for phrase in re.findall(r"[\u4e00-\u9fff]{4,}", query or ""):
        normalized = phrase.strip()
        if normalized and normalized not in phrases:
            phrases.append(normalized)
    return phrases


def _selected_retrieval_phrase_matches(phrase: str, text: str) -> bool:
    if not phrase:
        return False
    if phrase in text:
        return True
    bigrams = {phrase[index : index + 2] for index in range(len(phrase) - 1)}
    if not bigrams:
        return False
    matched = sum(1 for bigram in bigrams if bigram in text)
    if len(phrase) >= 5:
        edge_bigrams = {phrase[:2], phrase[-2:]}
        if not any(bigram in text for bigram in edge_bigrams):
            return False
    return matched >= min(2, len(bigrams))


def _selected_retrieval_chunk_matches_query(query: str, document: Any) -> bool:
    """Reject obvious low-evidence semantic drift for selected-source tool calls."""

    normalized_query = str(query or "").strip()
    if not normalized_query:
        return True

    text = _selected_retrieval_normalized_text(document)
    if not text:
        return False

    ascii_terms = _selected_retrieval_ascii_terms(normalized_query)
    cjk_phrases = _selected_retrieval_cjk_phrases(normalized_query)
    if not ascii_terms and not cjk_phrases:
        return True

    if any(term in text for term in ascii_terms):
        return True

    normalized_cjk_text = re.sub(r"[^\u4e00-\u9fff]", "", text)
    if any(
        _selected_retrieval_phrase_matches(phrase, normalized_cjk_text)
        for phrase in cjk_phrases
    ):
        return True

    return False


def _filter_selected_retrieval_sources_by_query(
    sources: list[dict], query: str
) -> tuple[list[dict], list[dict[str, Any]]]:
    filtered_sources: list[dict] = []
    diagnostics: list[dict[str, Any]] = []

    for source_index, source in enumerate(sources or []):
        if not isinstance(source, dict):
            diagnostics.append(
                {
                    "kind": "retrieval_quality",
                    "classification": "diagnostics",
                    "reason": "malformed_candidate",
                    "outcome": "malformed",
                    "candidate_index": source_index,
                }
            )
            continue

        documents = source.get("document") if isinstance(source.get("document"), list) else []
        accepted_indexes = [
            index
            for index, document in enumerate(documents)
            if _selected_retrieval_chunk_matches_query(query, document)
        ]
        if not accepted_indexes:
            diagnostics.append(
                {
                    "kind": "retrieval_quality",
                    "classification": "no_evidence",
                    "reason": "weak_or_indirect_evidence",
                    "outcome": "weak_evidence",
                    "candidate_index": source_index,
                    "chunk_total": len(documents),
                }
            )
            continue

        filtered = copy.deepcopy(source)
        for key, value in source.items():
            if isinstance(value, list) and len(value) == len(documents):
                filtered[key] = [value[index] for index in accepted_indexes]
        filtered_sources.append(filtered)

    return filtered_sources, diagnostics


BUILTIN_TOOL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "time",
        "name": "Time & Calculation",
        "description": "Get current time and perform date/time calculations",
        "capability_requirements": [],
        "feature_requirements": [],
        "config_requirements": [],
    },
    {
        "id": "memory",
        "name": "Memory",
        "description": "Search and manage user memories",
        "capability_requirements": [],
        "feature_requirements": ["memory"],
        "config_requirements": [],
    },
    {
        "id": "chats",
        "name": "Chat History",
        "description": "Search and view user chat history",
        "capability_requirements": [],
        "feature_requirements": [],
        "config_requirements": [],
    },
    {
        "id": "notes",
        "name": "Notes",
        "description": "Search, view, and manage user notes",
        "capability_requirements": [],
        "feature_requirements": [],
        "config_requirements": ["ENABLE_NOTES"],
    },
    {
        "id": "knowledge",
        "name": "Knowledge Base",
        "description": "Browse and query knowledge bases",
        "capability_requirements": [],
        "feature_requirements": [],
        "config_requirements": ["ENABLE_KNOWLEDGE"],
    },
    {
        "id": "channels",
        "name": "Channels",
        "description": "Search channels and channel messages",
        "capability_requirements": [],
        "feature_requirements": [],
        "config_requirements": ["ENABLE_CHANNELS"],
    },
    {
        "id": "web_search",
        "name": "Web Search",
        "description": "Search the web and fetch URLs",
        "capability_requirements": ["web_search"],
        "feature_requirements": ["web_search"],
        "config_requirements": ["ENABLE_WEB_SEARCH"],
    },
    {
        "id": "image_generation",
        "name": "Image Generation",
        "description": "Generate and edit images",
        "capability_requirements": ["image_generation"],
        "feature_requirements": ["image_generation"],
        "config_requirements": ["ENABLE_IMAGE_GENERATION_OR_EDIT"],
    },
    {
        "id": "code_interpreter",
        "name": "Code Interpreter",
        "description": "Execute code",
        "capability_requirements": ["code_interpreter"],
        "feature_requirements": ["code_interpreter"],
        "config_requirements": ["ENABLE_CODE_INTERPRETER"],
    },
)


def _has_nonempty_config_value(config: Any, attr_name: str) -> bool:
    value = getattr(config, attr_name, None)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return bool(value)


def is_image_generation_configured(config: Any) -> bool:
    if not bool(getattr(config, "ENABLE_IMAGE_GENERATION", False)):
        return False

    engine = str(getattr(config, "IMAGE_GENERATION_ENGINE", "") or "").strip().lower()

    if engine == "openai":
        return _has_nonempty_config_value(
            config, "IMAGES_OPENAI_API_BASE_URL"
        ) and _has_nonempty_config_value(config, "IMAGES_OPENAI_API_KEY")

    if engine == "gemini":
        return _has_nonempty_config_value(
            config, "IMAGES_GEMINI_API_BASE_URL"
        ) and _has_nonempty_config_value(config, "IMAGES_GEMINI_API_KEY")

    if engine == "comfyui":
        return _has_nonempty_config_value(config, "COMFYUI_BASE_URL") and (
            _has_nonempty_config_value(config, "COMFYUI_WORKFLOW")
            or _has_nonempty_config_value(config, "COMFYUI_WORKFLOW_NODES")
        )

    return _has_nonempty_config_value(config, "AUTOMATIC1111_BASE_URL")


def is_image_edit_configured(config: Any) -> bool:
    if not bool(getattr(config, "ENABLE_IMAGE_EDIT", False)):
        return False

    engine = str(getattr(config, "IMAGE_EDIT_ENGINE", "") or "").strip().lower()

    if engine == "openai":
        return _has_nonempty_config_value(
            config, "IMAGES_EDIT_OPENAI_API_BASE_URL"
        ) and _has_nonempty_config_value(config, "IMAGES_EDIT_OPENAI_API_KEY")

    if engine == "gemini":
        return _has_nonempty_config_value(
            config, "IMAGES_EDIT_GEMINI_API_BASE_URL"
        ) and _has_nonempty_config_value(config, "IMAGES_EDIT_GEMINI_API_KEY")

    if engine == "comfyui":
        return _has_nonempty_config_value(config, "IMAGES_EDIT_COMFYUI_BASE_URL") and (
            _has_nonempty_config_value(config, "IMAGES_EDIT_COMFYUI_WORKFLOW")
            or _has_nonempty_config_value(config, "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES")
        )

    return False


def is_image_generation_tool_available(config: Any) -> bool:
    return is_image_generation_configured(config) or is_image_edit_configured(config)


def get_builtin_tool_catalog(request: Request) -> list[dict[str, Any]]:
    config = request.app.state.config
    catalog: list[dict[str, Any]] = []

    for item in BUILTIN_TOOL_CATALOG:
        tool_id = item["id"]
        available = True

        if tool_id == "knowledge":
            available = ENABLE_KNOWLEDGE.value
        elif tool_id == "notes":
            available = bool(getattr(config, "ENABLE_NOTES", False))
        elif tool_id == "channels":
            available = bool(getattr(config, "ENABLE_CHANNELS", False))
        elif tool_id == "web_search":
            available = bool(getattr(config, "ENABLE_WEB_SEARCH", False))
        elif tool_id == "image_generation":
            available = is_image_generation_tool_available(config)
        elif tool_id == "code_interpreter":
            available = bool(getattr(config, "ENABLE_CODE_INTERPRETER", True))

        catalog.append(
            {
                "id": tool_id,
                "name": item["name"],
                "meta": {
                    "description": item["description"],
                    "category": "builtin",
                    "origin": "host",
                    "catalog_kind": "host",
                    "source_of_truth": "open-webui",
                    "execution_boundary": "open-webui",
                    "mutability": "locked",
                    "default_enabled": True,
                    "available": available,
                    "availability_state": "ready" if available else "disabled",
                    "visibility": "public",
                    "capability_requirements": item["capability_requirements"],
                    "feature_requirements": item["feature_requirements"],
                    "config_requirements": item["config_requirements"],
                },
            }
        )

    return catalog


def get_async_tool_function_and_apply_extra_params(
    function: Callable, extra_params: dict
) -> Callable[..., Awaitable]:
    sig = inspect.signature(function)
    extra_params = {k: v for k, v in extra_params.items() if k in sig.parameters}
    partial_func = partial(function, **extra_params)

    # Remove the 'frozen' keyword arguments from the signature
    # python-genai uses the signature to infer the tool properties for native function calling
    parameters = []
    for name, parameter in sig.parameters.items():
        # Exclude keyword arguments that are frozen
        if name in extra_params:
            continue
        # Keep remaining parameters
        parameters.append(parameter)

    new_sig = inspect.Signature(
        parameters=parameters, return_annotation=sig.return_annotation
    )

    if inspect.iscoroutinefunction(function):
        # wrap the functools.partial as python-genai has trouble with it
        # https://github.com/googleapis/python-genai/issues/907
        async def new_function(*args, **kwargs):
            return await partial_func(*args, **kwargs)

    else:
        # Make it a coroutine function when it is not already
        async def new_function(*args, **kwargs):
            return partial_func(*args, **kwargs)

    update_wrapper(new_function, function)
    new_function.__signature__ = new_sig

    new_function.__function__ = function  # type: ignore
    new_function.__extra_params__ = extra_params  # type: ignore

    return new_function


def get_updated_tool_function(function: Callable, extra_params: dict):
    # Get the original function and merge updated params
    __function__ = getattr(function, "__function__", None)
    __extra_params__ = getattr(function, "__extra_params__", None)

    if __function__ is not None and __extra_params__ is not None:
        return get_async_tool_function_and_apply_extra_params(
            __function__,
            {**__extra_params__, **extra_params},
        )

    return function


async def get_tools(
    request: Request, tool_ids: list[str], user: UserModel, extra_params: dict
) -> dict[str, dict]:
    """Load tools for the given tool_ids, checking access control."""
    if not tool_ids:
        return {}

    tools_dict = {}

    # Get user's group memberships for access control checks
    user_group_ids = get_user_group_ids(user.id)

    for tool_id in tool_ids:
        tool = Tools.get_tool_by_id(tool_id)
        if tool:
            if not is_tool_catalog_visible(tool, user, user_group_ids):
                log.warning(f"Access denied to tool {tool_id} for user {user.id}")
                continue

            module = request.app.state.TOOLS.get(tool_id, None)
            if module is None:
                module, _ = load_tool_module_by_id(tool_id)
                request.app.state.TOOLS[tool_id] = module

            __user__ = {
                **extra_params["__user__"],
            }

            # Set valves for the tool
            if hasattr(module, "valves") and hasattr(module, "Valves"):
                valves = Tools.get_tool_valves_by_id(tool_id) or {}
                module.valves = module.Valves(**valves)
            if hasattr(module, "UserValves"):
                __user__["valves"] = module.UserValves(  # type: ignore
                    **Tools.get_user_valves_by_id_and_user_id(tool_id, user.id)
                )

            for spec in tool.specs:
                # TODO: Fix hack for OpenAI API
                # Some times breaks OpenAI but others don't. Leaving the comment
                for val in spec.get("parameters", {}).get("properties", {}).values():
                    if val.get("type") == "str":
                        val["type"] = "string"

                # Remove internal reserved parameters (e.g. __id__, __user__)
                spec["parameters"]["properties"] = {
                    key: val
                    for key, val in spec["parameters"]["properties"].items()
                    if not key.startswith("__")
                }

                # convert to function that takes only model params and inserts custom params
                function_name = spec["name"]
                tool_function = getattr(module, function_name)
                callable = get_async_tool_function_and_apply_extra_params(
                    tool_function,
                    {
                        **extra_params,
                        "__id__": tool_id,
                        "__user__": __user__,
                    },
                )

                # TODO: Support Pydantic models as parameters
                if callable.__doc__ and callable.__doc__.strip() != "":
                    s = re.split(":(param|return)", callable.__doc__, 1)
                    spec["description"] = s[0]
                else:
                    spec["description"] = function_name

                tool_dict = {
                    "tool_id": tool_id,
                    "callable": callable,
                    "spec": spec,
                    # Misc info
                    "metadata": {
                        "file_handler": hasattr(module, "file_handler")
                        and module.file_handler,
                        "citation": hasattr(module, "citation") and module.citation,
                    },
                }

                # Handle function name collisions
                while function_name in tools_dict:
                    log.warning(
                        f"Tool {function_name} already exists in another tools!"
                    )
                    # Prepend tool ID to function name
                    function_name = f"{tool_id}_{function_name}"

                tools_dict[function_name] = tool_dict
        else:
            if tool_id.startswith("server:"):
                splits = tool_id.split(":")

                if len(splits) == 2:
                    type = "openapi"
                    server_id = splits[1]
                elif len(splits) == 3:
                    type = splits[1]
                    server_id = splits[2]

                server_id_splits = server_id.split("|")
                if len(server_id_splits) == 2:
                    server_id = server_id_splits[0]
                    function_names = server_id_splits[1].split(",")

                if type == "openapi":

                    tool_server_data = None
                    for server in await get_tool_servers(request):
                        if server["id"] == server_id:
                            tool_server_data = server
                            break

                    if tool_server_data is None:
                        log.warning(f"Tool server data not found for {server_id}")
                        continue

                    tool_server_idx = tool_server_data.get("idx", 0)
                    tool_server_connection = (
                        request.app.state.config.TOOL_SERVER_CONNECTIONS[
                            tool_server_idx
                        ]
                    )

                    # Check access control for tool server
                    if not has_connection_access(
                        user, tool_server_connection, user_group_ids
                    ):
                        log.warning(
                            f"Access denied to tool server {server_id} for user {user.id}"
                        )
                        continue

                    specs = tool_server_data.get("specs", [])
                    function_name_filter_list = tool_server_connection.get(
                        "config", {}
                    ).get("function_name_filter_list", "")

                    if isinstance(function_name_filter_list, str):
                        function_name_filter_list = function_name_filter_list.split(",")

                    for spec in specs:
                        function_name = spec["name"]
                        if function_name_filter_list:
                            if not is_string_allowed(
                                function_name, function_name_filter_list
                            ):
                                # Skip this function
                                continue

                        auth_type = tool_server_connection.get("auth_type", "bearer")

                        cookies = {}
                        headers = {
                            "Content-Type": "application/json",
                        }

                        if auth_type == "bearer":
                            headers["Authorization"] = (
                                f"Bearer {tool_server_connection.get('key', '')}"
                            )
                        elif auth_type == "none":
                            # No authentication
                            pass
                        elif auth_type == "session":
                            cookies = request.cookies
                            headers["Authorization"] = (
                                f"Bearer {request.state.token.credentials}"
                            )
                        elif auth_type == "system_oauth":
                            cookies = request.cookies
                            oauth_token = extra_params.get("__oauth_token__", None)
                            if oauth_token:
                                headers["Authorization"] = (
                                    f"Bearer {oauth_token.get('access_token', '')}"
                                )

                        connection_headers = tool_server_connection.get("headers", None)
                        if connection_headers and isinstance(connection_headers, dict):
                            for key, value in connection_headers.items():
                                headers[key] = value

                        # Add user info headers if enabled
                        if ENABLE_FORWARD_USER_INFO_HEADERS and user:
                            headers = include_user_info_headers(headers, user)
                            metadata = extra_params.get("__metadata__", {})
                            if metadata and metadata.get("chat_id"):
                                headers[FORWARD_SESSION_INFO_HEADER_CHAT_ID] = (
                                    metadata.get("chat_id")
                                )
                            if metadata and metadata.get("message_id"):
                                headers[FORWARD_SESSION_INFO_HEADER_MESSAGE_ID] = (
                                    metadata.get("message_id")
                                )

                        def make_tool_function(
                            function_name, tool_server_data, headers
                        ):
                            async def tool_function(**kwargs):
                                return await execute_tool_server(
                                    url=tool_server_data["url"],
                                    headers=headers,
                                    cookies=cookies,
                                    name=function_name,
                                    params=kwargs,
                                    server_data=tool_server_data,
                                )

                            return tool_function

                        tool_function = make_tool_function(
                            function_name, tool_server_data, headers
                        )

                        callable = get_async_tool_function_and_apply_extra_params(
                            tool_function,
                            {},
                        )

                        tool_dict = {
                            "tool_id": tool_id,
                            "callable": callable,
                            "spec": clean_openai_tool_schema(spec),
                            # Misc info
                            "type": "external",
                        }

                        # Handle function name collisions
                        while function_name in tools_dict:
                            log.warning(
                                f"Tool {function_name} already exists in another tools!"
                            )
                            # Prepend server ID to function name
                            function_name = f"{server_id}_{function_name}"

                        tools_dict[function_name] = tool_dict

                else:
                    continue

    return tools_dict


def get_builtin_tools(
    request: Request, extra_params: dict, features: dict = None, model: dict = None
) -> dict[str, dict]:
    """
    Get built-in tools for native function calling.
    Only returns tools when BOTH the global config is enabled AND the model capability allows it.
    """
    tools_dict = {}
    builtin_functions = []
    features = features or {}
    model = model or {}

    # Helper to get model capabilities (defaults to True if not specified)
    def get_model_capability(name: str, default: bool = True) -> bool:
        return (model.get("info", {}).get("meta", {}).get("capabilities") or {}).get(
            name, default
        )

    # Helper to check if a builtin tool category is enabled via meta.builtinTools
    # Defaults to True if not specified (backward compatible)
    def is_builtin_tool_enabled(category: str) -> bool:
        builtin_tools = model.get("info", {}).get("meta", {}).get("builtinTools", {})
        return builtin_tools.get(category, True)

    # Time utilities - available for date calculations
    if is_builtin_tool_enabled("time"):
        builtin_functions.extend([get_current_timestamp, calculate_timestamp])

    # Knowledge base tools - conditional injection based on model knowledge
    # If model has attached knowledge (any type), only provide query_knowledge_files
    # Otherwise, provide all KB browsing tools
    model_knowledge = model.get("info", {}).get("meta", {}).get("knowledge", [])
    # Merge folder-attached knowledge so builtin tools can search it
    folder_knowledge = extra_params.get("__metadata__", {}).get("folder_knowledge")
    if folder_knowledge:
        model_knowledge = list(model_knowledge or []) + list(folder_knowledge)
    if ENABLE_KNOWLEDGE.value and is_builtin_tool_enabled("knowledge"):
        if model_knowledge:
            # Model has attached knowledge - only allow semantic search within it
            builtin_functions.append(query_knowledge_files)

            knowledge_types = {item.get("type") for item in model_knowledge}
            if "file" in knowledge_types or "collection" in knowledge_types:
                builtin_functions.append(view_file)
            if "note" in knowledge_types:
                builtin_functions.append(view_note)
        else:
            # No model knowledge - allow full KB browsing
            builtin_functions.extend(
                [
                    list_knowledge_bases,
                    search_knowledge_bases,
                    query_knowledge_bases,
                    search_knowledge_files,
                    query_knowledge_files,
                    view_knowledge_file,
                ]
            )

    # Chats tools - search and fetch user's chat history
    if is_builtin_tool_enabled("chats"):
        builtin_functions.extend([search_chats, view_chat])

    # Add memory tools if builtin category enabled AND enabled for this chat
    if is_builtin_tool_enabled("memory") and features.get("memory"):
        builtin_functions.extend(
            [
                search_memories,
                add_memory,
                replace_memory_content,
                delete_memory,
                list_memories,
            ]
        )

    # Add web search tools if builtin category enabled AND enabled globally AND model has web_search capability
    if (
        is_builtin_tool_enabled("web_search")
        and getattr(request.app.state.config, "ENABLE_WEB_SEARCH", False)
        and get_model_capability("web_search")
        and features.get("web_search")
    ):
        builtin_functions.extend([search_web, fetch_url])

    # Add image generation/edit tools if builtin category enabled AND enabled globally AND model has image_generation capability
    if (
        is_builtin_tool_enabled("image_generation")
        and is_image_generation_configured(request.app.state.config)
        and get_model_capability("image_generation")
        and features.get("image_generation")
    ):
        builtin_functions.append(generate_image)
    if (
        is_builtin_tool_enabled("image_generation")
        and is_image_edit_configured(request.app.state.config)
        and get_model_capability("image_generation")
        and features.get("image_generation")
    ):
        builtin_functions.append(edit_image)

    # Add code interpreter tool if builtin category enabled AND enabled globally AND model has code_interpreter capability
    if (
        is_builtin_tool_enabled("code_interpreter")
        and getattr(request.app.state.config, "ENABLE_CODE_INTERPRETER", True)
        and get_model_capability("code_interpreter")
        and features.get("code_interpreter")
    ):
        builtin_functions.append(execute_code)

    # Notes tools - search, view, create, and update user's notes (if builtin category enabled AND notes enabled globally)
    if is_builtin_tool_enabled("notes") and getattr(
        request.app.state.config, "ENABLE_NOTES", False
    ):
        builtin_functions.extend(
            [search_notes, view_note, write_note, replace_note_content]
        )

    # Channels tools - search channels and messages (if builtin category enabled AND channels enabled globally)
    if is_builtin_tool_enabled("channels") and getattr(
        request.app.state.config, "ENABLE_CHANNELS", False
    ):
        builtin_functions.extend(
            [
                search_channels,
                search_channel_messages,
                view_channel_thread,
                view_channel_message,
            ]
        )

    # Skills tools - allow the model to discover and load full skill instructions on demand
    if extra_params.get("__skill_ids__"):
        builtin_functions.extend([list_skills, view_skill])

    for func in builtin_functions:
        callable = get_async_tool_function_and_apply_extra_params(
            func,
            {
                "__request__": request,
                "__user__": extra_params.get("__user__", {}),
                "__event_emitter__": extra_params.get("__event_emitter__"),
                "__event_call__": extra_params.get("__event_call__"),
                "__metadata__": extra_params.get("__metadata__"),
                "__chat_id__": extra_params.get("__chat_id__"),
                "__message_id__": extra_params.get("__message_id__"),
                "__model_knowledge__": model_knowledge,
                "__skill_ids__": extra_params.get("__skill_ids__"),
            },
        )

        # Generate spec from function
        pydantic_model = convert_function_to_pydantic_model(func)
        spec = convert_pydantic_model_to_openai_function_spec(pydantic_model)
        spec = clean_openai_tool_schema(spec)

        tools_dict[func.__name__] = {
            "tool_id": f"builtin:{func.__name__}",
            "callable": callable,
            "spec": spec,
            "type": "builtin",
        }

    return tools_dict


def parse_description(docstring: str | None) -> str:
    """
    Parse a function's docstring to extract the description.

    Args:
        docstring (str): The docstring to parse.

    Returns:
        str: The description.
    """

    if not docstring:
        return ""

    lines = [line.strip() for line in docstring.strip().split("\n")]
    description_lines: list[str] = []

    for line in lines:
        if re.match(r":param", line) or re.match(r":return", line):
            break

        description_lines.append(line)

    return "\n".join(description_lines)


def parse_docstring(docstring):
    """
    Parse a function's docstring to extract parameter descriptions in reST format.

    Args:
        docstring (str): The docstring to parse.

    Returns:
        dict: A dictionary where keys are parameter names and values are descriptions.
    """
    if not docstring:
        return {}

    # Regex to match `:param name: description` format
    param_pattern = re.compile(r":param (\w+):\s*(.+)")
    param_descriptions = {}

    for line in docstring.splitlines():
        match = param_pattern.match(line.strip())
        if not match:
            continue
        param_name, param_description = match.groups()
        if param_name.startswith("__"):
            continue
        param_descriptions[param_name] = param_description

    return param_descriptions


def convert_function_to_pydantic_model(func: Callable) -> type[BaseModel]:
    """
    Converts a Python function's type hints and docstring to a Pydantic model,
    including support for nested types, default values, and descriptions.

    Args:
        func: The function whose type hints and docstring should be converted.
        model_name: The name of the generated Pydantic model.

    Returns:
        A Pydantic model class.
    """
    type_hints = get_type_hints(func)
    signature = inspect.signature(func)
    parameters = signature.parameters

    docstring = func.__doc__

    function_description = parse_description(docstring)
    function_param_descriptions = parse_docstring(docstring)

    field_defs = {}
    for name, param in parameters.items():
        type_hint = type_hints.get(name, Any)
        default_value = param.default if param.default is not param.empty else ...

        param_description = function_param_descriptions.get(name, None)

        if param_description:
            field_defs[name] = (
                type_hint,
                Field(default_value, description=param_description),
            )
        else:
            field_defs[name] = type_hint, default_value

    model = create_model(func.__name__, **field_defs)
    model.__doc__ = function_description

    return model


def clean_properties(schema: dict):
    if not isinstance(schema, dict):
        return

    if "anyOf" in schema:
        non_null_types = [t for t in schema["anyOf"] if t.get("type") != "null"]
        if len(non_null_types) == 1:
            schema.update(non_null_types[0])
            del schema["anyOf"]
        else:
            schema["anyOf"] = non_null_types

    if "default" in schema and schema["default"] is None:
        del schema["default"]

    # fix missing type
    if "type" not in schema and "anyOf" not in schema and "properties" not in schema:
        schema["type"] = "string"

    if "properties" in schema:
        for prop_name, prop_schema in schema["properties"].items():
            clean_properties(prop_schema)

    if "items" in schema:
        clean_properties(schema["items"])


def clean_openai_tool_schema(spec: dict) -> dict:
    import copy

    cleaned_spec = copy.deepcopy(spec)

    if "parameters" in cleaned_spec:
        clean_properties(cleaned_spec["parameters"])

    return cleaned_spec


def get_functions_from_tool(tool: object) -> list[Callable]:
    return [
        getattr(tool, func)
        for func in dir(tool)
        if callable(
            getattr(tool, func)
        )  # checks if the attribute is callable (a method or function).
        and not func.startswith(
            "_"
        )  # filters out internal methods (starting with _) and special (dunder) methods.
        and not inspect.isclass(
            getattr(tool, func)
        )  # ensures that the callable is not a class itself, just a method or function.
    ]


def get_tool_specs(tool_module: object) -> list[dict]:
    function_models = map(
        convert_function_to_pydantic_model, get_functions_from_tool(tool_module)
    )

    specs = [
        clean_openai_tool_schema(
            convert_pydantic_model_to_openai_function_spec(function_model)
        )
        for function_model in function_models
    ]

    return specs


def _compute_deepagent_tool_revision(tool: Any) -> str:
    payload = {
        "id": getattr(tool, "id", ""),
        "content": replace_imports(str(getattr(tool, "content", "") or "")),
        "specs": getattr(tool, "specs", []),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_deepagent_runtime_skill_ids(metadata: dict | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []

    normalized: list[str] = []
    for skill_id in metadata.get(DEEPAGENT_RUNTIME_SKILL_IDS_METADATA_KEY, []) or []:
        if not isinstance(skill_id, str):
            continue
        candidate = skill_id.strip()
        if candidate:
            normalized.append(candidate)

    return sorted(dict.fromkeys(normalized))


def compute_deepagent_builtin_skills_revision(skill_ids: list[str] | None) -> str:
    payload = {
        "tool_id": DEEPAGENT_BUILTIN_SKILLS_TOOL_ID,
        "functions": [list_skills.__name__, view_skill.__name__],
        "skill_ids": sorted(dict.fromkeys(skill_ids or [])),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _deepagent_source_identity_values(item: dict[str, Any]) -> set[str]:
    values: set[str] = set()

    def add(value: Any) -> None:
        normalized = str(value or "").strip()
        if normalized:
            values.add(normalized)

    for key in (
        "id",
        "file_id",
        "fileId",
        "document_id",
        "documentId",
        "collection_name",
        "knowledge_id",
        "name",
        "filename",
        "title",
    ):
        add(item.get(key))

    source = item.get("source")
    if isinstance(source, dict):
        for key in ("id", "name", "title", "url"):
            add(source.get(key))

    file_info = item.get("file")
    if isinstance(file_info, dict):
        add(file_info.get("id"))
        add(file_info.get("filename"))
        add(file_info.get("name"))

    return values


def _normalize_deepagent_knowledge_items(items: list[dict] | None) -> list[dict]:
    normalized_items: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("collection_name"):
            normalized_items.append(
                {
                    "id": item.get("collection_name"),
                    "name": item.get("name") or item.get("collection_name"),
                    "type": "collection",
                    "legacy": True,
                }
            )
        elif item.get("collection_names"):
            normalized_items.append(
                {
                    "name": item.get("name"),
                    "type": "collection",
                    "collection_names": item.get("collection_names"),
                    "legacy": True,
                }
            )
        else:
            normalized_items.append(copy.deepcopy(item))
    return normalized_items


def _deepagent_selected_source_candidates(
    *,
    files: list[dict] | None,
    knowledge: list[dict] | None,
    metadata: dict | None,
) -> list[dict]:
    metadata = metadata if isinstance(metadata, dict) else {}
    candidates: list[dict] = []
    for item in files or []:
        if isinstance(item, dict):
            candidates.append(copy.deepcopy(item))
    for item in _normalize_deepagent_knowledge_items(knowledge):
        candidates.append(item)
    for item in metadata.get("folder_knowledge") or []:
        if isinstance(item, dict):
            candidates.append(copy.deepcopy(item))
    return candidates


def _filter_deepagent_sources_by_ids(
    candidates: list[dict], source_ids: list[str] | None
) -> list[dict]:
    requested = {
        str(source_id or "").strip()
        for source_id in (source_ids or [])
        if str(source_id or "").strip()
    }
    if not requested:
        return candidates
    return [
        candidate
        for candidate in candidates
        if _deepagent_source_identity_values(candidate).intersection(requested)
    ]


def _truncate_deepagent_tool_text(value: Any, max_chars: int) -> tuple[str, bool]:
    text = str(value or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    suffix = "\n[excerpt truncated]"
    if max_chars <= len(suffix):
        return text[:max_chars].rstrip(), True
    excerpt_limit = max_chars - len(suffix)
    return f"{text[:excerpt_limit].rstrip()}{suffix}", True


def _compact_deepagent_reference_sources(
    sources: list[dict],
    *,
    max_sources: int = DEEPAGENT_RETRIEVAL_MAX_SOURCES,
    max_chunks: int = DEEPAGENT_RETRIEVAL_MAX_CHUNKS,
    max_chars_per_chunk: int = DEEPAGENT_RETRIEVAL_MAX_CHARS_PER_CHUNK,
    max_total_chars: int = DEEPAGENT_RETRIEVAL_MAX_TOTAL_CHARS,
) -> tuple[list[dict], dict[str, Any]]:
    compact_sources: list[dict] = []
    total_chars = 0
    input_source_count = len([source for source in sources or [] if isinstance(source, dict)])
    input_chunk_count = 0
    output_chunk_count = 0
    truncated = False

    for source in sources or []:
        if not isinstance(source, dict):
            continue
        if len(compact_sources) >= max_sources or output_chunk_count >= max_chunks:
            truncated = True
            break

        documents = source.get("document") if isinstance(source.get("document"), list) else []
        metadatas = source.get("metadata") if isinstance(source.get("metadata"), list) else []
        distances = source.get("distances") if isinstance(source.get("distances"), list) else []
        input_chunk_count += len(documents)

        compact_documents: list[str] = []
        compact_metadatas: list[dict] = []
        compact_distances: list[Any] = []

        for index, document in enumerate(documents):
            if output_chunk_count >= max_chunks or total_chars >= max_total_chars:
                truncated = True
                break

            remaining_chars = max_total_chars - total_chars
            chunk_limit = min(max_chars_per_chunk, max(remaining_chars, 0))
            excerpt, chunk_truncated = _truncate_deepagent_tool_text(
                document,
                chunk_limit,
            )
            if not excerpt:
                continue
            truncated = truncated or chunk_truncated
            total_chars += len(excerpt)
            output_chunk_count += 1

            metadata = (
                copy.deepcopy(metadatas[index])
                if index < len(metadatas) and isinstance(metadatas[index], dict)
                else {}
            )
            if chunk_truncated:
                metadata["excerpt_truncated"] = True
            compact_documents.append(excerpt)
            compact_metadatas.append(metadata)
            if index < len(distances):
                compact_distances.append(distances[index])

        if compact_documents:
            compact_source = {
                **copy.deepcopy(source),
                "document": compact_documents,
                "metadata": compact_metadatas,
            }
            if compact_distances:
                compact_source["distances"] = compact_distances
            compact_sources.append(compact_source)

    if input_source_count > len(compact_sources) or input_chunk_count > output_chunk_count:
        truncated = True

    return compact_sources, {
        "input_source_count": input_source_count,
        "input_chunk_count": input_chunk_count,
        "returned_source_count": len(compact_sources),
        "returned_chunk_count": output_chunk_count,
        "returned_char_count": total_chars,
        "truncated": truncated,
        "budget": {
            "max_sources": max_sources,
            "max_chunks": max_chunks,
            "max_chars_per_chunk": max_chars_per_chunk,
            "max_total_chars": max_total_chars,
        },
    }


def compute_deepagent_builtin_retrieval_revision(
    files: list[dict] | None = None,
    knowledge: list[dict] | None = None,
) -> str:
    source_signatures = []
    for item in [*(files or []), *_normalize_deepagent_knowledge_items(knowledge)]:
        if not isinstance(item, dict):
            continue
        source_signatures.append(
            sorted(_deepagent_source_identity_values(item))
            or [str(item.get("type") or "source")]
        )
    payload = {
        "tool_id": DEEPAGENT_BUILTIN_RETRIEVAL_TOOL_ID,
        "functions": ["query_selected_knowledge_files", "read_selected_file"],
        "sources": source_signatures,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def query_selected_knowledge_files(
    query: str,
    source_ids: Optional[list[str]] = None,
    k: int = 5,
    retrieval_round: int = 1,
    __request__: Any = None,
    __files__: Any = None,
    __knowledge__: Any = None,
    __metadata__: Any = None,
    __user_model__: Any = None,
) -> dict[str, Any]:
    """Search only the currently selected and authorized files or Knowflow knowledge sources.

    :param query: Focused retrieval query for the unresolved evidence gap.
    :param source_ids: Optional selected file, document, or KB ids/names to narrow the search.
    :param k: Maximum chunks per selected source and query.
    :param retrieval_round: Retrieval round number for provenance.
    """

    normalized_query = str(query or "").strip()
    if not normalized_query:
        return {
            "status": "malformed",
            "tool_name": "query_selected_knowledge_files",
            "code": "missing_query",
            "message": "A focused query is required.",
            "retrieval_round": retrieval_round,
        }
    if __request__ is None:
        return {
            "status": "error",
            "tool_name": "query_selected_knowledge_files",
            "code": "runtime_context_unavailable",
            "message": "Selected-source retrieval runtime context is unavailable.",
            "query": normalized_query,
            "retrieval_round": retrieval_round,
        }

    candidates = _deepagent_selected_source_candidates(
        files=__files__,
        knowledge=__knowledge__,
        metadata=__metadata__,
    )
    candidates = _filter_deepagent_sources_by_ids(candidates, source_ids)
    if not candidates:
        return {
            "status": "permission_denied" if source_ids else "no_evidence",
            "tool_name": "query_selected_knowledge_files",
            "code": "requested_source_out_of_scope" if source_ids else "no_selected_sources",
            "query": normalized_query,
            "retrieval_round": retrieval_round,
        }

    from open_webui.retrieval.utils import get_sources_from_items

    sources = await get_sources_from_items(
        request=__request__,
        items=candidates,
        queries=[normalized_query],
        embedding_function=lambda text, prefix: __request__.app.state.EMBEDDING_FUNCTION(
            text, prefix=prefix, user=__user_model__
        ),
        k=max(1, min(int(k or 5), 8)),
        reranking_function=(
            (
                lambda query_text, documents: __request__.app.state.RERANKING_FUNCTION(
                    query_text, documents, user=__user_model__
                )
            )
            if __request__.app.state.RERANKING_FUNCTION
            else None
        ),
        k_reranker=__request__.app.state.config.TOP_K_RERANKER,
        r=__request__.app.state.config.RELEVANCE_THRESHOLD,
        hybrid_bm25_weight=__request__.app.state.config.HYBRID_BM25_WEIGHT,
        hybrid_search=__request__.app.state.config.ENABLE_RAG_HYBRID_SEARCH,
        full_context=False,
        user=__user_model__,
    )
    if not sources:
        return {
            "status": "no_evidence",
            "tool_name": "query_selected_knowledge_files",
            "code": "empty_retrieval_result",
            "query": normalized_query,
            "retrieval_round": retrieval_round,
        }

    sources, diagnostics = _filter_selected_retrieval_sources_by_query(
        sources, normalized_query
    )
    if not sources:
        return {
            "status": "no_evidence",
            "tool_name": "query_selected_knowledge_files",
            "code": "weak_or_empty_retrieval_result",
            "query": normalized_query,
            "retrieval_round": retrieval_round,
            "retrieval_diagnostics": diagnostics,
        }

    for source in sources:
        if not isinstance(source, dict):
            continue
        for metadata in source.get("metadata") or []:
            if isinstance(metadata, dict):
                metadata.setdefault(
                    "retrieval_tool_name", "query_selected_knowledge_files"
                )
                metadata.setdefault("retrieval_round", retrieval_round)
                metadata.setdefault("query", normalized_query)

    compact_sources, context_budget = _compact_deepagent_reference_sources(sources)
    return {
        "status": "success",
        "tool_name": "query_selected_knowledge_files",
        "query": normalized_query,
        "retrieval_round": retrieval_round,
        "canonical_references": compact_sources,
        "result_count": len(sources),
        "context_budget": context_budget,
    }


async def read_selected_file(
    source_id: str,
    query: str = "",
    retrieval_round: int = 1,
    __request__: Any = None,
    __files__: Any = None,
    __knowledge__: Any = None,
    __metadata__: Any = None,
    __user_model__: Any = None,
) -> dict[str, Any]:
    """Read an already selected source when its identity is known and authorized.

    :param source_id: Selected file, document, or KB id/name to read.
    :param query: Optional focus query used only for provenance.
    :param retrieval_round: Retrieval round number for provenance.
    """

    normalized_source_id = str(source_id or "").strip()
    if not normalized_source_id:
        return {
            "status": "malformed",
            "tool_name": "read_selected_file",
            "code": "missing_source_id",
            "message": "A selected source id is required.",
            "retrieval_round": retrieval_round,
        }
    if __request__ is None:
        return {
            "status": "error",
            "tool_name": "read_selected_file",
            "code": "runtime_context_unavailable",
            "source_id": normalized_source_id,
            "retrieval_round": retrieval_round,
        }

    candidates = _deepagent_selected_source_candidates(
        files=__files__,
        knowledge=__knowledge__,
        metadata=__metadata__,
    )
    candidates = _filter_deepagent_sources_by_ids(candidates, [normalized_source_id])
    if len(candidates) != 1:
        return {
            "status": "permission_denied" if not candidates else "malformed",
            "tool_name": "read_selected_file",
            "code": "requested_file_out_of_scope"
            if not candidates
            else "ambiguous_selected_source",
            "source_id": normalized_source_id,
            "retrieval_round": retrieval_round,
        }

    from open_webui.retrieval.utils import get_sources_from_items

    source_item = {**candidates[0], "context": "full"}
    sources = await get_sources_from_items(
        request=__request__,
        items=[source_item],
        queries=[str(query or normalized_source_id).strip()],
        embedding_function=lambda text, prefix: __request__.app.state.EMBEDDING_FUNCTION(
            text, prefix=prefix, user=__user_model__
        ),
        k=1,
        reranking_function=None,
        k_reranker=__request__.app.state.config.TOP_K_RERANKER,
        r=__request__.app.state.config.RELEVANCE_THRESHOLD,
        hybrid_bm25_weight=__request__.app.state.config.HYBRID_BM25_WEIGHT,
        hybrid_search=False,
        full_context=True,
        user=__user_model__,
    )
    if not sources:
        return {
            "status": "no_evidence",
            "tool_name": "read_selected_file",
            "code": "empty_retrieval_result",
            "source_id": normalized_source_id,
            "retrieval_round": retrieval_round,
        }

    for source in sources:
        if not isinstance(source, dict):
            continue
        for metadata in source.get("metadata") or []:
            if isinstance(metadata, dict):
                metadata.setdefault("retrieval_tool_name", "read_selected_file")
                metadata.setdefault("retrieval_round", retrieval_round)
                metadata.setdefault("query", str(query or "").strip())

    compact_sources, context_budget = _compact_deepagent_reference_sources(
        sources,
        max_sources=1,
        max_chunks=8,
        max_chars_per_chunk=DEEPAGENT_READ_MAX_CHARS_PER_CHUNK,
        max_total_chars=DEEPAGENT_READ_MAX_TOTAL_CHARS,
    )
    return {
        "status": "success",
        "tool_name": "read_selected_file",
        "source_id": normalized_source_id,
        "query": str(query or "").strip(),
        "retrieval_round": retrieval_round,
        "canonical_references": compact_sources,
        "result_count": len(sources),
        "context_budget": context_budget,
    }


def _sanitize_deepagent_runtime_parameters(parameters: dict | None) -> dict:
    if not isinstance(parameters, dict):
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    sanitized = copy.deepcopy(parameters)
    properties = sanitized.get("properties", {})
    if isinstance(properties, dict):
        sanitized["properties"] = {
            key: value
            for key, value in properties.items()
            if not str(key).startswith("__")
        }

    required = sanitized.get("required", [])
    if isinstance(required, list):
        sanitized["required"] = [
            key for key in required if not str(key).startswith("__")
        ]

    sanitized.setdefault("type", "object")
    sanitized.setdefault("properties", {})
    sanitized.setdefault("required", [])
    return sanitized


def _build_deepagent_registered_tool_name(
    tool_id: str, function_name: str, revision: str
) -> str:
    safe_tool_id = re.sub(r"[^A-Za-z0-9_]", "_", str(tool_id or "").strip()) or "tool"
    digest = hashlib.sha256(
        f"{tool_id}:{function_name}:{revision}".encode("utf-8")
    ).hexdigest()[:10]
    prefix = f"{DEEPAGENT_RUNTIME_TOOL_PREFIX}{safe_tool_id}__"
    max_tool_id_len = max(8, 64 - len(DEEPAGENT_RUNTIME_TOOL_PREFIX) - len(digest) - 2)
    if len(safe_tool_id) > max_tool_id_len:
        safe_tool_id = safe_tool_id[:max_tool_id_len]
        prefix = f"{DEEPAGENT_RUNTIME_TOOL_PREFIX}{safe_tool_id}__"
    return f"{prefix}{digest}"


def _build_deepagent_builtin_function_entry(
    func: Callable, registered_name: str = ""
) -> dict[str, Any]:
    spec = clean_openai_tool_schema(
        convert_pydantic_model_to_openai_function_spec(
            convert_function_to_pydantic_model(func)
        )
    )
    function_name = str(spec.get("name") or func.__name__).strip() or func.__name__
    registered_name = str(registered_name or function_name).strip() or function_name
    return {
        "registered_name": registered_name,
        "function_name": function_name,
        "description": str(spec.get("description") or function_name),
        "openai_tool": {
            "type": "function",
            "function": {
                "name": registered_name,
                "description": str(spec.get("description") or function_name),
                "parameters": _sanitize_deepagent_runtime_parameters(
                    spec.get("parameters")
                    if isinstance(spec.get("parameters"), dict)
                    else None
                ),
            },
        },
    }


def build_deepagent_runtime_tool_snapshot(
    tool_ids: list[str] | None,
    user: UserModel,
    *,
    files: list[dict] | None = None,
    metadata: dict | None = None,
    model_knowledge: list[dict] | None = None,
    db=None,
) -> dict[str, Any]:
    requested_tool_ids = [
        str(tool_id).strip()
        for tool_id in (tool_ids or [])
        if isinstance(tool_id, str) and str(tool_id).strip()
    ]
    if user.role == "admin":
        user_group_ids: set[str] = set()
    else:
        user_group_ids = get_user_group_ids(user.id, db=db)

    runtime_tools: list[dict[str, Any]] = []

    for tool_id in requested_tool_ids:
        if tool_id.startswith("server:"):
            continue

        tool = Tools.get_tool_by_id(tool_id, db=db)
        if tool is None:
            continue
        if not is_tool_catalog_visible(tool, user, user_group_ids, db=db):
            continue

        revision = _compute_deepagent_tool_revision(tool)
        functions: list[dict[str, Any]] = []

        for spec in list(getattr(tool, "specs", []) or []):
            if not isinstance(spec, dict):
                continue

            function_name = str(spec.get("name") or "").strip()
            if not function_name:
                continue

            registered_name = _build_deepagent_registered_tool_name(
                tool.id,
                function_name,
                revision,
            )
            parameters = _sanitize_deepagent_runtime_parameters(
                spec.get("parameters") if isinstance(spec.get("parameters"), dict) else None
            )

            functions.append(
                {
                    "registered_name": registered_name,
                    "function_name": function_name,
                    "description": str(spec.get("description") or function_name),
                    "openai_tool": {
                        "type": "function",
                        "function": {
                            "name": registered_name,
                            "description": str(spec.get("description") or function_name),
                            "parameters": parameters,
                        },
                    },
                }
            )

        if not functions:
            continue

        runtime_tools.append(
            {
                "tool_id": tool.id,
                "tool_name": tool.name,
                "revision": revision,
                "updated_at": int(getattr(tool, "updated_at", 0) or 0),
                "functions": functions,
            }
        )

    runtime_skill_ids = get_deepagent_runtime_skill_ids(metadata)
    if runtime_skill_ids:
        revision = compute_deepagent_builtin_skills_revision(runtime_skill_ids)
        functions = [
            _build_deepagent_builtin_function_entry(func)
            for func in (list_skills, view_skill)
        ]

        runtime_tools.append(
            {
                "tool_id": DEEPAGENT_BUILTIN_SKILLS_TOOL_ID,
                "tool_name": "Workspace Skills",
                "revision": revision,
                "updated_at": 0,
                "functions": functions,
            }
        )

    retrieval_candidates = _deepagent_selected_source_candidates(
        files=files,
        knowledge=model_knowledge,
        metadata=metadata,
    )
    if retrieval_candidates:
        revision = compute_deepagent_builtin_retrieval_revision(
            files=files,
            knowledge=model_knowledge,
        )
        runtime_tools.append(
            {
                "tool_id": DEEPAGENT_BUILTIN_RETRIEVAL_TOOL_ID,
                "tool_name": "Selected Source Retrieval",
                "revision": revision,
                "updated_at": 0,
                "functions": [
                    _build_deepagent_builtin_function_entry(
                        query_selected_knowledge_files
                    ),
                    _build_deepagent_builtin_function_entry(read_selected_file),
                ],
            }
        )

    sanitized_metadata = copy.deepcopy(metadata or {})
    sanitized_metadata.pop("deepagent_runtime_tools", None)
    sanitized_metadata.pop("model", None)

    return {
        "version": DEEPAGENT_RUNTIME_TOOL_SCHEMA_VERSION,
        "tools": runtime_tools,
        "context": {
            "user_id": user.id,
            "chat_id": sanitized_metadata.get("chat_id"),
            "session_id": sanitized_metadata.get("session_id"),
            "message_id": sanitized_metadata.get("message_id"),
            "files": copy.deepcopy(files or []),
            "knowledge": copy.deepcopy(model_knowledge or []),
            "metadata": sanitized_metadata,
        },
    }


def resolve_schema(schema, components):
    """
    Recursively resolves a JSON schema using OpenAPI components.
    """
    if not schema:
        return {}

    if "$ref" in schema:
        ref_path = schema["$ref"]
        ref_parts = ref_path.strip("#/").split("/")
        resolved = components
        for part in ref_parts[1:]:  # Skip the initial 'components'
            resolved = resolved.get(part, {})
        return resolve_schema(resolved, components)

    resolved_schema = copy.deepcopy(schema)

    # Recursively resolve inner schemas
    if "properties" in resolved_schema:
        for prop, prop_schema in resolved_schema["properties"].items():
            resolved_schema["properties"][prop] = resolve_schema(
                prop_schema, components
            )

    if "items" in resolved_schema:
        resolved_schema["items"] = resolve_schema(resolved_schema["items"], components)

    return resolved_schema


def convert_openapi_to_tool_payload(openapi_spec):
    """
    Converts an OpenAPI specification into a custom tool payload structure.

    Args:
        openapi_spec (dict): The OpenAPI specification as a Python dict.

    Returns:
        list: A list of tool payloads.
    """
    tool_payload = []

    for path, methods in openapi_spec.get("paths", {}).items():
        for method, operation in methods.items():
            if operation.get("operationId"):
                tool = {
                    "name": operation.get("operationId"),
                    "description": operation.get(
                        "description",
                        operation.get("summary", "No description available."),
                    ),
                    "parameters": {"type": "object", "properties": {}, "required": []},
                }

                for param in operation.get("parameters", []):
                    param_name = param.get("name")
                    if not param_name:
                        continue
                    param_schema = param.get("schema", {})
                    description = param_schema.get("description", "")
                    if not description:
                        description = param.get("description") or ""
                    if param_schema.get("enum") and isinstance(
                        param_schema.get("enum"), list
                    ):
                        description += (
                            f". Possible values: {', '.join(param_schema.get('enum'))}"
                        )
                    param_property = {
                        "type": param_schema.get("type") or "string",
                        "description": description,
                    }

                    # Include items property for array types (required by OpenAI)
                    if param_schema.get("type") == "array" and "items" in param_schema:
                        param_property["items"] = param_schema["items"]

                    # Filter out None values to prevent schema validation errors
                    param_property = {
                        k: v for k, v in param_property.items() if v is not None
                    }

                    tool["parameters"]["properties"][param_name] = param_property
                    if param.get("required"):
                        tool["parameters"]["required"].append(param_name)

                # Extract and resolve requestBody if available
                request_body = operation.get("requestBody")
                if request_body:
                    content = request_body.get("content", {})
                    json_schema = content.get("application/json", {}).get("schema")
                    if json_schema:
                        resolved_schema = resolve_schema(
                            json_schema, openapi_spec.get("components", {})
                        )

                        if resolved_schema.get("properties"):
                            tool["parameters"]["properties"].update(
                                resolved_schema["properties"]
                            )
                            if "required" in resolved_schema:
                                tool["parameters"]["required"] = list(
                                    set(
                                        tool["parameters"]["required"]
                                        + resolved_schema["required"]
                                    )
                                )
                        elif resolved_schema.get("type") == "array":
                            tool["parameters"] = (
                                resolved_schema  # special case for array
                            )

                tool_payload.append(tool)

    return tool_payload


async def set_tool_servers(request: Request):
    request.app.state.TOOL_SERVERS = await get_tool_servers_data(
        request.app.state.config.TOOL_SERVER_CONNECTIONS
    )

    if request.app.state.redis is not None:
        await request.app.state.redis.set(
            "tool_servers", json.dumps(request.app.state.TOOL_SERVERS)
        )

    return request.app.state.TOOL_SERVERS


async def get_tool_servers(request: Request):
    tool_servers = []
    if request.app.state.redis is not None:
        try:
            tool_servers = json.loads(await request.app.state.redis.get("tool_servers"))
            request.app.state.TOOL_SERVERS = tool_servers
        except Exception as e:
            log.error(f"Error fetching tool_servers from Redis: {e}")

    if not tool_servers:
        tool_servers = await set_tool_servers(request)

    return tool_servers


async def get_terminal_cwd(
    base_url: str,
    headers: dict,
    cookies: Optional[dict] = None,
) -> Optional[str]:
    """Fetch the current working directory from a terminal server."""
    try:
        cwd_url = f"{base_url.rstrip('/')}/files/cwd"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
            trust_env=True,
        ) as session:
            async with session.get(
                cwd_url, headers=headers, cookies=cookies or {}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("cwd")
    except Exception as e:
        log.debug(f"Failed to fetch terminal CWD: {e}")
    return None


async def set_terminal_servers(request: Request):
    """Load and cache OpenAPI specs from all TERMINAL_SERVER_CONNECTIONS."""
    connections = request.app.state.config.TERMINAL_SERVER_CONNECTIONS or []

    # Build server configs compatible with get_tool_servers_data
    # Terminal connections store id/name at top level; translate to info dict
    server_configs = []
    for connection in connections:
        if not connection.get("url"):
            continue

        enabled = connection.get("enabled", True)

        server_configs.append(
            {
                "url": connection.get("url", ""),
                "key": connection.get("key", ""),
                "auth_type": connection.get("auth_type", "bearer"),
                "path": connection.get("path", "/openapi.json"),
                "spec_type": "url",
                # get_tool_servers_data reads config.enable to filter active servers
                "config": {"enable": enabled},
                "info": {
                    "id": connection.get("id", ""),
                    "name": connection.get("name", ""),
                },
            }
        )

    request.app.state.TERMINAL_SERVERS = await get_tool_servers_data(server_configs)

    if request.app.state.redis is not None:
        await request.app.state.redis.set(
            "terminal_servers", json.dumps(request.app.state.TERMINAL_SERVERS)
        )

    return request.app.state.TERMINAL_SERVERS


async def get_terminal_servers(request: Request):
    """Return cached terminal server specs, loading if needed."""
    terminal_servers = []
    if request.app.state.redis is not None:
        try:
            terminal_servers = json.loads(
                await request.app.state.redis.get("terminal_servers")
            )
            request.app.state.TERMINAL_SERVERS = terminal_servers
        except Exception as e:
            log.error(f"Error fetching terminal_servers from Redis: {e}")

    if not terminal_servers:
        terminal_servers = await set_terminal_servers(request)

    return terminal_servers


async def get_terminal_tools(
    request: Request,
    terminal_id: str,
    user: UserModel,
    extra_params: dict,
) -> dict[str, dict]:
    """Resolve tools for a terminal server identified by terminal_id.

    - Finds the connection in TERMINAL_SERVER_CONNECTIONS
    - Checks access_grants
    - Loads specs from cache
    - Builds callables that route through the terminal proxy
    """
    connections = request.app.state.config.TERMINAL_SERVER_CONNECTIONS or []
    connection = next((c for c in connections if c.get("id") == terminal_id), None)
    if connection is None:
        log.warning(f"Terminal server not found: {terminal_id}")
        return {}

    user_group_ids = {group.id for group in Groups.get_groups_by_member_id(user.id)}
    if not has_connection_access(user, connection, user_group_ids):
        log.warning(f"Access denied to terminal {terminal_id} for user {user.id}")
        return {}

    # Find the cached spec data for this terminal
    terminal_servers = await get_terminal_servers(request)
    server_data = next(
        (s for s in terminal_servers if s.get("id") == terminal_id), None
    )
    if server_data is None:
        log.warning(f"Terminal server spec not found for {terminal_id}")
        return {}

    specs = server_data.get("specs", [])
    if not specs:
        return {}

    # Build auth headers
    auth_type = connection.get("auth_type", "bearer")
    cookies = {}
    headers = {"Content-Type": "application/json", "X-User-Id": user.id}

    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {connection.get('key', '')}"
    elif auth_type == "session":
        cookies = request.cookies
        headers["Authorization"] = f"Bearer {request.state.token.credentials}"
    elif auth_type == "system_oauth":
        cookies = request.cookies
        oauth_token = extra_params.get("__oauth_token__", None)
        if oauth_token:
            headers["Authorization"] = f"Bearer {oauth_token.get('access_token', '')}"
    # auth_type == "none": no Authorization header

    terminal_cwd = await get_terminal_cwd(connection.get("url", ""), headers, cookies)

    tools_dict = {}
    for spec in specs:
        function_name = spec["name"]

        # Inject CWD into run_command description
        tool_spec = clean_openai_tool_schema(spec)
        if function_name == "run_command" and terminal_cwd:
            tool_spec["description"] = (
                tool_spec.get("description", "")
                + f"\n\nThe current working directory is: {terminal_cwd}"
            )

        def make_tool_function(fn_name, srv_data, hdrs, cks):
            async def tool_function(**kwargs):
                return await execute_tool_server(
                    url=srv_data["url"],
                    headers=hdrs,
                    cookies=cks,
                    name=fn_name,
                    params=kwargs,
                    server_data=srv_data,
                )

            return tool_function

        tool_function = make_tool_function(function_name, server_data, headers, cookies)
        callable = get_async_tool_function_and_apply_extra_params(tool_function, {})

        tools_dict[function_name] = {
            "tool_id": f"terminal:{terminal_id}",
            "callable": callable,
            "spec": tool_spec,
            "type": "terminal",
        }

    return tools_dict


async def get_tool_server_data(url: str, headers: Optional[dict]) -> Dict[str, Any]:
    _headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if headers:
        _headers.update(headers)

    error = None
    try:
        timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url, headers=_headers, ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL
            ) as response:
                if response.status != 200:
                    error_body = await response.json()
                    raise Exception(error_body)

                text_content = None

                # Check if URL ends with .yaml or .yml to determine format
                if url.lower().endswith((".yaml", ".yml")):
                    text_content = await response.text()
                    res = yaml.safe_load(text_content)
                else:
                    text_content = await response.text()

                try:
                    res = json.loads(text_content)
                except json.JSONDecodeError:
                    try:
                        res = yaml.safe_load(text_content)
                    except Exception as e:
                        raise e

    except Exception as err:
        log.exception(f"Could not fetch tool server spec from {url}")
        if isinstance(err, dict) and "detail" in err:
            error = err["detail"]
        else:
            error = str(err)
        raise Exception(error)

    log.debug(f"Fetched data: {res}")
    return res


async def get_tool_servers_data(servers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Prepare list of enabled servers along with their original index

    tasks = []
    server_entries = []
    for idx, server in enumerate(servers):
        if (
            server.get("config", {}).get("enable")
            and server.get("type", "openapi") == "openapi"
        ):
            info = server.get("info", {})

            auth_type = server.get("auth_type", "bearer")
            token = None

            if auth_type == "bearer":
                token = server.get("key", "")
            elif auth_type == "none":
                # No authentication
                pass

            id = info.get("id")
            if not id:
                id = str(idx)

            server_url = server.get("url")
            spec_type = server.get("spec_type", "url")

            # Create async tasks to fetch data
            task = None
            if spec_type == "url":
                # Path (to OpenAPI spec URL) can be either a full URL or a path to append to the base URL
                openapi_path = server.get("path", "openapi.json")
                spec_url = get_tool_server_url(server_url, openapi_path)
                # Fetch from URL
                task = get_tool_server_data(
                    spec_url,
                    {"Authorization": f"Bearer {token}"} if token else None,
                )
            elif spec_type == "json" and server.get("spec", ""):
                # Use provided JSON spec
                spec_json = None
                try:
                    spec_json = json.loads(server.get("spec", ""))
                except Exception as e:
                    log.error(f"Error parsing JSON spec for tool server {id}: {e}")

                if spec_json:
                    task = asyncio.sleep(
                        0,
                        result=spec_json,
                    )

            if task:
                tasks.append(task)
                server_entries.append((id, idx, server, server_url, info, token))

    # Execute tasks concurrently
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    # Build final results with index and server metadata
    results = []
    for (id, idx, server, url, info, _), response in zip(server_entries, responses):
        if isinstance(response, Exception):
            log.error(f"Failed to connect to {url} OpenAPI tool server")
            continue

        # Guard against invalid or non-OpenAPI specs (e.g., MCP-style configs)
        if not isinstance(response, dict) or "paths" not in response:
            log.warning(f"Invalid OpenAPI spec from {url}: missing 'paths'")
            continue

        response = {
            "openapi": response,
            "info": response.get("info", {}),
            "specs": convert_openapi_to_tool_payload(response),
        }

        openapi_data = response.get("openapi", {})
        if info and isinstance(openapi_data, dict):
            openapi_data["info"] = openapi_data.get("info", {})

            if "name" in info:
                openapi_data["info"]["title"] = info.get("name", "Tool Server")

            if "description" in info:
                openapi_data["info"]["description"] = info.get("description", "")

        results.append(
            {
                "id": str(id),
                "idx": idx,
                "url": (server.get("url") or "").rstrip("/"),
                "openapi": openapi_data,
                "info": response.get("info"),
                "specs": response.get("specs"),
            }
        )

    return results


async def execute_tool_server(
    url: str,
    headers: Dict[str, str],
    cookies: Dict[str, str],
    name: str,
    params: Dict[str, Any],
    server_data: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    error = None
    try:
        openapi = server_data.get("openapi", {})
        paths = openapi.get("paths", {})

        matching_route = None
        for route_path, methods in paths.items():
            for http_method, operation in methods.items():
                if isinstance(operation, dict) and operation.get("operationId") == name:
                    matching_route = (route_path, methods)
                    break
            if matching_route:
                break

        if not matching_route:
            raise Exception(f"No matching route found for operationId: {name}")

        route_path, methods = matching_route

        method_entry = None
        for http_method, operation in methods.items():
            if operation.get("operationId") == name:
                method_entry = (http_method.lower(), operation)
                break

        if not method_entry:
            raise Exception(f"No matching method found for operationId: {name}")

        http_method, operation = method_entry

        path_params = {}
        query_params = {}
        body_params = {}

        for param in operation.get("parameters", []):
            param_name = param.get("name")
            if not param_name:
                continue
            param_in = param.get("in")
            if param_name in params:
                if param_in == "path":
                    path_params[param_name] = params[param_name]
                elif param_in == "query":
                    if params[param_name] is not None:
                        query_params[param_name] = params[param_name]

        final_url = f"{url.rstrip('/')}{route_path}"
        for key, value in path_params.items():
            final_url = final_url.replace(f"{{{key}}}", str(value))

        if query_params:
            query_string = "&".join(f"{k}={v}" for k, v in query_params.items())
            final_url = f"{final_url}?{query_string}"

        if operation.get("requestBody", {}).get("content"):
            if params:
                body_params = params

        async with aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        ) as session:
            request_method = getattr(session, http_method.lower())

            if http_method in ["post", "put", "patch", "delete"]:
                async with request_method(
                    final_url,
                    json=body_params,
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL,
                    allow_redirects=False,
                ) as response:
                    if response.status >= 400:
                        text = await response.text()
                        raise Exception(f"HTTP error {response.status}: {text}")

                    try:
                        response_data = await response.json()
                    except Exception:
                        response_data = await response.text()

                    response_headers = response.headers
                    return (response_data, response_headers)
            else:
                async with request_method(
                    final_url,
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL,
                    allow_redirects=False,
                ) as response:
                    if response.status >= 400:
                        text = await response.text()
                        raise Exception(f"HTTP error {response.status}: {text}")

                    try:
                        response_data = await response.json()
                    except Exception:
                        response_data = await response.text()

                    response_headers = response.headers
                    return (response_data, response_headers)

    except Exception as err:
        error = str(err)
        log.exception(f"API Request Error: {error}")
        return ({"error": error}, None)


def get_tool_server_url(url: Optional[str], path: str) -> str:
    """
    Build the full URL for a tool server, given a base url and a path.
    """
    if "://" in path:
        # If it contains "://", it's a full URL
        return path
    if url:
        url = url.rstrip("/")
    if not path.startswith("/"):
        # Ensure the path starts with a slash
        path = f"/{path}"
    return f"{url}{path}"
