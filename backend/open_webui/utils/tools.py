import inspect
import logging
import re
import inspect
import aiohttp
import asyncio
import yaml
import json
import hashlib
import unicodedata

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
SELECTED_SOURCE_RETRIEVAL_DEFAULT_TIMEOUT_SECONDS = 25.0
SELECTED_SOURCE_RETRIEVAL_MAX_TIMEOUT_SECONDS = 90.0
_SELECTED_SOURCE_RAW_PATH_KEYS = frozenset(
    {
        "path",
        "filepath",
        "file_path",
        "local_path",
        "server_path",
        "storage_path",
        "abs_path",
        "absolute_path",
        "upload_path",
    }
)
SELECTED_SOURCE_RETRIEVAL_DESCRIPTOR_BY_TOOL: dict[str, dict[str, Any]] = {
    "query_selected_knowledge_files": {
        "tool_name": "query_selected_knowledge_files",
        "full_context": False,
        "default_k": 5,
        "max_k": 8,
        "max_sources": DEEPAGENT_RETRIEVAL_MAX_SOURCES,
        "max_chunks": DEEPAGENT_RETRIEVAL_MAX_CHUNKS,
        "max_chars_per_chunk": DEEPAGENT_RETRIEVAL_MAX_CHARS_PER_CHUNK,
        "max_total_chars": DEEPAGENT_RETRIEVAL_MAX_TOTAL_CHARS,
        "default_evidence_need": "balanced",
    },
    "read_selected_file": {
        "tool_name": "read_selected_file",
        "full_context": True,
        "default_k": 1,
        "max_k": 1,
        "max_sources": 1,
        "max_chunks": 8,
        "max_chars_per_chunk": DEEPAGENT_READ_MAX_CHARS_PER_CHUNK,
        "max_total_chars": DEEPAGENT_READ_MAX_TOTAL_CHARS,
        "default_evidence_need": "focused_read",
    },
}
_METADATA_FIRST_SELECTED_SOURCE_EVIDENCE_NEEDS = frozenset(
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
_SEMANTIC_DETAIL_SELECTED_SOURCE_EVIDENCE_NEEDS = frozenset(
    {
        "narrow_fact",
        "selected_source_retrieval",
        "focused_read",
        "semantic_detail",
        "semantic_chunks",
    }
)
_SELECTED_SOURCE_METADATA_FIRST_QUERY_FACET_PATTERN = re.compile(
    r"\b(?:document|doc_id|quota_id|table_label|row_label|answer_fields|"
    r"unit_terms|standard_no|document_no|evidence_facets|topic_terms|"
    r"requested_types|user_anchors)\s*=",
    flags=re.IGNORECASE,
)
_SELECTED_SOURCE_WEAK_METADATA_FIRST_QUERY_HINT_PATTERN = re.compile(
    r"\b(?:list|listing|inventory|catalog|metadata|table\s+of\s+contents|toc|"
    r"count|statistics|latest|recent)\b|"
    r"(?:列出|清单|目录|元数据|目录结构|统计|最近|最新|前两年|近两年|近三年|公文|论文|讲话|政策|相关)",
    flags=re.IGNORECASE,
)
_SELECTED_SOURCE_WEAK_NARROW_QUERY_HINT_PATTERN = re.compile(
    r"\b(?:what\s+is|define|definition|explain|how|why)\b|"
    r"(?:什么是|解释|定义|如何|为什么|怎么|详情)",
    flags=re.IGNORECASE,
)
_SELECTED_SOURCE_RECENCY_CLAIM_PATTERN = re.compile(
    r"\b(?:latest|recent|newest|most\s+recent)\b|"
    r"(?:最新|最近|近(?:两|2|三|3)年|前(?:两|2|三|3)年)",
    flags=re.IGNORECASE,
)
_SELECTED_SOURCE_YEAR_RANGE_CLAIM_PATTERN = re.compile(
    r"\b(?:19|20)\d{2}\b|"
    r"(?:前(?:两|2|三|3)年|近(?:两|2|三|3)年|最近(?:两|2|三|3)年|近年)",
    flags=re.IGNORECASE,
)
_SELECTED_SOURCE_TYPE_CLAIM_PATTERN = re.compile(
    r"\b(?:policy|policies|paper|papers|speech|speeches|report|reports|notice|notices)\b|"
    r"(?:公文|论文|讲话|政策|报告|通知|纪要)",
    flags=re.IGNORECASE,
)
_SELECTED_SOURCE_TOPIC_CLAIM_PATTERN = re.compile(
    r"\b(?:related\s+to|about|topic|entity|with)\b|"
    r"(?:相关|关于|围绕|主题|话题|有关)",
    flags=re.IGNORECASE,
)
_STRUCTURED_FACT_STANDARD_NO_PATTERN = re.compile(
    r"\b(?:GB|GB/T|SL|DL|JGJ|JGJ/T|NB|NB/T|NY|NY/T|TD|TD/T|HJ|AQ|AQ/T)"
    r"\s*[- ]?\d{2,6}(?:[-—]\d{2,4})\b",
    flags=re.IGNORECASE,
)
_STRUCTURED_FACT_QUOTA_ID_PATTERN = re.compile(
    r"(?:\bquota[_\s-]?id\s*[:=]?\s*|定额(?:编号|号|id)?\s*[:：]?\s*)"
    r"(0\d{4,6}|[1-9]\d{4,6})(?!\d)",
    flags=re.IGNORECASE,
)
_STRUCTURED_FACT_ROW_TABLE_LABEL_PATTERN = re.compile(
    r"(?:表\s*\d+(?:[.\-．]\d+){0,3}|table\s*\d+(?:[.\-]\d+){0,3}|"
    r"第[一二三四五六七八九十百千零〇\d]{1,8}(?:行|列|表))",
    flags=re.IGNORECASE,
)
_STRUCTURED_FACT_DOCUMENT_NO_PATTERN = re.compile(
    r"(?:文号|发文号|文件号)\s*[:：]?\s*([^\s,，;；。]{3,40})"
)
_STRUCTURED_FACT_UNIT_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?)\s*(?:t/\(km2·a\)|t/\(km²·a\)|工日|m3|m²|m2|元|台班|kg|m|km2·a)",
    flags=re.IGNORECASE,
)
_STRUCTURED_FACT_KNOWN_SLOT_KEYWORDS = frozenset(
    {
        "labor",
        "farmyard manure",
        "other material cost",
        "拖拉机37w",
    }
)
_STRUCTURED_FACT_SLOT_REQUEST_PATTERN = re.compile(
    r"(?:提取|抽取|列出|给出|返回|输出|extract|list|provide)\s*"
    r"(?:如下|以下|字段|slot|slots|fields?)?\s*[:：]?\s*([^。；;\n]{2,160})",
    flags=re.IGNORECASE,
)
_STRUCTURED_FACT_SLOT_CONNECTOR_PATTERN = re.compile(
    r"\s*(?:和|及|与|以及|并且|and)\s*",
    flags=re.IGNORECASE,
)
_STRUCTURED_FACT_NON_ANCHOR_REQUIRED_PATTERN = re.compile(
    r"(?:\b(?:field|fields|slot|slots|mapping|value|values)\b|"
    r"字段|取值|值|单位行|表头|header|unit\s*row)",
    flags=re.IGNORECASE,
)
_STRUCTURED_FACT_SUPPORTED_STATE = "supported"
_STRUCTURED_FACT_UNSUPPORTED_STATE = "unsupported"
_STRUCTURED_FACT_CONFLICTING_STATE = "conflicting"
_STRUCTURED_FACT_TRUNCATED_STATE = "truncated"
_STRUCTURED_FACT_PRECISION_MODE_COMPATIBILITY = "compatibility"
_STRUCTURED_FACT_PRECISION_MODE_DISABLED = "disabled"
_STRUCTURED_FACT_STANDARD_BUNDLE_NEIGHBOR_CHUNKS = 1
_STRUCTURED_FACT_STANDARD_MAX_BUNDLES = 8
_STRUCTURED_FACT_TABLE_BUNDLE_NEIGHBOR_CHUNKS = 2
_STRUCTURED_FACT_TABLE_MAX_CANDIDATE_ROWS = 8
_STRUCTURED_FACT_ANCHOR_STRIP_PATTERN = re.compile(
    r"[\s\-\u2013\u2014_:/\\|,，.;；。!?！？'\"“”‘’\[\]{}()（）<>《》]+"
)
_STRUCTURED_FACT_TABLE_UNIT_TOKENS = frozenset(
    {"工日", "m3", "m2", "m²", "元", "台班", "kg"}
)
_STRUCTURED_FACT_TABLE_FIELD_UNIT_HINTS = {
    "labor": ("工日",),
    "farmyard manure": ("m3",),
    "other material cost": ("元",),
    "拖拉机37w": ("台班",),
}
_STRUCTURED_FACT_SECTION_METADATA_KEYS = (
    "heading",
    "section",
    "section_title",
    "chapter",
    "clause",
    "breadcrumb",
    "breadcrumbs",
)


def _selected_retrieval_dedupe_terms(values: list[Any]) -> list[str]:
    deduped: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def _split_structured_retrieval_terms(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    else:
        values = [value]
    terms: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        for part in re.split(r"[;,，、|/]", text):
            normalized = part.strip()
            if normalized and normalized not in terms:
                terms.append(normalized)
    return terms


def _selected_retrieval_structured_terms(query: str) -> dict[str, list[str]]:
    terms = {
        "document": [],
        "doc_id": [],
        "quota_id": [],
        "table_label": [],
        "row_label": [],
        "answer_fields": [],
        "unit_terms": [],
        "standard_no": [],
        "document_no": [],
        "evidence_facets": [],
        "topic_terms": [],
        "requested_types": [],
        "user_anchors": [],
        "date_basis": [],
        "type_basis": [],
        "topic_basis": [],
    }
    for key, raw_value in re.findall(
        r"\b(document|doc_id|quota_id|table_label|row_label|answer_fields|"
        r"unit_terms|standard_no|document_no|evidence_facets|topic_terms|"
        r"requested_types|user_anchors|date_basis|type_basis|topic_basis)\s*=\s*([^;\n]+)",
        str(query or ""),
        flags=re.IGNORECASE,
    ):
        normalized_key = str(key or "").strip().lower()
        if normalized_key not in terms:
            continue
        for term in _split_structured_retrieval_terms(raw_value):
            if term not in terms[normalized_key]:
                terms[normalized_key].append(term)
    return terms


def _selected_retrieval_structured_fact_slot_terms(
    query: str,
    structured_terms: dict[str, list[str]],
) -> list[str]:
    slot_terms = _selected_retrieval_dedupe_terms(
        [
            *(structured_terms.get("answer_fields") or []),
            *(structured_terms.get("evidence_facets") or []),
            *(structured_terms.get("requested_types") or []),
        ]
    )
    for match in _STRUCTURED_FACT_SLOT_REQUEST_PATTERN.findall(str(query or "")):
        slot_terms.extend(_split_structured_retrieval_terms(match))

    normalized_query = str(query or "")
    for keyword in _STRUCTURED_FACT_KNOWN_SLOT_KEYWORDS:
        if keyword.lower() in normalized_query.lower() and keyword not in slot_terms:
            slot_terms.append(keyword)

    generic_labels = {
        "字段",
        "取值",
        "值",
        "value",
        "values",
        "slot",
        "slots",
        "field",
        "fields",
        "信息",
        "内容",
    }
    filtered: list[str] = []
    known_slot_keywords_lower = {
        keyword.lower() for keyword in _STRUCTURED_FACT_KNOWN_SLOT_KEYWORDS
    }
    for term in slot_terms:
        for fragment in _STRUCTURED_FACT_SLOT_CONNECTOR_PATTERN.split(str(term or "").strip()):
            normalized = str(fragment or "").strip()
            normalized = re.sub(
                r"^(?:请|请从|从)?(?:定额)?\s*0?\d{4,6}\s*(?:中|里)?(?:提取|抽取|列出|给出|返回|输出)?\s*",
                "",
                normalized,
                flags=re.IGNORECASE,
            ).strip()
            normalized = re.sub(
                r"(?:的)?(?:取值|值|字段|信息|内容|是多少|是什么)$",
                "",
                normalized,
                flags=re.IGNORECASE,
            ).strip()
            normalized = re.sub(
                r"(?:和|及|与|以及|并且|and)\s*$",
                "",
                normalized,
                flags=re.IGNORECASE,
            ).strip()
            normalized = re.sub(r"^[=:：\-_/,\s]+|[=:：\-_/,\s]+$", "", normalized).strip()
            if not normalized:
                continue
            if (
                normalized.lower() == "other material cost"
                and "other material cost" in str(term or "").lower()
            ):
                normalized = "other material cost"
            if (
                normalized.lower() == "farmyard manure"
                and "farmyard manure" in str(term or "").lower()
            ):
                normalized = "farmyard manure"
            if (
                normalized.lower() == "labor"
                and "labor" in str(term or "").lower()
            ):
                normalized = "labor"
            if normalized == "拖拉机37w" and "拖拉机37w" in str(term or ""):
                normalized = "拖拉机37w"
            if (
                not normalized
                or len(normalized) <= 1
                or normalized.lower() in generic_labels
                or re.fullmatch(r"[=:/\-_.]+", normalized)
                or normalized.lower() in known_slot_keywords_lower
                and normalized not in _STRUCTURED_FACT_KNOWN_SLOT_KEYWORDS
            ):
                continue
            filtered.append(normalized)
    return _selected_retrieval_dedupe_terms(filtered)


def _selected_retrieval_structured_fact_plan(
    *,
    query: str,
    required_anchors: list[str],
    source_ids: list[str],
    active_source_scope: dict[str, Any] | None,
    structured_terms: dict[str, list[str]],
) -> dict[str, Any]:
    query_text = str(query or "").strip()
    normalized_anchor_values = _selected_retrieval_dedupe_terms(required_anchors)
    quota_context_signal = bool(
        re.search(r"(?:\bquota\b|定额|清单|表|row|行|列)", query_text, flags=re.IGNORECASE)
    )
    answer_slots = _selected_retrieval_structured_fact_slot_terms(query_text, structured_terms)
    answer_slot_terms_lower = {slot.lower() for slot in answer_slots}
    hard_anchors = _selected_retrieval_dedupe_terms(
        [
            anchor
            for anchor in normalized_anchor_values
            if anchor
            and anchor.lower() not in answer_slot_terms_lower
            and anchor.lower() not in _STRUCTURED_FACT_KNOWN_SLOT_KEYWORDS
            and not _STRUCTURED_FACT_NON_ANCHOR_REQUIRED_PATTERN.search(anchor)
        ]
    )

    standard_numbers = _selected_retrieval_dedupe_terms(
        [
            *(structured_terms.get("standard_no") or []),
            *re.findall(_STRUCTURED_FACT_STANDARD_NO_PATTERN, query_text),
            *[
                anchor
                for anchor in normalized_anchor_values
                if re.search(_STRUCTURED_FACT_STANDARD_NO_PATTERN, anchor)
            ],
        ]
    )
    quota_ids = _selected_retrieval_dedupe_terms(
        [
            *(structured_terms.get("quota_id") or []),
            *[
                str(match[0] if isinstance(match, tuple) else match or "").strip()
                for match in _STRUCTURED_FACT_QUOTA_ID_PATTERN.findall(query_text)
            ],
            *[
                anchor
                for anchor in normalized_anchor_values
                if quota_context_signal and re.fullmatch(r"0\d{4,6}|[1-9]\d{4,6}", anchor)
            ],
        ]
    )
    row_or_table_labels = _selected_retrieval_dedupe_terms(
        [
            *(structured_terms.get("table_label") or []),
            *(structured_terms.get("row_label") or []),
            *re.findall(_STRUCTURED_FACT_ROW_TABLE_LABEL_PATTERN, query_text),
        ]
    )
    document_numbers = _selected_retrieval_dedupe_terms(
        [
            *(structured_terms.get("document_no") or []),
            *[match for match in _STRUCTURED_FACT_DOCUMENT_NO_PATTERN.findall(query_text)],
            *[
                anchor
                for anchor in normalized_anchor_values
                if "号" in anchor and len(anchor) >= 4
            ],
        ]
    )
    quoted_terms = _selected_retrieval_dedupe_terms(
        re.findall(
            r"[\"“”'‘’《》「」『』]([^\"“”'‘’《》「」『』]{2,80})[\"“”'‘’《》「」『』]",
            query_text,
        )
    )
    units = _selected_retrieval_dedupe_terms(
        [
            *(structured_terms.get("unit_terms") or []),
            *re.findall(_STRUCTURED_FACT_UNIT_PATTERN, query_text),
            *[
                anchor
                for anchor in normalized_anchor_values
                if re.search(_STRUCTURED_FACT_UNIT_PATTERN, anchor)
            ],
        ]
    )

    for term_group in (
        standard_numbers,
        quota_ids,
        row_or_table_labels,
        document_numbers,
        quoted_terms,
        units,
    ):
        for term in term_group:
            if term and term.lower() not in answer_slot_terms_lower and term not in hard_anchors:
                hard_anchors.append(term)

    kv_pairs = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;\n]+)", query_text)
    planning_keys = {
        "document",
        "doc_id",
        "quota_id",
        "table_label",
        "row_label",
        "answer_fields",
        "unit_terms",
        "standard_no",
        "document_no",
        "evidence_facets",
        "topic_terms",
        "requested_types",
        "user_anchors",
        "date_basis",
        "type_basis",
        "topic_basis",
    }
    soft_context_terms = _selected_retrieval_dedupe_terms(
        [
            f"{key}={value.strip()}"
            for key, value in kv_pairs
            if key.strip().lower() not in planning_keys and value.strip()
        ]
    )
    soft_context_terms.extend(
        [
            term
            for term in structured_terms.get("topic_terms") or []
            if term not in hard_anchors and term not in soft_context_terms
        ]
    )
    soft_context_terms = _selected_retrieval_dedupe_terms(soft_context_terms)

    has_exact_anchor_signal = bool(
        standard_numbers
        or quota_ids
        or row_or_table_labels
        or document_numbers
        or quoted_terms
    )
    has_multi_field_signal = len(answer_slots) >= 2
    has_table_row_signal = bool(quota_ids or row_or_table_labels)
    has_standard_value_signal = bool(standard_numbers and units)
    if has_table_row_signal:
        for slot in answer_slots:
            if slot and slot not in hard_anchors:
                hard_anchors.append(slot)

    intent_labels: list[str] = []
    if has_standard_value_signal:
        intent_labels.append("standard_value")
    if has_exact_anchor_signal:
        intent_labels.append("exact_identifier")
    if has_table_row_signal:
        intent_labels.append("quota_table_row")
    if has_multi_field_signal:
        intent_labels.append("multi_field_slot_extraction")
    if intent_labels:
        intent_labels.insert(0, "structured_engineering_fact")

    if has_multi_field_signal:
        primary_intent = "multi_field_slot_extraction"
    elif has_table_row_signal:
        primary_intent = "quota_table_row"
    elif has_standard_value_signal:
        primary_intent = "standard_value"
    elif has_exact_anchor_signal:
        primary_intent = "exact_identifier"
    elif intent_labels:
        primary_intent = "structured_engineering_fact"
    else:
        primary_intent = "none"

    focused_queries: list[dict[str, Any]] = []
    focused_anchor_terms = _selected_retrieval_dedupe_terms(
        [*standard_numbers, *quota_ids, *document_numbers, *row_or_table_labels]
    )
    if not focused_anchor_terms and hard_anchors:
        focused_anchor_terms = hard_anchors[:2]

    if answer_slots:
        for slot in answer_slots:
            for anchor in focused_anchor_terms or [""]:
                focused_query = (
                    f"anchor={anchor}; answer_slot={slot}"
                    if anchor
                    else f"answer_slot={slot}"
                )
                focused_queries.append(
                    {
                        "anchor": anchor,
                        "answer_slot": slot,
                        "query": focused_query,
                    }
                )
    elif focused_anchor_terms:
        for anchor in focused_anchor_terms[:3]:
            focused_queries.append(
                {
                    "anchor": anchor,
                    "answer_slot": "",
                    "query": f"anchor={anchor}",
                }
            )

    source_scope = {
        "requested_source_ids": [value for value in source_ids if value],
        "active_source_scope_state": (
            str(active_source_scope.get("status") or "").strip().lower()
            if isinstance(active_source_scope, dict)
            else ""
        ),
    }
    return {
        "intent": primary_intent,
        "intent_labels": _selected_retrieval_dedupe_terms(intent_labels),
        "structured_fact_candidate": primary_intent != "none",
        "structured_routing_allowed": bool(has_exact_anchor_signal or has_multi_field_signal),
        "has_exact_anchor_signal": bool(has_exact_anchor_signal),
        "has_multi_field_signal": bool(has_multi_field_signal),
        "hard_anchors": hard_anchors,
        "answer_slots": answer_slots,
        "soft_context_terms": soft_context_terms,
        "anchor_groups": {
            "standard_numbers": standard_numbers,
            "quota_ids": quota_ids,
            "row_or_table_labels": row_or_table_labels,
            "document_numbers": document_numbers,
            "quoted_terms": quoted_terms,
            "units": units,
        },
        "focused_queries": focused_queries,
        "source_scope": source_scope,
    }


def _selected_retrieval_claim_flags(query: str) -> dict[str, bool]:
    normalized_query = str(query or "")
    return {
        "latest_or_recent": bool(
            _SELECTED_SOURCE_RECENCY_CLAIM_PATTERN.search(normalized_query)
        ),
        "year_or_range": bool(
            _SELECTED_SOURCE_YEAR_RANGE_CLAIM_PATTERN.search(normalized_query)
        ),
        "document_type": bool(
            _SELECTED_SOURCE_TYPE_CLAIM_PATTERN.search(normalized_query)
        ),
        "topic_scope": bool(
            _SELECTED_SOURCE_TOPIC_CLAIM_PATTERN.search(normalized_query)
        ),
    }


def _selected_retrieval_normalized_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _selected_retrieval_anchor_direct_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("km²", "km2").replace("m²", "m2")
    text = text.replace("·", ".")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _selected_retrieval_anchor_compact_text(value: Any) -> str:
    direct = _selected_retrieval_anchor_direct_text(value)
    return _STRUCTURED_FACT_ANCHOR_STRIP_PATTERN.sub("", direct)


def _selected_retrieval_standard_value_section_tokens(
    metadata: dict[str, Any],
) -> set[str]:
    tokens: set[str] = set()
    if not isinstance(metadata, dict):
        return tokens
    for key in _STRUCTURED_FACT_SECTION_METADATA_KEYS:
        raw_value = metadata.get(key)
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in values:
            token = _selected_retrieval_anchor_compact_text(value)
            if token:
                tokens.add(token)
    return tokens


def _selected_retrieval_standard_value_index(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _selected_retrieval_query_preserves_required_anchors(
    query: str,
    required_anchors: list[str],
) -> bool:
    if not required_anchors:
        return True

    direct_query = _selected_retrieval_anchor_direct_text(query)
    compact_query = _selected_retrieval_anchor_compact_text(query)
    for anchor in required_anchors:
        direct_anchor = _selected_retrieval_anchor_direct_text(anchor)
        compact_anchor = _selected_retrieval_anchor_compact_text(anchor)
        if direct_anchor and direct_anchor in direct_query:
            continue
        if compact_anchor and compact_anchor in compact_query:
            continue
        return False
    return True


def _selected_retrieval_structured_query_variants(
    *,
    query: str,
    structured_fact_plan: dict[str, Any] | None,
    required_anchors: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    base_query = str(query or "").strip()
    variants: list[str] = [base_query] if base_query else []
    rejected: list[dict[str, Any]] = []
    plan = structured_fact_plan if isinstance(structured_fact_plan, dict) else {}
    focused_queries = plan.get("focused_queries")
    if not isinstance(focused_queries, list):
        return variants, rejected

    for item in focused_queries:
        if not isinstance(item, dict):
            continue
        focused_query = str(item.get("query") or "").strip()
        if not focused_query:
            continue

        # Candidate variant intentionally starts focused; reject if it drops required anchors.
        if _selected_retrieval_query_preserves_required_anchors(
            focused_query,
            required_anchors,
        ):
            if focused_query not in variants:
                variants.append(focused_query)
        else:
            rejected.append(
                {
                    "query": focused_query,
                    "reason": "dropped_required_anchor",
                    "required_anchor_count": len(required_anchors),
                }
            )

        merged_query = f"{base_query}; {focused_query}".strip("; ").strip()
        if merged_query and _selected_retrieval_query_preserves_required_anchors(
            merged_query,
            required_anchors,
        ):
            if merged_query not in variants:
                variants.append(merged_query)

    return variants, rejected


def _selected_retrieval_find_exact_anchor_match(
    anchor: str,
    text: str,
) -> dict[str, Any] | None:
    direct_anchor = _selected_retrieval_anchor_direct_text(anchor)
    compact_anchor = _selected_retrieval_anchor_compact_text(anchor)
    direct_text = _selected_retrieval_anchor_direct_text(text)
    compact_text = _selected_retrieval_anchor_compact_text(text)

    if direct_anchor and direct_text:
        direct_index = direct_text.find(direct_anchor)
        if direct_index >= 0:
            return {
                "match_type": "direct",
                "normalized_text_span": [direct_index, direct_index + len(direct_anchor)],
                "span_basis": "normalized_direct_text",
            }
    if compact_anchor and compact_text:
        compact_index = compact_text.find(compact_anchor)
        if compact_index >= 0:
            return {
                "match_type": "normalized",
                "normalized_text_span": [
                    compact_index,
                    compact_index + len(compact_anchor),
                ],
                "span_basis": "normalized_compact_text",
            }
    return None


def _selected_retrieval_local_exact_lookup(
    *,
    sources: list[dict[str, Any]],
    anchors: list[str],
) -> dict[str, Any]:
    normalized_anchors = _selected_retrieval_dedupe_terms(anchors)
    if not normalized_anchors:
        return {
            "available": True,
            "candidates": [],
            "matched_sources": sources,
            "match_count": 0,
        }

    candidates: list[dict[str, Any]] = []
    matched_sources: list[dict[str, Any]] = []
    searchable_chunk_count = 0

    for source in sources or []:
        if not isinstance(source, dict):
            continue
        documents = source.get("document") if isinstance(source.get("document"), list) else []
        metadatas = source.get("metadata") if isinstance(source.get("metadata"), list) else []
        source_info = source.get("source") if isinstance(source.get("source"), dict) else {}
        source_id = str(source_info.get("id") or "")
        source_name = str(source_info.get("name") or "")
        source_hit = False

        for chunk_index, document in enumerate(documents):
            metadata = metadatas[chunk_index] if chunk_index < len(metadatas) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            text_parts = [
                str(document or ""),
                str(metadata.get("name") or ""),
                str(metadata.get("title") or ""),
                str(metadata.get("source") or ""),
                str(metadata.get("filename") or ""),
                str(source_info.get("name") or ""),
                str(source_info.get("title") or ""),
                str(source_info.get("id") or ""),
            ]
            combined_text = " ".join([part for part in text_parts if part]).strip()
            if not combined_text:
                continue
            searchable_chunk_count += 1

            for anchor in normalized_anchors:
                match = _selected_retrieval_find_exact_anchor_match(anchor, combined_text)
                if not match:
                    continue
                source_hit = True
                candidates.append(
                    {
                        "candidate_kind": "exact_anchor_hit",
                        "anchor": anchor,
                        "source_id": source_id,
                        "source_name": source_name,
                        "chunk_index": chunk_index,
                        "match_type": match.get("match_type"),
                        # Compatibility field: offsets are against normalized text, not
                        # raw provider document byte/character positions.
                        "match_span": match.get("normalized_text_span"),
                        "normalized_text_span": match.get("normalized_text_span"),
                        "span_basis": str(match.get("span_basis") or "normalized_text"),
                        "preliminary_support_state": "unsupported",
                    }
                )

        if source_hit:
            matched_sources.append(source)

    return {
        "available": searchable_chunk_count > 0,
        "candidates": candidates,
        "matched_sources": matched_sources,
        "match_count": len(candidates),
    }


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


def _selected_retrieval_normalize_identifier(value: Any) -> str:
    return str(value or "").strip().lower()


def _selected_retrieval_compact_diagnostic(
    *,
    classification: str,
    reason: str,
    outcome: str,
    tool_name: str,
    candidate_index: int = -1,
    chunk_total: int = 0,
    source_id: str = "",
    query: str = "",
    retrieval_round: Any = None,
    detail: Any = None,
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "kind": "retrieval_quality",
        "classification": str(classification or "diagnostics"),
        "reason": str(reason or "unknown"),
        "outcome": str(outcome or "diagnostics"),
        "tool_name": str(tool_name or ""),
        "candidate_index": int(candidate_index),
    }
    if chunk_total:
        diagnostic["chunk_total"] = int(chunk_total)
    if source_id:
        diagnostic["source_id"] = source_id
    if query:
        diagnostic["query"] = query
    if retrieval_round not in (None, "", [], {}):
        diagnostic["retrieval_round"] = retrieval_round
    if detail not in (None, "", [], {}):
        diagnostic["detail"] = detail
    return diagnostic


def _selected_retrieval_dedupe_diagnostics(
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in diagnostics or []:
        if not isinstance(item, dict):
            continue
        signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(item)
    return deduped


def _selected_retrieval_metadata_first_diagnostics(
    *,
    query: str,
    tool_name: str,
    retrieval_round: Any,
    routing_strategy: dict[str, Any] | None,
    selected_inventory_count: int,
    scoped_inventory_count: int,
    structured_terms: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    strategy = routing_strategy if isinstance(routing_strategy, dict) else {}
    if not strategy.get("metadata_first_intent"):
        return []

    diagnostics: list[dict[str, Any]] = []
    claims = _selected_retrieval_claim_flags(query)
    terms = (
        structured_terms
        if isinstance(structured_terms, dict)
        else _selected_retrieval_structured_terms(query)
    )
    has_date_basis_for_recency = bool(terms.get("date_basis"))
    has_date_basis_for_range = bool(
        terms.get("date_basis")
        or _SELECTED_SOURCE_YEAR_RANGE_CLAIM_PATTERN.search(str(query or ""))
    )
    has_type_basis = bool(
        terms.get("requested_types") or terms.get("type_basis") or terms.get("document")
    )
    has_topic_basis = bool(
        terms.get("topic_terms") or terms.get("topic_basis") or terms.get("document")
    )

    if selected_inventory_count <= 0 or scoped_inventory_count <= 0:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="no_evidence",
                reason="inventory_unavailable",
                outcome="blocked",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )

    if claims.get("latest_or_recent") and not has_date_basis_for_recency:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="missing_date_metadata",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="no_evidence",
                reason="unsupported_latest_or_recent_claim",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
    if claims.get("year_or_range") and not has_date_basis_for_range:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="missing_date_metadata",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="no_evidence",
                reason="unsupported_year_range_claim",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
    if claims.get("document_type") and not has_type_basis:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="missing_type_metadata",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="no_evidence",
                reason="unsupported_document_type_claim",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
    if claims.get("topic_scope") and not has_topic_basis:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="missing_topic_metadata",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="no_evidence",
                reason="unsupported_topic_scope_claim",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
    return _selected_retrieval_dedupe_diagnostics(diagnostics)


def _selected_retrieval_scope_identity_values(scope_source: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in (
        "id",
        "file_id",
        "fileId",
        "document_id",
        "documentId",
        "collection_name",
        "knowledge_id",
        "name",
        "title",
        "filename",
    ):
        normalized = _selected_retrieval_normalize_identifier(scope_source.get(key))
        if normalized:
            values.add(normalized)
    return values


def _selected_retrieval_source_identity_values(source: dict[str, Any]) -> set[str]:
    values = {
        _selected_retrieval_normalize_identifier(value)
        for value in _deepagent_source_identity_values(source)
        if _selected_retrieval_normalize_identifier(value)
    }
    source_info = source.get("source")
    if isinstance(source_info, dict):
        for key in (
            "id",
            "file_id",
            "fileId",
            "document_id",
            "documentId",
            "collection_name",
            "knowledge_id",
            "name",
            "title",
            "filename",
            "source",
            "url",
        ):
            normalized = _selected_retrieval_normalize_identifier(source_info.get(key))
            if normalized:
                values.add(normalized)

    metadatas = source.get("metadata")
    for metadata in metadatas if isinstance(metadatas, list) else []:
        if not isinstance(metadata, dict):
            continue
        for key in (
            "file_id",
            "fileId",
            "document_id",
            "documentId",
            "knowledge_id",
            "collection_name",
            "name",
            "title",
            "filename",
            "source",
            "url",
        ):
            normalized = _selected_retrieval_normalize_identifier(metadata.get(key))
            if normalized:
                values.add(normalized)
    return values


def _selected_retrieval_scope_candidate_filter(
    candidates: list[dict],
    metadata: dict | None,
    *,
    query: str,
    retrieval_round: Any,
    tool_name: str,
) -> tuple[list[dict], list[dict[str, Any]]]:
    metadata = metadata if isinstance(metadata, dict) else {}
    scope = metadata.get("active_source_scope")
    scope = scope if isinstance(scope, dict) else {}
    if not scope:
        return candidates, []

    status = str(scope.get("status") or "").strip().lower()
    source_set_mode = str(scope.get("source_set_mode") or "").strip().lower()
    reason = str(
        scope.get("reason")
        or scope.get("ambiguity_reason")
        or scope.get("expiration_reason")
        or "source_scope_mismatch"
    ).strip()
    blocked = status in {"ambiguous", "expired"} or (
        source_set_mode == "none"
        and reason in {"ambiguous_retrieval_scope", "expired_or_conflicting"}
    )
    if blocked:
        return [], [
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason=reason or "source_scope_blocked",
                outcome="blocked",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        ]

    scope_sources = [item for item in scope.get("sources") or [] if isinstance(item, dict)]
    if not scope_sources:
        return candidates, []

    allowed_values: set[str] = set()
    for source in scope_sources:
        allowed_values.update(_selected_retrieval_scope_identity_values(source))

    if not allowed_values:
        return candidates, []

    filtered = [
        candidate
        for candidate in candidates
        if _selected_retrieval_source_identity_values(candidate).intersection(
            allowed_values
        )
    ]
    if filtered:
        return filtered, []

    return [], [
        _selected_retrieval_compact_diagnostic(
            classification="diagnostics",
            reason="source_scope_mismatch",
            outcome="unauthorized",
            tool_name=tool_name,
            query=query,
            retrieval_round=retrieval_round,
        )
    ]


def _selected_retrieval_original_query(metadata: dict | None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in (
        "original_query",
        "initial_user_query",
        "user_prompt",
        "user_query",
    ):
        candidate = str(metadata.get(key) or "").strip()
        if candidate:
            return candidate
    return ""


def _selected_retrieval_normalize_strategy(
    *,
    evidence_need: str,
    query: str,
    required_anchors: list[str],
    source_ids: list[str],
    active_source_scope: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized_evidence_need = str(evidence_need or "").strip().lower()
    reason_codes: list[str] = []
    query_text = str(query or "").strip()
    scoped_source_ids = [
        str(value or "").strip()
        for value in source_ids or []
        if str(value or "").strip()
    ]
    normalized_required_anchors = [
        str(value or "").strip()
        for value in required_anchors or []
        if str(value or "").strip()
    ]
    structured_terms = _selected_retrieval_structured_terms(query_text)
    structured_fact_plan = _selected_retrieval_structured_fact_plan(
        query=query_text,
        required_anchors=normalized_required_anchors,
        source_ids=scoped_source_ids,
        active_source_scope=active_source_scope,
        structured_terms=structured_terms,
    )
    structured_query_variants, rejected_query_variants = _selected_retrieval_structured_query_variants(
        query=query_text,
        structured_fact_plan=structured_fact_plan,
        required_anchors=normalized_required_anchors,
    )

    metadata_first_intent = False
    if normalized_evidence_need:
        reason_codes.append(f"evidence_need:{normalized_evidence_need}")
    if normalized_evidence_need in _METADATA_FIRST_SELECTED_SOURCE_EVIDENCE_NEEDS:
        metadata_first_intent = True
    elif normalized_evidence_need.startswith("metadata_first"):
        metadata_first_intent = True

    weak_metadata_hint = bool(
        query_text and _SELECTED_SOURCE_WEAK_METADATA_FIRST_QUERY_HINT_PATTERN.search(query_text)
    )
    if weak_metadata_hint:
        reason_codes.append("weak_query_metadata_first_hint")
    weak_narrow_hint = bool(
        query_text and _SELECTED_SOURCE_WEAK_NARROW_QUERY_HINT_PATTERN.search(query_text)
    )
    if weak_narrow_hint:
        reason_codes.append("weak_query_narrow_detail_hint")
    if weak_metadata_hint and not weak_narrow_hint:
        metadata_first_intent = True

    has_structured_query_facets = bool(
        query_text and _SELECTED_SOURCE_METADATA_FIRST_QUERY_FACET_PATTERN.search(query_text)
    )
    if has_structured_query_facets:
        reason_codes.append("structured_query_facets")
    if structured_fact_plan.get("structured_fact_candidate"):
        reason_codes.append("structured_fact_candidate")
    if structured_fact_plan.get("has_exact_anchor_signal"):
        reason_codes.append("structured_fact_exact_anchor_signal")
    if structured_fact_plan.get("has_multi_field_signal"):
        reason_codes.append("structured_fact_multi_field_signal")
    if (
        structured_fact_plan.get("structured_fact_candidate")
        and not structured_fact_plan.get("structured_routing_allowed")
    ):
        reason_codes.append("structured_fact_requires_exact_anchor_or_multi_field")
    if rejected_query_variants:
        reason_codes.append("structured_fact_rejected_anchor_dropping_query_variant")

    has_bounded_source_ids = bool(scoped_source_ids)
    has_bounded_anchors = bool(normalized_required_anchors)
    targeted_context_present = has_bounded_source_ids and has_bounded_anchors
    if targeted_context_present:
        reason_codes.append("bounded_targeted_context_present")

    targeted_context_bounded = targeted_context_present and has_structured_query_facets
    if targeted_context_bounded:
        reason_codes.append("bounded_targeted_context_proven")

    retrieval_strategy = "semantic_chunks"
    semantic_chunk_lookup_ok = True
    if metadata_first_intent:
        retrieval_strategy = "metadata_first_then_targeted_chunks"
        semantic_chunk_lookup_ok = targeted_context_bounded
        if not semantic_chunk_lookup_ok:
            reason_codes.append("metadata_first_requires_bounded_targeted_context")
    elif normalized_evidence_need in _SEMANTIC_DETAIL_SELECTED_SOURCE_EVIDENCE_NEEDS:
        reason_codes.append("semantic_detail_evidence_need")
    elif not normalized_evidence_need:
        reason_codes.append("default_semantic_strategy")

    scope_status = (
        str(active_source_scope.get("status") or "").strip().lower()
        if isinstance(active_source_scope, dict)
        else ""
    )
    if scope_status:
        reason_codes.append(f"active_scope_status:{scope_status}")

    return {
        "normalized_evidence_need": normalized_evidence_need,
        "retrieval_strategy": retrieval_strategy,
        "semantic_chunk_lookup_ok": bool(semantic_chunk_lookup_ok),
        "metadata_first_intent": bool(metadata_first_intent),
        "targeted_context_present": bool(targeted_context_present),
        "targeted_context_bounded": bool(targeted_context_bounded),
        "structured_fact_intent": str(structured_fact_plan.get("intent") or "none"),
        "structured_routing_allowed": bool(
            structured_fact_plan.get("structured_routing_allowed")
        ),
        "structured_fact_plan": structured_fact_plan,
        "structured_query_variants": structured_query_variants,
        "structured_rejected_query_variants": rejected_query_variants,
        "reason_codes": list(dict.fromkeys(reason_codes)),
    }


def _selected_retrieval_with_strategy_metadata(
    strategy_used: dict[str, Any],
    routing_strategy: dict[str, Any],
) -> dict[str, Any]:
    strategy = (
        copy.deepcopy(strategy_used)
        if isinstance(strategy_used, dict)
        else {}
    )
    if not isinstance(routing_strategy, dict):
        return strategy
    strategy["retrieval_strategy"] = str(
        routing_strategy.get("retrieval_strategy") or "semantic_chunks"
    )
    strategy["semantic_chunk_lookup_ok"] = bool(
        routing_strategy.get("semantic_chunk_lookup_ok")
    )
    strategy["reason_codes"] = [
        str(value)
        for value in routing_strategy.get("reason_codes") or []
        if str(value).strip()
    ]
    strategy["structured_fact_intent"] = str(
        routing_strategy.get("structured_fact_intent") or "none"
    )
    strategy["structured_routing_allowed"] = bool(
        routing_strategy.get("structured_routing_allowed")
    )
    strategy["structured_query_variants"] = [
        str(value)
        for value in routing_strategy.get("structured_query_variants") or []
        if str(value).strip()
    ]
    strategy["structured_rejected_query_variants"] = [
        value
        for value in routing_strategy.get("structured_rejected_query_variants") or []
        if isinstance(value, dict)
    ]
    structured_fact_plan = routing_strategy.get("structured_fact_plan")
    if isinstance(structured_fact_plan, dict):
        strategy["structured_fact_plan"] = copy.deepcopy(structured_fact_plan)
    return strategy


def _selected_retrieval_descriptor(tool_name: str) -> dict[str, Any]:
    descriptor = SELECTED_SOURCE_RETRIEVAL_DESCRIPTOR_BY_TOOL.get(str(tool_name or "").strip())
    if not isinstance(descriptor, dict):
        return {}
    return copy.deepcopy(descriptor)


def _selected_retrieval_timeout_seconds(
    timeout_seconds: Any,
    *,
    request: Any = None,
) -> float:
    configured_timeout = None
    if request is not None:
        try:
            configured_timeout = getattr(
                request.app.state.config,
                "SELECTED_SOURCE_RETRIEVAL_TIMEOUT_SECONDS",
                None,
            )
        except Exception:
            configured_timeout = None
    candidate = timeout_seconds
    if candidate in (None, "", [], {}):
        candidate = configured_timeout
    try:
        normalized = float(candidate)
    except Exception:
        normalized = SELECTED_SOURCE_RETRIEVAL_DEFAULT_TIMEOUT_SECONDS
    if normalized <= 0:
        normalized = SELECTED_SOURCE_RETRIEVAL_DEFAULT_TIMEOUT_SECONDS
    return min(normalized, SELECTED_SOURCE_RETRIEVAL_MAX_TIMEOUT_SECONDS)


def _selected_retrieval_effective_query(
    query: str,
    *,
    retrieval_round: Any,
    metadata: dict | None,
    explicit_original_query: str = "",
) -> tuple[str, str, bool]:
    normalized_query = str(query or "").strip()
    original_query = str(explicit_original_query or "").strip() or _selected_retrieval_original_query(metadata)
    try:
        normalized_round = int(retrieval_round)
    except Exception:
        normalized_round = 1
    enforce_original_query = bool(original_query) and normalized_round <= 1
    effective_query = original_query if enforce_original_query else normalized_query
    return effective_query, original_query, enforce_original_query


def _selected_retrieval_extract_confident_required_anchors(
    query: str,
    *,
    metadata: dict | None,
    explicit_required_anchors: list[str] | None = None,
    candidates: list[dict] | None = None,
) -> list[str]:
    anchors: list[str] = []
    normalized_query = str(query or "").strip()

    def add_anchor(value: Any) -> None:
        candidate = str(value or "").strip()
        if not candidate:
            return
        if candidate not in anchors:
            anchors.append(candidate)

    for item in explicit_required_anchors or []:
        add_anchor(item)

    # When callers provide explicit required anchors, treat them as the strict
    # anchor set for this turn and avoid widening with incidental prompt terms.
    if anchors:
        return anchors

    if not normalized_query:
        return anchors

    for match in re.findall(
        r"[\"“”'‘’《》「」『』]([^\"“”'‘’《》「」『』]{2,80})[\"“”'‘’《》「」『』]",
        normalized_query,
    ):
        add_anchor(match)

    for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9._:/-]{3,}", normalized_query):
        if any(char.isdigit() for char in term) or any(
            char in term for char in ("_", "-", "/", ".")
        ):
            add_anchor(term)

    for pattern in (
        r"第[\dA-Za-z一二三四五六七八九十百千零〇.．-]{1,24}[章节条款项]",
        r"\d{4}[-/年.]\d{1,2}(?:[-/月.]\d{1,2})?",
    ):
        for term in re.findall(pattern, normalized_query):
            add_anchor(term)

    metadata = metadata if isinstance(metadata, dict) else {}
    for key in ("normalized_required_anchors", "required_anchors", "explicit_user_terms"):
        value = metadata.get(key)
        if isinstance(value, str):
            add_anchor(value)
            continue
        if isinstance(value, list):
            for item in value:
                add_anchor(item)

    normalized_query_text = _selected_retrieval_normalized_text(normalized_query)
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        for key in ("name", "title", "filename"):
            label = str(candidate.get(key) or "").strip()
            if not label or len(label) < 4:
                continue
            normalized_label = _selected_retrieval_normalized_text(label)
            if normalized_label and normalized_label in normalized_query_text:
                add_anchor(label)

    return anchors


def _selected_retrieval_anchor_policy(required_anchors: list[str]) -> str:
    if required_anchors:
        return "strict_confident_required_anchors_only"
    return "query_correlation_only"


def _selected_retrieval_provenance_ok(
    source: dict[str, Any],
    metadata: dict[str, Any],
) -> bool:
    source_info = source.get("source") if isinstance(source.get("source"), dict) else {}
    for container in (metadata, source_info, source):
        if not isinstance(container, dict):
            continue
        for key in (
            "file_id",
            "fileId",
            "document_id",
            "documentId",
            "knowledge_id",
            "collection_name",
            "name",
            "title",
            "source",
            "url",
            "id",
        ):
            if str(container.get(key) or "").strip():
                return True
    return False


def _selected_retrieval_metadata_denied(
    source: dict[str, Any],
    metadata: dict[str, Any],
) -> bool:
    def is_falsey(value: Any) -> bool:
        if isinstance(value, bool):
            return value is False
        if isinstance(value, (int, float)):
            return value == 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized in {"false", "0", "denied", "forbidden", "unauthorized"}
        return False

    def is_truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value is True
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized in {"true", "1", "yes", "denied", "forbidden", "unauthorized"}
        return False

    denied_markers = (
        metadata.get("unauthorized"),
        metadata.get("permission_denied"),
        source.get("unauthorized"),
        source.get("permission_denied"),
    )
    allowed_markers = (
        metadata.get("authorized"),
        metadata.get("has_access"),
        metadata.get("permission_allowed"),
        source.get("authorized"),
        source.get("has_access"),
        source.get("permission_allowed"),
    )
    return any(is_truthy(value) for value in denied_markers) or any(
        is_falsey(value) for value in allowed_markers
    )


def _selected_retrieval_anchor_match(
    anchor: str,
    *,
    source: dict[str, Any],
    document: Any,
    metadata: dict[str, Any],
) -> bool:
    normalized_anchor = _selected_retrieval_normalized_text(anchor)
    if not normalized_anchor:
        return True
    metadata_text = " ".join(
        [
            str(metadata.get("name") or ""),
            str(metadata.get("title") or ""),
            str(metadata.get("source") or ""),
            str(metadata.get("filename") or ""),
        ]
    )
    source_info = source.get("source") if isinstance(source.get("source"), dict) else {}
    source_text = " ".join(
        [
            str(source_info.get("name") or ""),
            str(source_info.get("title") or ""),
            str(source_info.get("id") or ""),
            str(source_info.get("source") or ""),
        ]
    )
    combined = f"{document or ''}\n{metadata_text}\n{source_text}"
    if normalized_anchor in _selected_retrieval_normalized_text(combined):
        return True
    return _selected_retrieval_find_exact_anchor_match(anchor, combined) is not None


def _filter_selected_retrieval_sources_by_query(
    sources: list[dict],
    *,
    query: str,
    required_anchors: list[str],
    required_facets: list[str],
    metadata_first_intent: bool,
    allow_anchor_bundle_join: bool,
    allowed_identity_values: set[str],
    tool_name: str,
    retrieval_round: Any,
) -> tuple[list[dict], list[dict[str, Any]]]:
    filtered_sources: list[dict] = []
    diagnostics: list[dict[str, Any]] = []

    for source_index, source in enumerate(sources or []):
        if not isinstance(source, dict):
            diagnostics.append(
                _selected_retrieval_compact_diagnostic(
                    classification="diagnostics",
                    reason="malformed_candidate",
                    outcome="malformed",
                    tool_name=tool_name,
                    candidate_index=source_index,
                    query=query,
                    retrieval_round=retrieval_round,
                )
            )
            continue

        source_values = _selected_retrieval_source_identity_values(source)
        source_id = next(iter(source_values), "")
        if allowed_identity_values and not source_values.intersection(
            allowed_identity_values
        ):
            diagnostics.append(
                _selected_retrieval_compact_diagnostic(
                    classification="diagnostics",
                    reason="source_scope_mismatch",
                    outcome="unauthorized",
                    tool_name=tool_name,
                    candidate_index=source_index,
                    source_id=source_id,
                    query=query,
                    retrieval_round=retrieval_round,
                )
            )
            continue

        documents = source.get("document") if isinstance(source.get("document"), list) else []
        metadatas = source.get("metadata") if isinstance(source.get("metadata"), list) else []
        if not documents:
            diagnostics.append(
                _selected_retrieval_compact_diagnostic(
                    classification="no_evidence",
                    reason="missing_document_body",
                    outcome="empty",
                    tool_name=tool_name,
                    candidate_index=source_index,
                    source_id=source_id,
                    query=query,
                    retrieval_round=retrieval_round,
                )
            )
            continue

        accepted_indexes: list[int] = []
        for index, document in enumerate(documents):
            if not str(document or "").strip():
                continue
            metadata = metadatas[index] if index < len(metadatas) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            if _selected_retrieval_metadata_denied(source, metadata):
                diagnostics.append(
                    _selected_retrieval_compact_diagnostic(
                        classification="no_evidence",
                        reason="permission_denied",
                        outcome="denied",
                        tool_name=tool_name,
                        candidate_index=source_index,
                        chunk_total=len(documents),
                        source_id=source_id,
                        query=query,
                        retrieval_round=retrieval_round,
                    )
                )
                continue
            if not _selected_retrieval_provenance_ok(source, metadata):
                diagnostics.append(
                    _selected_retrieval_compact_diagnostic(
                        classification="diagnostics",
                        reason="missing_provenance",
                        outcome="malformed",
                        tool_name=tool_name,
                        candidate_index=source_index,
                        chunk_total=len(documents),
                        source_id=source_id,
                        query=query,
                        retrieval_round=retrieval_round,
                    )
                )
                continue
            query_correlation_match = _selected_retrieval_chunk_matches_query(
                query, document
            )
            if (
                allow_anchor_bundle_join
                and not query_correlation_match
                and any(
                    _selected_retrieval_find_exact_anchor_match(anchor, str(document or ""))
                    is not None
                    for anchor in required_anchors
                )
            ):
                query_correlation_match = True
            if (required_anchors or metadata_first_intent) and not query_correlation_match:
                diagnostics.append(
                    _selected_retrieval_compact_diagnostic(
                        classification="no_evidence",
                        reason=(
                            "weak_targeted_chunk_evidence"
                            if metadata_first_intent
                            else "weak_or_indirect_evidence"
                        ),
                        outcome="weak_evidence",
                        tool_name=tool_name,
                        candidate_index=source_index,
                        chunk_total=len(documents),
                        source_id=source_id,
                        query=query,
                        retrieval_round=retrieval_round,
                    )
                )
                continue
            anchor_matches = (
                [
                    _selected_retrieval_anchor_match(
                        anchor,
                        source=source,
                        document=document,
                        metadata=metadata,
                    )
                    for anchor in required_anchors
                ]
                if required_anchors
                else []
            )
            if required_anchors and (
                not any(anchor_matches)
                if allow_anchor_bundle_join
                else not all(anchor_matches)
            ):
                diagnostics.append(
                    _selected_retrieval_compact_diagnostic(
                        classification="no_evidence",
                        reason="off_anchor_evidence",
                        outcome="low_relevance",
                        tool_name=tool_name,
                        candidate_index=source_index,
                        chunk_total=len(documents),
                        source_id=source_id,
                        query=query,
                        retrieval_round=retrieval_round,
                        detail={"required_anchor_count": len(required_anchors)},
                    )
                )
                continue
            if required_facets and not any(
                _selected_retrieval_anchor_match(
                    facet,
                    source=source,
                    document=document,
                    metadata=metadata,
                )
                for facet in required_facets
            ):
                diagnostics.append(
                    _selected_retrieval_compact_diagnostic(
                        classification="no_evidence",
                        reason="off_facet_evidence",
                        outcome="low_relevance",
                        tool_name=tool_name,
                        candidate_index=source_index,
                        chunk_total=len(documents),
                        source_id=source_id,
                        query=query,
                        retrieval_round=retrieval_round,
                        detail={"required_facet_count": len(required_facets)},
                    )
                )
                continue
            accepted_indexes.append(index)

        if accepted_indexes and allow_anchor_bundle_join:
            expanded_indexes = set(accepted_indexes)
            for index, document in enumerate(documents):
                if any(
                    _selected_retrieval_find_exact_anchor_match(anchor, str(document or ""))
                    is not None
                    for anchor in required_anchors
                ):
                    expanded_indexes.add(index)
            for index in list(accepted_indexes):
                if index - 1 >= 0:
                    expanded_indexes.add(index - 1)
                if index + 1 < len(documents):
                    expanded_indexes.add(index + 1)
            accepted_indexes = sorted(expanded_indexes)

        if not accepted_indexes:
            diagnostics.append(
                _selected_retrieval_compact_diagnostic(
                    classification="no_evidence",
                    reason="no_injectable_evidence",
                    outcome="empty",
                    tool_name=tool_name,
                    candidate_index=source_index,
                    chunk_total=len(documents),
                    source_id=source_id,
                    query=query,
                    retrieval_round=retrieval_round,
                )
            )
            continue

        filtered = copy.deepcopy(source)
        for key, value in source.items():
            if isinstance(value, list) and len(value) == len(documents):
                filtered[key] = [value[index] for index in accepted_indexes]
        filtered_sources.append(filtered)

    return filtered_sources, _selected_retrieval_dedupe_diagnostics(diagnostics)


async def _selected_retrieval_run_local_exact_lookup(
    *,
    get_sources_from_items: Callable[..., Awaitable[list[dict[str, Any]]]],
    request: Any,
    user_model: Any,
    candidates: list[dict[str, Any]],
    queries: list[str],
    anchors: list[str],
    timeout_seconds: float,
    tool_name: str,
    query: str,
    retrieval_round: Any,
    allowed_identity_values: set[str] | None = None,
) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    lookup_sources: list[dict[str, Any]] = []
    exact_candidates: list[dict[str, Any]] = []

    exact_items = [{**candidate, "context": "full"} for candidate in candidates]
    try:
        provider_call = get_sources_from_items(
            request=request,
            items=exact_items,
            queries=queries,
            embedding_function=lambda text, prefix: request.app.state.EMBEDDING_FUNCTION(
                text, prefix=prefix, user=user_model
            ),
            k=1,
            reranking_function=None,
            k_reranker=request.app.state.config.TOP_K_RERANKER,
            r=request.app.state.config.RELEVANCE_THRESHOLD,
            hybrid_bm25_weight=request.app.state.config.HYBRID_BM25_WEIGHT,
            hybrid_search=False,
            full_context=True,
            user=user_model,
        )
        lookup_sources = await asyncio.wait_for(
            provider_call,
            timeout=timeout_seconds,
        )
    except Exception:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="exact_anchor_lookup_unavailable",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
                detail={"cause": "local_source_enumeration_failed"},
            )
        )
        return {
            "sources": [],
            "exact_candidates": [],
            "diagnostics": diagnostics,
            "used": False,
        }

    if not lookup_sources:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="exact_anchor_lookup_unavailable",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
                detail={"cause": "no_local_source_rows"},
            )
        )
        return {
            "sources": [],
            "exact_candidates": [],
            "diagnostics": diagnostics,
            "used": False,
        }

    scoped_lookup_sources: list[dict[str, Any]] = []
    allowed_identity_values = allowed_identity_values or set()
    for source_index, source in enumerate(lookup_sources):
        if not isinstance(source, dict):
            continue
        source_values = _selected_retrieval_source_identity_values(source)
        if allowed_identity_values and not source_values.intersection(allowed_identity_values):
            diagnostics.append(
                _selected_retrieval_compact_diagnostic(
                    classification="diagnostics",
                    reason="source_scope_mismatch",
                    outcome="unauthorized",
                    tool_name=tool_name,
                    candidate_index=source_index,
                    query=query,
                    retrieval_round=retrieval_round,
                )
            )
            continue
        scoped_lookup_sources.append(source)

    lookup = _selected_retrieval_local_exact_lookup(
        sources=scoped_lookup_sources,
        anchors=anchors,
    )
    exact_candidates = [
        candidate
        for candidate in lookup.get("candidates") or []
        if isinstance(candidate, dict)
    ]
    if not lookup.get("available"):
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="exact_anchor_lookup_unavailable",
                outcome="unsupported",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
                detail={"cause": "no_searchable_local_text"},
            )
        )

    matched_sources = lookup.get("matched_sources")
    matched_sources = (
        [item for item in matched_sources if isinstance(item, dict)]
        if isinstance(matched_sources, list)
        else []
    )
    return {
        "sources": matched_sources,
        "exact_candidates": exact_candidates,
        "diagnostics": diagnostics,
        "used": bool(matched_sources),
    }


def _selected_retrieval_with_exact_candidates(
    strategy_used: dict[str, Any],
    exact_candidates: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    strategy = copy.deepcopy(strategy_used) if isinstance(strategy_used, dict) else {}
    candidates = [
        item
        for item in exact_candidates or []
        if isinstance(item, dict)
    ]
    if not candidates:
        return strategy

    strategy["exact_anchor_candidates"] = candidates
    strategy_plan = (
        strategy.get("structured_fact_plan")
        if isinstance(strategy.get("structured_fact_plan"), dict)
        else None
    )
    if isinstance(strategy_plan, dict):
        strategy_plan["exact_anchor_candidates"] = candidates
        strategy_plan["exact_anchor_lookup_used"] = True
    return strategy


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


def _selected_retrieval_candidate_denied(candidate: dict[str, Any]) -> bool:
    denied_values = (
        candidate.get("unauthorized"),
        candidate.get("permission_denied"),
        candidate.get("denied"),
    )
    for value in denied_values:
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str) and value.strip().lower() in {
            "true",
            "1",
            "yes",
            "denied",
            "unauthorized",
            "forbidden",
        }:
            return True
    return False


def _selected_retrieval_filter_authorized_candidates(
    candidates: list[dict[str, Any]],
    *,
    query: str,
    retrieval_round: Any,
    tool_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    allowed: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        if _selected_retrieval_candidate_denied(candidate):
            source_values = _deepagent_source_identity_values(candidate)
            diagnostics.append(
                _selected_retrieval_compact_diagnostic(
                    classification="no_evidence",
                    reason="source_not_authorized",
                    outcome="denied",
                    tool_name=tool_name,
                    candidate_index=index,
                    source_id=next(iter(source_values), ""),
                    query=query,
                    retrieval_round=retrieval_round,
                )
            )
            continue
        allowed.append(candidate)
    return allowed, diagnostics


def _selected_retrieval_strategy_used(
    *,
    descriptor: dict[str, Any],
    normalized_required_anchors: list[str],
    anchor_policy: str,
    evidence_need: str,
    original_query: str,
    executed_query: str,
    retrieval_round: Any,
    query_enforced_from_original: bool,
) -> dict[str, Any]:
    return {
        "source_lane": "selected_source",
        "tool_name": descriptor.get("tool_name") or "",
        "full_context": bool(descriptor.get("full_context")),
        "evidence_need": str(
            evidence_need or descriptor.get("default_evidence_need") or "balanced"
        ),
        "anchor_policy": str(anchor_policy or "query_correlation_only"),
        "normalized_required_anchors": list(normalized_required_anchors or []),
        "original_query": str(original_query or ""),
        "executed_query": str(executed_query or ""),
        "retrieval_round": retrieval_round,
        "query_enforced_from_original": bool(query_enforced_from_original),
    }


def _selected_retrieval_authorization_context(
    *,
    selected_inventory_count: int,
    scoped_inventory_count: int,
    requested_source_ids: list[str],
    active_source_scope: Any,
) -> dict[str, Any]:
    scope = active_source_scope if isinstance(active_source_scope, dict) else {}
    return {
        "selected_inventory_count": int(max(selected_inventory_count, 0)),
        "scoped_inventory_count": int(max(scoped_inventory_count, 0)),
        "requested_source_ids": [str(value) for value in requested_source_ids if str(value).strip()],
        "active_source_scope_state": str(scope.get("status") or "none"),
    }


def _selected_retrieval_retry_policy(timeout_seconds: float) -> dict[str, Any]:
    return {
        "max_retries": 0,
        "retries_attempted": 0,
        "retry_allowed": False,
        "timeout_seconds": float(timeout_seconds),
    }


def _selected_retrieval_compact_budget_provenance(
    context_budget: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(context_budget, dict) or not context_budget:
        return {}

    budget = context_budget.get("budget")
    if not isinstance(budget, dict):
        budget = {}

    compact = {
        "requested_budget_tokens": int(
            context_budget.get("requested_budget_tokens") or 0
        ),
        "estimated_used_tokens": int(context_budget.get("estimated_used_tokens") or 0),
        "accepted_output_count": int(context_budget.get("accepted_output_count") or 0),
        "reference_count": int(context_budget.get("reference_count") or 0),
        "candidate_accepted_bundle_count": int(
            context_budget.get("candidate_accepted_bundle_count") or 0
        ),
        "truncated_accepted_bundle_count": int(
            context_budget.get("truncated_accepted_bundle_count") or 0
        ),
        "omitted_accepted_bundle_count": int(
            context_budget.get("omitted_accepted_bundle_count") or 0
        ),
        "truncated": bool(context_budget.get("truncated")),
        "policy": str(budget.get("policy") or "").strip(),
        "budget_policy": str(budget.get("budget_policy") or "").strip(),
        "return_policy": str(budget.get("return_policy") or "").strip(),
        "ranking_policy": str(budget.get("ranking_policy") or "").strip(),
        "engine_version": str(budget.get("engine_version") or "").strip(),
        "authorization_generation": str(
            budget.get("authorization_generation") or ""
        ).strip(),
        "cache_eligible": bool(budget.get("cache_eligible"))
        if "cache_eligible" in budget
        else False,
        "cache_miss_reason": str(budget.get("cache_miss_reason") or "").strip(),
        "cache_missing_dimensions": [
            str(item)
            for item in (budget.get("cache_missing_dimensions") or [])
            if str(item).strip()
        ],
        "source_card_count": int(budget.get("source_card_count") or 0),
        "ordinary_prompt_context_count": int(
            budget.get("ordinary_prompt_context_count") or 0
        ),
    }
    return _selected_retrieval_safe_contract_value(compact)


def _selected_retrieval_worker_contract_lanes(
    response: dict[str, Any] | None,
    *,
    tool_name: str,
    normalized_timeout_seconds: float,
) -> dict[str, Any]:
    payload = copy.deepcopy(response) if isinstance(response, dict) else {}
    for key in (
        "provider_payload",
        "provider_response",
        "raw_payload",
        "raw_response",
        "raw_results",
        "inventory_rows",
        "inventory_documents",
    ):
        payload.pop(key, None)

    normalized_tool_name = str(payload.get("tool_name") or tool_name or "").strip()
    if normalized_tool_name:
        payload["tool_name"] = normalized_tool_name

    status = str(payload.get("status") or "").strip() or "error"
    payload["status"] = status

    canonical_references = payload.get("canonical_references")
    if not isinstance(canonical_references, list):
        canonical_references = []
    canonical_references = [item for item in canonical_references if isinstance(item, dict)]
    payload["canonical_references"] = canonical_references
    payload["references"] = copy.deepcopy(canonical_references)

    accepted_outputs = payload.get("accepted_outputs")
    if not isinstance(accepted_outputs, list):
        accepted_outputs = []
    payload["accepted_outputs"] = [
        item for item in accepted_outputs if isinstance(item, dict)
    ]

    retrieval_diagnostics = payload.get("retrieval_diagnostics")
    if not isinstance(retrieval_diagnostics, list):
        retrieval_diagnostics = []
    retrieval_diagnostics = [
        item for item in retrieval_diagnostics if isinstance(item, dict)
    ]
    payload["retrieval_diagnostics"] = retrieval_diagnostics
    payload["diagnostics"] = copy.deepcopy(retrieval_diagnostics)

    authorization_context = payload.get("authorization_context")
    if not isinstance(authorization_context, dict):
        authorization_context = {}
    payload["authorization_context"] = authorization_context

    default_retry_policy = _selected_retrieval_retry_policy(normalized_timeout_seconds)
    retry_policy = payload.get("retry_policy")
    if not isinstance(retry_policy, dict):
        retry_policy = copy.deepcopy(default_retry_policy)
    else:
        retry_policy = copy.deepcopy(retry_policy)
        for key, value in default_retry_policy.items():
            retry_policy.setdefault(key, value)
    payload["retry_policy"] = retry_policy

    terminal_reason = (
        str(payload.get("terminal_reason") or "").strip()
        or str(payload.get("code") or "").strip()
        or status
    )
    payload["terminal_reason"] = terminal_reason

    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    provenance = copy.deepcopy(provenance)
    strategy_used = payload.get("strategy_used")
    if isinstance(strategy_used, dict) and strategy_used:
        provenance.setdefault("strategy_used", copy.deepcopy(strategy_used))
    context_budget = payload.get("context_budget")
    if isinstance(context_budget, dict) and context_budget:
        provenance.setdefault("context_budget", copy.deepcopy(context_budget))
    for key in (
        "query",
        "original_query",
        "retrieval_round",
        "query_enforced_from_original",
        "source_id",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            provenance.setdefault(key, value)
    provenance.setdefault("worker_kind", "selected_source_retrieval")
    if normalized_tool_name:
        provenance.setdefault("tool_name", normalized_tool_name)
    provenance.setdefault("status", status)
    provenance.setdefault("terminal_reason", terminal_reason)
    strategy_used = (
        payload.get("strategy_used")
        if isinstance(payload.get("strategy_used"), dict)
        else {}
    )
    engine_owned = bool(
        provenance.get("middleware_strategy_bypassed")
        or provenance.get("tool_handler_policy_bypassed")
        or strategy_used.get("middleware_strategy_bypassed")
        or strategy_used.get("tool_handler_policy_bypassed")
        or str(strategy_used.get("retrieval_strategy") or "").strip().lower()
        == "retrieval_engine_authority"
    )
    runtime_mode = str(
        provenance.get("selected_source_runtime_mode")
        or ("retrieval_engine_authority" if engine_owned else "compatibility_fallback")
    )
    engine_owned = bool(provenance.get("engine_owned")) if "engine_owned" in provenance else engine_owned
    provenance.setdefault("selected_source_runtime_mode", runtime_mode)
    provenance.setdefault("engine_owned", engine_owned)
    if not engine_owned:
        provenance.setdefault("compatibility_fallback", True)
        provenance.setdefault(
            "compatibility_boundary",
            "legacy_selected_source_fallback_until_10_5",
        )
    authorization_context = (
        payload.get("authorization_context")
        if isinstance(payload.get("authorization_context"), dict)
        else {}
    )
    limitations = [
        str(item.get("reason") or "").strip()
        for item in retrieval_diagnostics
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    ]
    structured_terms = _selected_retrieval_structured_terms(
        str(payload.get("query") or "")
    )
    compatibility_hybrid_events = [
        str(item)
        for item in provenance.get("compatibility_hybrid_events", []) or []
        if str(item).strip()
    ]
    compact = (
        provenance.get("compact_retrieval_provenance")
        if isinstance(provenance.get("compact_retrieval_provenance"), dict)
        else {}
    )
    compact = copy.deepcopy(compact)
    date_basis_values = structured_terms.get("date_basis") or []
    type_basis_values = structured_terms.get("type_basis") or []
    topic_basis_values = structured_terms.get("topic_basis") or []
    compact.update({
        "strategy": {
            "evidence_need": str(strategy_used.get("evidence_need") or ""),
            "retrieval_strategy": str(
                strategy_used.get("retrieval_strategy")
                or "semantic_chunks"
            ),
            "semantic_chunk_lookup_ok": bool(
                strategy_used.get("semantic_chunk_lookup_ok", True)
            ),
        },
        "inventory_counts": {
            "selected_inventory_count": int(
                authorization_context.get("selected_inventory_count") or 0
            ),
            "scoped_inventory_count": int(
                authorization_context.get("scoped_inventory_count") or 0
            ),
            "shortlist_count": len(
                authorization_context.get("requested_source_ids") or []
            ),
        },
        "accepted_counts": {
            "reference_count": len(canonical_references),
            "accepted_output_count": len(payload.get("accepted_outputs") or []),
        },
        "runtime_mode": runtime_mode,
        "engine_owned": engine_owned,
        "engine_authority": bool(provenance.get("engine_authority")),
        "compatibility_fallback": bool(provenance.get("compatibility_fallback")),
        "compatibility_boundary": str(provenance.get("compatibility_boundary") or ""),
        "compatibility_hybrid_events": compatibility_hybrid_events,
        "fallback_used": bool(
            strategy_used.get("metadata_first_intent")
            and str(strategy_used.get("retrieval_strategy") or "").strip().lower()
            == "semantic_chunks"
        ),
        "basis": {
            "date_basis": (
                (date_basis_values[0] if date_basis_values else "")
                or ("structured_document_anchor" if structured_terms.get("document") else "")
            ),
            "type_basis": (
                (type_basis_values[0] if type_basis_values else "")
                or (
                    "structured_requested_types"
                    if structured_terms.get("requested_types")
                    else ""
                )
            ),
            "topic_basis": (
                (topic_basis_values[0] if topic_basis_values else "")
                or (
                    "structured_topic_terms"
                    if structured_terms.get("topic_terms")
                    else ""
                )
            ),
        },
        "material_limitations": list(dict.fromkeys([item for item in limitations if item])),
    })
    compact_budget = _selected_retrieval_compact_budget_provenance(context_budget)
    if compact_budget:
        compact["budget"] = compact_budget
    provenance["compact_retrieval_provenance"] = compact
    normalized_diagnostics: list[dict[str, Any]] = []
    for item in retrieval_diagnostics:
        if not isinstance(item, dict):
            continue
        diagnostic = copy.deepcopy(item)
        diagnostic_provenance = (
            diagnostic.get("provenance")
            if isinstance(diagnostic.get("provenance"), dict)
            else {}
        )
        diagnostic_provenance = copy.deepcopy(diagnostic_provenance)
        for key, value in {
            "worker_kind": "selected_source_retrieval",
            "tool_name": normalized_tool_name,
            "status": status,
            "terminal_reason": terminal_reason,
            "selected_source_runtime_mode": runtime_mode,
            "engine_owned": engine_owned,
            "engine_authority": bool(provenance.get("engine_authority")),
            "compatibility_fallback": bool(provenance.get("compatibility_fallback")),
            "compatibility_boundary": str(provenance.get("compatibility_boundary") or ""),
            "compact_retrieval_provenance": compact,
        }.items():
            if value not in (None, "", [], {}):
                diagnostic_provenance.setdefault(key, copy.deepcopy(value))
        if diagnostic_provenance:
            diagnostic["provenance"] = diagnostic_provenance
        normalized_diagnostics.append(_selected_retrieval_safe_contract_value(diagnostic))
    payload["retrieval_diagnostics"] = normalized_diagnostics
    payload["diagnostics"] = copy.deepcopy(normalized_diagnostics)
    payload["provenance"] = provenance

    return payload


def _selected_retrieval_safe_contract_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _selected_retrieval_safe_contract_value(raw_value)
            for key, raw_value in value.items()
            if str(key).strip().lower() not in _SELECTED_SOURCE_RAW_PATH_KEYS
        }
    if isinstance(value, tuple):
        return [_selected_retrieval_safe_contract_value(item) for item in value]
    if isinstance(value, list):
        return [_selected_retrieval_safe_contract_value(item) for item in value]
    if isinstance(value, set):
        return sorted(str(_selected_retrieval_safe_contract_value(item)) for item in value)
    if isinstance(value, str) and _selected_retrieval_looks_like_raw_path(value):
        return "[redacted_path]"
    return value


def _selected_retrieval_looks_like_raw_path(value: str) -> bool:
    text = str(value or "").strip()
    if text.startswith(("/api/v1/files/", "/openai/v1/files/", "/v1/files/")):
        return False
    return text.startswith(("/", "\\")) or (
        len(text) > 2 and text[1] == ":" and text[2] in {"\\", "/"}
    )


def _selected_retrieval_engine_contract_source(metadata: dict | None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in (
        "retrieval_engine_first_pass_contract",
        "retrieval_engine_contract",
        "selected_source_retrieval_engine_contract",
    ):
        value = metadata.get(key)
        if isinstance(value, dict):
            return copy.deepcopy(value)
    if any(
        key in metadata
        for key in (
            "accepted_outputs",
            "references",
            "canonical_references",
            "retrieval_diagnostics",
            "diagnostics",
        )
    ):
        return copy.deepcopy(metadata)
    return {}


def _selected_retrieval_engine_contract_is_trusted(
    payload: dict[str, Any],
    metadata: dict | None,
) -> bool:
    metadata = metadata if isinstance(metadata, dict) else {}
    provenance = payload.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    compact = provenance.get("compact_retrieval_provenance")
    compact = compact if isinstance(compact, dict) else {}
    strategy = payload.get("strategy_used")
    strategy = strategy if isinstance(strategy, dict) else {}
    first_pass_strategy = metadata.get("first_pass_retrieval_strategy")
    first_pass_strategy = first_pass_strategy if isinstance(first_pass_strategy, dict) else {}
    return bool(
        provenance.get("engine_authority")
        or provenance.get("middleware_strategy_bypassed")
        or compact.get("engine_authority")
        or compact.get("middleware_strategy_bypassed")
        or strategy.get("middleware_strategy_bypassed")
        or first_pass_strategy.get("middleware_strategy_bypassed")
    )


def _selected_retrieval_engine_scope_failure(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    terminal_reason = str(payload.get("terminal_reason") or payload.get("code") or "")
    terminal_reason = terminal_reason.strip().lower()
    if status in {"blocked", "denied", "permission_denied"}:
        return True
    if any(
        token in terminal_reason
        for token in (
            "denied",
            "blocked",
            "scope",
            "source_identity",
            "source_scope",
            "out_of_scope",
        )
    ):
        return True
    for item in [
        *(payload.get("diagnostics") if isinstance(payload.get("diagnostics"), list) else []),
        *(
            payload.get("retrieval_diagnostics")
            if isinstance(payload.get("retrieval_diagnostics"), list)
            else []
        ),
    ]:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or item.get("code") or "").strip().lower()
        outcome = str(item.get("outcome") or item.get("status") or "").strip().lower()
        if outcome in {"denied", "blocked", "permission_denied"}:
            return True
        if any(
            token in reason
            for token in (
                "denied",
                "blocked",
                "scope",
                "source_identity",
                "source_scope",
                "out_of_scope",
            )
        ):
            return True
    return False


def _selected_retrieval_engine_authority_response(
    *,
    metadata: dict | None,
    tool_name: str,
    normalized_timeout_seconds: float,
    query: str,
    original_query: str,
    retrieval_round: Any,
    source_id: str = "",
) -> dict[str, Any] | None:
    metadata = metadata if isinstance(metadata, dict) else {}
    payload = _selected_retrieval_engine_contract_source(metadata)
    if not payload or not _selected_retrieval_engine_contract_is_trusted(payload, metadata):
        return None

    status = str(payload.get("status") or "").strip().lower()
    terminal_reason = (
        str(payload.get("terminal_reason") or payload.get("code") or "").strip()
        or status
        or "unknown"
    )
    references = payload.get("references")
    if not isinstance(references, list):
        references = payload.get("canonical_references")
    references = [item for item in (references or []) if isinstance(item, dict)]
    accepted_outputs = [
        item
        for item in (payload.get("accepted_outputs") or [])
        if isinstance(item, dict)
    ]
    diagnostics = payload.get("retrieval_diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = payload.get("diagnostics")
    diagnostics = [item for item in (diagnostics or []) if isinstance(item, dict)]
    provenance = payload.get("provenance")
    provenance = copy.deepcopy(provenance) if isinstance(provenance, dict) else {}
    payload_runtime_mode = str(
        provenance.get("selected_source_runtime_mode") or ""
    ).strip().lower()
    engine_success = status in {"success", "partial"} and bool(
        references and accepted_outputs
    )
    fail_closed = (
        payload_runtime_mode == "retrieval_engine_fail_closed"
        or _selected_retrieval_engine_scope_failure(payload)
    )
    engine_diagnostics = bool(
        not engine_success
        and not fail_closed
        and payload_runtime_mode != "compatibility_fallback"
        and (
            provenance.get("engine_owned")
            or provenance.get("middleware_strategy_bypassed")
            or provenance.get("tool_handler_policy_bypassed")
            or payload_runtime_mode.startswith("retrieval_engine_")
        )
    )
    if not engine_success and not fail_closed and not engine_diagnostics:
        return None

    runtime_mode = (
        "retrieval_engine_authority"
        if engine_success
        else "retrieval_engine_fail_closed"
        if fail_closed
        else "retrieval_engine_diagnostics"
    )
    bypass_reason = (
        "retrieval_engine_authority_succeeded"
        if engine_success
        else "retrieval_engine_fail_closed"
        if fail_closed
        else "retrieval_engine_diagnostics_only"
    )
    retrieval_strategy = runtime_mode
    response_status = status or ("blocked" if fail_closed else "error")
    provenance["worker_kind"] = "selected_source_retrieval"
    provenance["tool_name"] = tool_name
    provenance["status"] = response_status
    provenance["terminal_reason"] = terminal_reason
    provenance["engine_authority"] = bool(engine_success)
    provenance["selected_source_runtime_mode"] = runtime_mode
    provenance["engine_owned"] = True
    provenance["compatibility_fallback"] = False
    provenance["tool_handler_policy_bypassed"] = True
    provenance["tool_handler_bypass_reason"] = bypass_reason

    plan_summary = (
        payload.get("plan_summary")
        if isinstance(payload.get("plan_summary"), dict)
        else metadata.get("retrieval_engine_plan_summary")
    )
    if isinstance(plan_summary, dict) and plan_summary:
        provenance["plan_summary"] = copy.deepcopy(plan_summary)
    context_budget = (
        copy.deepcopy(payload.get("context_budget"))
        if isinstance(payload.get("context_budget"), dict)
        else {}
    )
    if context_budget:
        provenance["context_budget"] = copy.deepcopy(context_budget)

    strategy_used = {
        "evidence_need": "engine_owned",
        "retrieval_strategy": retrieval_strategy,
        "semantic_chunk_lookup_ok": False,
        "metadata_first_intent": False,
        "middleware_strategy_bypassed": True,
        "tool_handler_policy_bypassed": True,
        "engine_status": status,
        "engine_terminal_reason": terminal_reason,
        "reason_codes": [
            "tool_handler_policy_bypassed",
            bypass_reason,
        ],
    }
    response: dict[str, Any] = {
        "status": response_status,
        "tool_name": tool_name,
        "query": query,
        "original_query": original_query,
        "retrieval_round": retrieval_round,
        "canonical_references": references if engine_success else [],
        "accepted_outputs": accepted_outputs if engine_success else [],
        "retrieval_diagnostics": diagnostics,
        "terminal_reason": terminal_reason,
        "strategy_used": strategy_used,
        "authorization_context": (
            copy.deepcopy(payload.get("authorization_context"))
            if isinstance(payload.get("authorization_context"), dict)
            else {}
        ),
        "retry_policy": (
            copy.deepcopy(payload.get("retry_policy"))
            if isinstance(payload.get("retry_policy"), dict)
            else _selected_retrieval_retry_policy(normalized_timeout_seconds)
        ),
        "context_budget": context_budget,
        "provenance": provenance,
        "result_count": len(references) if engine_success else 0,
    }
    if not engine_success:
        response["code"] = terminal_reason
    if source_id:
        response["source_id"] = source_id
    if isinstance(plan_summary, dict) and plan_summary:
        response["retrieval_engine_plan_summary"] = copy.deepcopy(plan_summary)
    return _selected_retrieval_worker_contract_lanes(
        _selected_retrieval_safe_contract_value(response),
        tool_name=tool_name,
        normalized_timeout_seconds=normalized_timeout_seconds,
    )


def _selected_retrieval_blocked_terminal_reason(
    diagnostics: list[dict[str, Any]] | None,
    *,
    fallback: str = "source_scope_blocked",
) -> str:
    for item in diagnostics or []:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip().lower()
        outcome = str(item.get("outcome") or item.get("status") or "").strip().lower()
        if reason == "missing_active_source_scope":
            return "missing_active_source_scope"
        if reason == "ambiguous_active_source_scope":
            return "ambiguous_active_source_scope"
        if "ambiguous" in reason:
            return "ambiguous_active_source_scope"
        if "expired" in reason:
            return "expired_active_source_scope"
        if reason == "invalid_authorization_context":
            return "invalid_authorization_context"
        if reason == "source_not_authorized":
            return "invalid_authorization_context"
        if outcome in {"denied", "permission_denied", "unauthorized"}:
            return "invalid_authorization_context"
    return str(fallback or "source_scope_blocked")


def _selected_retrieval_blocked_response(
    *,
    descriptor: dict[str, Any],
    tool_name: str,
    query: str,
    original_query: str,
    retrieval_round: Any,
    normalized_timeout_seconds: float,
    diagnostics: list[dict[str, Any]] | None,
    selected_inventory_count: int,
    scoped_inventory_count: int,
    requested_source_ids: list[str],
    active_source_scope: Any,
    evidence_need: str,
    executed_query: str,
    query_enforced_from_original: bool,
    normalized_required_anchors: list[str] | None = None,
    anchor_policy: str = "query_correlation_only",
    source_id: str = "",
) -> dict[str, Any]:
    blocked_diagnostics = _selected_retrieval_dedupe_diagnostics(
        [item for item in (diagnostics or []) if isinstance(item, dict)]
    )
    terminal_reason = _selected_retrieval_blocked_terminal_reason(blocked_diagnostics)
    response: dict[str, Any] = {
        "status": "blocked",
        "tool_name": tool_name,
        "code": terminal_reason,
        "query": query,
        "original_query": original_query,
        "retrieval_round": retrieval_round,
        "canonical_references": [],
        "accepted_outputs": [],
        "retrieval_diagnostics": blocked_diagnostics,
        "terminal_reason": terminal_reason,
        "strategy_used": _selected_retrieval_strategy_used(
            descriptor=descriptor,
            normalized_required_anchors=list(normalized_required_anchors or []),
            anchor_policy=anchor_policy,
            evidence_need=evidence_need,
            original_query=original_query,
            executed_query=executed_query,
            retrieval_round=retrieval_round,
            query_enforced_from_original=bool(query_enforced_from_original),
        ),
        "authorization_context": _selected_retrieval_authorization_context(
            selected_inventory_count=selected_inventory_count,
            scoped_inventory_count=scoped_inventory_count,
            requested_source_ids=requested_source_ids,
            active_source_scope=active_source_scope,
        ),
        "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
    }
    if source_id:
        response["source_id"] = source_id
    return _selected_retrieval_worker_contract_lanes(
        response,
        tool_name=tool_name,
        normalized_timeout_seconds=normalized_timeout_seconds,
    )


def _selected_retrieval_cancelled_response(
    *,
    tool_name: str,
    query: str,
    original_query: str,
    retrieval_round: Any,
    normalized_timeout_seconds: float,
    descriptor: dict[str, Any],
    normalized_required_anchors: list[str],
    anchor_policy: str,
    evidence_need: str,
    executed_query: str,
    query_enforced_from_original: bool,
    selected_inventory_count: int,
    scoped_inventory_count: int,
    requested_source_ids: list[str],
    active_source_scope: Any,
    source_id: str = "",
) -> dict[str, Any]:
    diagnostic = _selected_retrieval_compact_diagnostic(
        classification="diagnostics",
        reason="retrieval_cancelled",
        outcome="cancelled",
        tool_name=tool_name,
        query=executed_query or query,
        retrieval_round=retrieval_round,
        source_id=source_id,
    )
    response: dict[str, Any] = {
        "status": "error",
        "tool_name": tool_name,
        "code": "retrieval_cancelled",
        "query": query,
        "original_query": original_query,
        "retrieval_round": retrieval_round,
        "canonical_references": [],
        "accepted_outputs": [],
        "retrieval_diagnostics": [diagnostic],
        "terminal_reason": "retrieval_cancelled",
        "strategy_used": _selected_retrieval_strategy_used(
            descriptor=descriptor,
            normalized_required_anchors=list(normalized_required_anchors or []),
            anchor_policy=anchor_policy,
            evidence_need=evidence_need,
            original_query=original_query,
            executed_query=executed_query or query,
            retrieval_round=retrieval_round,
            query_enforced_from_original=bool(query_enforced_from_original),
        ),
        "authorization_context": _selected_retrieval_authorization_context(
            selected_inventory_count=selected_inventory_count,
            scoped_inventory_count=scoped_inventory_count,
            requested_source_ids=requested_source_ids,
            active_source_scope=active_source_scope,
        ),
        "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
    }
    if source_id:
        response["source_id"] = source_id
    return _selected_retrieval_worker_contract_lanes(
        response,
        tool_name=tool_name,
        normalized_timeout_seconds=normalized_timeout_seconds,
    )


def _selected_retrieval_structured_precision_mode(metadata: dict | None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    mode = metadata.get("structured_fact_precision_mode")
    if mode is None:
        mode = metadata.get("structured_fact_precision_guard")
    if mode is None and isinstance(metadata.get("retrieval_options"), dict):
        options = metadata.get("retrieval_options") or {}
        mode = options.get("structured_fact_precision_mode")
        if mode is None:
            mode = options.get("structured_fact_precision_guard")
    normalized_mode = str(mode or "").strip().lower()
    if not normalized_mode:
        return _STRUCTURED_FACT_PRECISION_MODE_COMPATIBILITY
    if normalized_mode in {"false", "0", "off", "disabled", "disable", "none"}:
        return _STRUCTURED_FACT_PRECISION_MODE_DISABLED
    if normalized_mode in {"true", "1", "on", "enabled", "enable"}:
        return _STRUCTURED_FACT_PRECISION_MODE_COMPATIBILITY
    if normalized_mode == _STRUCTURED_FACT_PRECISION_MODE_DISABLED:
        return _STRUCTURED_FACT_PRECISION_MODE_DISABLED
    return _STRUCTURED_FACT_PRECISION_MODE_COMPATIBILITY


def _selected_retrieval_structured_fact_source_anchor(
    structured_plan: dict[str, Any],
) -> str:
    anchor_groups = (
        structured_plan.get("anchor_groups")
        if isinstance(structured_plan.get("anchor_groups"), dict)
        else {}
    )
    for key in (
        "quota_ids",
        "standard_numbers",
        "document_numbers",
        "row_or_table_labels",
        "quoted_terms",
        "units",
    ):
        values = anchor_groups.get(key)
        if isinstance(values, list):
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
    hard_anchors = structured_plan.get("hard_anchors")
    if isinstance(hard_anchors, list):
        for value in hard_anchors:
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _selected_retrieval_structured_fact_documents(
    canonical_references: list[dict[str, Any]],
) -> tuple[list[str], bool]:
    documents: list[str] = []
    excerpt_truncated = False
    for reference in canonical_references or []:
        if not isinstance(reference, dict):
            continue
        chunks = reference.get("document") if isinstance(reference.get("document"), list) else []
        metadatas = reference.get("metadata") if isinstance(reference.get("metadata"), list) else []
        for index, chunk in enumerate(chunks):
            text = str(chunk or "").strip()
            if text:
                documents.append(text)
            metadata = metadatas[index] if index < len(metadatas) else None
            if isinstance(metadata, dict) and metadata.get("excerpt_truncated"):
                excerpt_truncated = True
    return documents, excerpt_truncated


def _selected_retrieval_standard_value_chunks(
    canonical_references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for reference_index, reference in enumerate(canonical_references or []):
        if not isinstance(reference, dict):
            continue
        documents = reference.get("document") if isinstance(reference.get("document"), list) else []
        metadatas = reference.get("metadata") if isinstance(reference.get("metadata"), list) else []
        source = reference.get("source") if isinstance(reference.get("source"), dict) else {}
        source_name = str(source.get("name") or "")
        source_id = str(source.get("id") or "")

        for chunk_index, chunk in enumerate(documents):
            chunk_text = str(chunk or "").strip()
            if not chunk_text:
                continue
            metadata = metadatas[chunk_index] if chunk_index < len(metadatas) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            metadata_text = " ".join(
                [
                    str(metadata.get("title") or ""),
                    str(metadata.get("heading") or ""),
                    str(metadata.get("section") or ""),
                    str(metadata.get("source") or ""),
                    str(metadata.get("name") or ""),
                    str(metadata.get("filename") or ""),
                    str(metadata.get("page") or ""),
                    source_name,
                    source_id,
                ]
            ).strip()
            chunks.append(
                {
                    "reference_index": reference_index,
                    "chunk_index": chunk_index,
                    "text": chunk_text,
                    "metadata_text": metadata_text,
                    "metadata": metadata,
                    "section_tokens": _selected_retrieval_standard_value_section_tokens(
                        metadata
                    ),
                    "source": source,
                }
            )
    return chunks


def _selected_retrieval_standard_value_chunk_tied_to_bundle(
    chunk: dict[str, Any],
    bundle: dict[str, Any],
    *,
    max_neighbors: int,
) -> bool:
    chunk_reference_index = _selected_retrieval_standard_value_index(
        chunk.get("reference_index"),
        -1,
    )
    bundle_reference_index = _selected_retrieval_standard_value_index(
        bundle.get("reference_index"),
        -2,
    )
    if chunk_reference_index != bundle_reference_index:
        return False

    chunk_index = _selected_retrieval_standard_value_index(
        chunk.get("chunk_index"),
        -1,
    )
    anchor_chunk_index = _selected_retrieval_standard_value_index(
        bundle.get("anchor_chunk_index"),
        -2,
    )
    if abs(chunk_index - anchor_chunk_index) <= max_neighbors:
        return True

    chunk_section_tokens = (
        chunk.get("section_tokens")
        if isinstance(chunk.get("section_tokens"), set)
        else set()
    )
    bundle_section_tokens = (
        bundle.get("section_tokens")
        if isinstance(bundle.get("section_tokens"), set)
        else set()
    )
    return bool(chunk_section_tokens and chunk_section_tokens & bundle_section_tokens)


def _selected_retrieval_standard_value_extract(
    *,
    structured_plan: dict[str, Any],
    canonical_references: list[dict[str, Any]],
    context_budget: dict[str, Any] | None,
) -> dict[str, Any]:
    chunks = _selected_retrieval_standard_value_chunks(canonical_references)
    if not chunks:
        return {
            "value": "",
            "support_state": _STRUCTURED_FACT_UNSUPPORTED_STATE,
            "confidence_reason": "",
            "limitation_reason": "no_anchor_match",
            "bundle_debug": {
                "bundle_count": 0,
                "standard_anchor_hits": 0,
                "classification_hits": 0,
                "value_hits": 0,
                "required_mapping": "project_or_source_to_classification_to_value",
            },
        }

    context_budget = context_budget if isinstance(context_budget, dict) else {}
    budget_truncated = bool(context_budget.get("truncated"))

    anchor_groups = (
        structured_plan.get("anchor_groups")
        if isinstance(structured_plan.get("anchor_groups"), dict)
        else {}
    )
    standard_numbers = _selected_retrieval_dedupe_terms(
        [str(item or "").strip() for item in anchor_groups.get("standard_numbers", [])]
    )
    raw_unit_anchors = _selected_retrieval_dedupe_terms(
        [str(item or "").strip() for item in anchor_groups.get("units", [])]
    )
    expected_unit_tokens = _selected_retrieval_dedupe_terms(
        [
            "t/(km2·a)"
            if "t/(km2·a)" in unit
            or "t/(km²·a)" in unit
            or "km2·a" in unit
            or "km²·a" in unit
            else unit
            for unit in raw_unit_anchors
            if unit
        ]
    )
    hard_anchors = [
        str(item or "").strip()
        for item in structured_plan.get("hard_anchors", [])
        if str(item or "").strip()
    ]
    classification_terms = _selected_retrieval_dedupe_terms(
        [
            *[
                str(item or "").strip()
                for item in anchor_groups.get("quoted_terms", [])
                if str(item or "").strip()
            ],
            *[
                term
                for term in hard_anchors
                if term not in standard_numbers
                and term not in raw_unit_anchors
                and not re.fullmatch(r"\d+(?:\.\d+)?", term)
            ],
        ]
    )

    if not standard_numbers:
        return {
            "value": "",
            "support_state": _STRUCTURED_FACT_UNSUPPORTED_STATE,
            "confidence_reason": "",
            "limitation_reason": "no_anchor_match",
            "bundle_debug": {
                "bundle_count": 0,
                "standard_anchor_hits": 0,
                "classification_hits": 0,
                "value_hits": 0,
                "required_mapping": "project_or_source_to_classification_to_value",
            },
        }

    standard_anchor_hits = [
        chunk
        for chunk in chunks
        if any(
            _selected_retrieval_anchor_match(
                anchor,
                source={"source": chunk.get("source") or {}},
                document=f"{chunk.get('text') or ''}\n{chunk.get('metadata_text') or ''}",
                metadata=chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {},
            )
            for anchor in standard_numbers
        )
    ]
    if not standard_anchor_hits:
        return {
            "value": "",
            "support_state": _STRUCTURED_FACT_UNSUPPORTED_STATE,
            "confidence_reason": "",
            "limitation_reason": "no_anchor_match",
            "bundle_debug": {
                "bundle_count": 0,
                "standard_anchor_hits": 0,
                "classification_hits": 0,
                "value_hits": 0,
                "required_mapping": "project_or_source_to_classification_to_value",
            },
        }

    bundle_keys: set[tuple[int, int]] = set()
    bundles: list[dict[str, Any]] = []
    max_neighbors = max(0, int(_STRUCTURED_FACT_STANDARD_BUNDLE_NEIGHBOR_CHUNKS))
    for chunk in standard_anchor_hits:
        reference_index = _selected_retrieval_standard_value_index(
            chunk.get("reference_index"),
            0,
        )
        chunk_index = _selected_retrieval_standard_value_index(
            chunk.get("chunk_index"),
            0,
        )
        if (reference_index, chunk_index) in bundle_keys:
            continue
        bundle_keys.add((reference_index, chunk_index))

        left = max(0, chunk_index - max_neighbors)
        right = chunk_index + max_neighbors
        bundle_chunks = [
            candidate
            for candidate in chunks
            if _selected_retrieval_standard_value_index(
                candidate.get("reference_index"),
                -1,
            )
            == reference_index
            and left
            <= _selected_retrieval_standard_value_index(
                candidate.get("chunk_index"),
                -1,
            )
            <= right
        ]
        if not bundle_chunks:
            bundle_chunks = [chunk]
        bundle_text = " ".join(str(item.get("text") or "") for item in bundle_chunks)
        bundle_meta_text = " ".join(
            str(item.get("metadata_text") or "") for item in bundle_chunks
        )
        bundle_section_tokens: set[str] = set()
        for item in bundle_chunks:
            section_tokens = item.get("section_tokens")
            if isinstance(section_tokens, set):
                bundle_section_tokens.update(section_tokens)
        bundles.append(
            {
                "text": bundle_text,
                "metadata_text": bundle_meta_text,
                "reference_index": reference_index,
                "anchor_chunk_index": chunk_index,
                "chunk_indexes": [
                    _selected_retrieval_standard_value_index(
                        item.get("chunk_index"),
                        0,
                    )
                    for item in bundle_chunks
                ],
                "section_tokens": bundle_section_tokens,
            }
        )
        if len(bundles) >= _STRUCTURED_FACT_STANDARD_MAX_BUNDLES:
            break

    value_candidates: list[str] = []
    classification_hit_count = 0
    value_hit_count = 0
    observed_numbers_without_unit = False
    classification_bundles: list[dict[str, Any]] = []
    for bundle in bundles:
        bundle_text = str(bundle.get("text") or "")
        bundle_meta_text = str(bundle.get("metadata_text") or "")
        combined = f"{bundle_text} {bundle_meta_text}".strip()

        has_classification = True
        if classification_terms:
            has_classification = any(
                _selected_retrieval_find_exact_anchor_match(term, combined) is not None
                or _selected_retrieval_anchor_direct_text(term)
                in _selected_retrieval_anchor_direct_text(combined)
                for term in classification_terms
            )
        if has_classification:
            classification_hit_count += 1
            classification_bundles.append(bundle)

        unit_matches = _selected_retrieval_dedupe_terms(
            [
                str(match or "").strip()
                for match in _STRUCTURED_FACT_UNIT_PATTERN.findall(combined)
                if str(match or "").strip()
            ]
        )
        if not unit_matches and raw_unit_anchors:
            unit_matches = _selected_retrieval_dedupe_terms(
                [
                    unit_anchor
                    for unit_anchor in raw_unit_anchors
                    if unit_anchor
                    and _selected_retrieval_find_exact_anchor_match(
                        unit_anchor, combined
                    )
                    is not None
                ]
            )
        has_expected_unit = True
        if expected_unit_tokens:
            has_expected_unit = any(
                any(
                    _selected_retrieval_anchor_compact_text(token)
                    in _selected_retrieval_anchor_compact_text(unit_value)
                    for token in expected_unit_tokens
                )
                for unit_value in unit_matches
            )
        if unit_matches and has_expected_unit:
            value_hit_count += 1
            for unit_value in unit_matches:
                normalized_value = _selected_retrieval_anchor_direct_text(unit_value)
                if normalized_value and normalized_value not in {
                    _selected_retrieval_anchor_direct_text(existing)
                    for existing in value_candidates
                }:
                    value_candidates.append(unit_value)
        if re.search(r"\d+(?:\.\d+)?", combined) and not unit_matches:
            observed_numbers_without_unit = True

    # Compatibility fallback: accept split value/unit evidence only when it remains
    # tied to the same classification bundle by adjacency or section metadata.
    if not value_candidates and classification_bundles and raw_unit_anchors:
        for unit_anchor in raw_unit_anchors:
            if not unit_anchor:
                continue
            for chunk in chunks:
                chunk_text = str(chunk.get("text") or "")
                if (
                    _selected_retrieval_find_exact_anchor_match(unit_anchor, chunk_text)
                    is None
                ):
                    continue
                if any(
                    _selected_retrieval_standard_value_chunk_tied_to_bundle(
                        chunk,
                        bundle,
                        max_neighbors=max_neighbors,
                    )
                    for bundle in classification_bundles
                ):
                    value_candidates.append(unit_anchor)
                    value_hit_count += 1
                    break
            if value_candidates:
                break

    if budget_truncated and not value_candidates:
        limitation_reason = "budget_truncation"
        support_state = _STRUCTURED_FACT_TRUNCATED_STATE
    elif not classification_hit_count:
        limitation_reason = "missing_standard_mapping"
        support_state = _STRUCTURED_FACT_UNSUPPORTED_STATE
    elif not value_candidates:
        limitation_reason = "missing_unit" if observed_numbers_without_unit else "missing_standard_mapping"
        support_state = _STRUCTURED_FACT_UNSUPPORTED_STATE
    elif len(value_candidates) > 1:
        limitation_reason = "conflicting_value"
        support_state = _STRUCTURED_FACT_CONFLICTING_STATE
    else:
        limitation_reason = ""
        support_state = _STRUCTURED_FACT_SUPPORTED_STATE

    confidence_reason = (
        "classification_and_value_bundle_verified"
        if support_state == _STRUCTURED_FACT_SUPPORTED_STATE
        else ""
    )
    value = value_candidates[0] if value_candidates else ""
    return {
        "value": value,
        "support_state": support_state,
        "confidence_reason": confidence_reason,
        "limitation_reason": limitation_reason,
        "bundle_debug": {
            "bundle_count": len(bundles),
            "standard_anchor_hits": len(standard_anchor_hits),
            "classification_hits": classification_hit_count,
            "value_hits": value_hit_count,
            "required_mapping": "project_or_source_to_classification_to_value",
        },
    }


def _selected_retrieval_structured_field_values(
    documents: list[str],
    *,
    field_name: str,
    required_sentence_terms: list[str] | None = None,
) -> list[str]:
    escaped_name = re.escape(str(field_name or "").strip())
    if not escaped_name:
        return []
    values: list[str] = []
    patterns = (
        re.compile(
            rf"{escaped_name}\s*[:=：]\s*([^\s,，;；。\n]+)",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"{escaped_name}[^。；;\n]{{0,16}}(?:为|是)\s*([^\s,，;；。\n]+)",
            flags=re.IGNORECASE,
        ),
    )
    required_terms = _selected_retrieval_dedupe_terms(required_sentence_terms or [])
    for document in documents:
        document_text = str(document or "")
        candidate_segments: list[str] = []
        if required_terms:
            row_blocks = re.findall(
                r"(?:定额\s*0?\d{4,6}[^\n。]*)",
                document_text,
                flags=re.IGNORECASE,
            )
            if row_blocks:
                candidate_segments.extend(
                    [
                        block
                        for block in row_blocks
                        if any(term in block for term in required_terms)
                    ]
                )
            else:
                candidate_segments.extend(
                    [
                        sentence
                        for sentence in re.split(r"[。；;\n]", document_text)
                        if any(term in str(sentence or "") for term in required_terms)
                    ]
                )
        else:
            candidate_segments = re.split(r"[。；;\n]", document_text)

        for sentence in candidate_segments:
            sentence_text = str(sentence or "").strip()
            if not sentence_text:
                continue
            for pattern in patterns:
                for match in pattern.findall(sentence_text):
                    normalized = str(match or "").strip()
                    if normalized and normalized not in values:
                            values.append(normalized)
    return values


def _selected_retrieval_table_split_cells(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if "|" in text:
        cells = [cell.strip() for cell in text.strip().strip("|").split("|")]
        cells = [re.sub(r"\s+", " ", cell).strip() for cell in cells]
        if len(cells) >= 2 and not all(
            re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in cells if cell
        ):
            return cells
    if "\t" in text:
        cells = [cell.strip() for cell in text.split("\t")]
        if len(cells) >= 2:
            return cells
    return []


def _selected_retrieval_table_line_segments(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    markers = list(
        re.finditer(
            r"(?=(?:定额\s*0?\d{4,6}\s*(?:行|号)?\s*[:：]?))",
            text,
            flags=re.IGNORECASE,
        )
    )
    if len(markers) <= 1:
        return [text]
    segments: list[str] = []
    for index, marker in enumerate(markers):
        start = marker.start()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        segment = text[start:end].strip("。；; \n\t")
        if segment:
            segments.append(segment)
    return segments or [text]


def _selected_retrieval_table_rows(
    canonical_references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for reference_index, reference in enumerate(canonical_references or []):
        if not isinstance(reference, dict):
            continue
        documents = reference.get("document") if isinstance(reference.get("document"), list) else []
        metadatas = reference.get("metadata") if isinstance(reference.get("metadata"), list) else []
        source = reference.get("source") if isinstance(reference.get("source"), dict) else {}
        source_name = str(source.get("name") or "")
        source_id = str(source.get("id") or "")

        for chunk_index, chunk in enumerate(documents):
            chunk_text = str(chunk or "").strip()
            if not chunk_text:
                continue
            metadata = metadatas[chunk_index] if chunk_index < len(metadatas) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            metadata_text = " ".join(
                [
                    str(metadata.get("title") or ""),
                    str(metadata.get("heading") or ""),
                    str(metadata.get("section") or ""),
                    str(metadata.get("table_title") or ""),
                    str(metadata.get("source") or ""),
                    str(metadata.get("name") or ""),
                    str(metadata.get("filename") or ""),
                    str(metadata.get("page") or ""),
                    source_name,
                    source_id,
                ]
            ).strip()
            raw_lines = [line for line in str(chunk_text).splitlines() if line.strip()]
            if not raw_lines:
                raw_lines = [chunk_text]
            synthetic_line_index = 0
            for line in raw_lines:
                for text in _selected_retrieval_table_line_segments(line):
                    if not text:
                        continue
                    rows.append(
                        {
                            "reference_index": reference_index,
                            "chunk_index": chunk_index,
                            "line_index": synthetic_line_index,
                            "text": text,
                            "cells": _selected_retrieval_table_split_cells(text),
                            "metadata": metadata,
                            "metadata_text": metadata_text,
                            "section_tokens": _selected_retrieval_standard_value_section_tokens(
                                metadata
                            ),
                            "source": source,
                        }
                    )
                    synthetic_line_index += 1
    return rows


def _selected_retrieval_table_text_has_term(text: str, term: str) -> bool:
    normalized_term = str(term or "").strip()
    if not normalized_term:
        return False
    return _selected_retrieval_find_exact_anchor_match(normalized_term, str(text or "")) is not None


def _selected_retrieval_table_field_units(field_name: str) -> tuple[str, ...]:
    normalized = str(field_name or "").strip().lower()
    return _STRUCTURED_FACT_TABLE_FIELD_UNIT_HINTS.get(normalized, ())


def _selected_retrieval_table_value_has_unit(value: str, units: tuple[str, ...]) -> bool:
    text = _selected_retrieval_anchor_direct_text(value)
    return any(_selected_retrieval_anchor_direct_text(unit) in text for unit in units if unit)


def _selected_retrieval_table_append_unit(value: str, unit: str) -> str:
    normalized_value = str(value or "").strip()
    normalized_unit = str(unit or "").strip()
    if not normalized_value or not normalized_unit:
        return normalized_value
    if _selected_retrieval_table_value_has_unit(normalized_value, (normalized_unit,)):
        return normalized_value
    return f"{normalized_value}{normalized_unit}"


def _selected_retrieval_table_context_rows(
    rows: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    max_neighbors: int,
) -> list[dict[str, Any]]:
    candidate_reference_index = _selected_retrieval_standard_value_index(
        candidate.get("reference_index"),
        -1,
    )
    candidate_chunk_index = _selected_retrieval_standard_value_index(
        candidate.get("chunk_index"),
        -1,
    )
    candidate_section_tokens = (
        candidate.get("section_tokens")
        if isinstance(candidate.get("section_tokens"), set)
        else set()
    )
    context: list[dict[str, Any]] = []
    for row in rows:
        row_reference_index = _selected_retrieval_standard_value_index(
            row.get("reference_index"),
            -2,
        )
        if row_reference_index != candidate_reference_index:
            continue
        row_chunk_index = _selected_retrieval_standard_value_index(
            row.get("chunk_index"),
            -2,
        )
        row_section_tokens = (
            row.get("section_tokens")
            if isinstance(row.get("section_tokens"), set)
            else set()
        )
        if abs(row_chunk_index - candidate_chunk_index) <= max_neighbors or (
            candidate_section_tokens and candidate_section_tokens & row_section_tokens
        ):
            context.append(row)
    return sorted(
        context,
        key=lambda item: (
            _selected_retrieval_standard_value_index(item.get("chunk_index"), 0),
            _selected_retrieval_standard_value_index(item.get("line_index"), 0),
        ),
    )


def _selected_retrieval_table_direct_value(row_text: str, field_name: str) -> str:
    escaped_name = re.escape(str(field_name or "").strip())
    if not escaped_name:
        return ""
    for pattern in (
        re.compile(
            rf"{escaped_name}\s*[:=：]\s*([^\s,，;；。\n|]+)",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"{escaped_name}[^。；;\n|]{{0,16}}(?:为|是)\s*([^\s,，;；。\n|]+)",
            flags=re.IGNORECASE,
        ),
    ):
        match = pattern.search(str(row_text or ""))
        if not match:
            continue
        value = str(match.group(1) or "").strip()
        value = re.sub(r"^[=:：\-\s]+|[。；;，,\s]+$", "", value).strip()
        if value and not re.fullmatch(r"[-_/—]*", value):
            return value
    return ""


def _selected_retrieval_table_cell_value(
    *,
    field_name: str,
    candidate_row: dict[str, Any],
    context_rows: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, int | None]:
    candidate_cells = (
        candidate_row.get("cells") if isinstance(candidate_row.get("cells"), list) else []
    )
    if not candidate_cells:
        return "", None, None, None

    header_rows = [
        row
        for row in context_rows
        if row is not candidate_row
        and isinstance(row.get("cells"), list)
        and row.get("cells")
        and any(
            _selected_retrieval_table_text_has_term(str(cell or ""), field_name)
            for cell in row.get("cells") or []
        )
    ]
    unit_rows = [
        row
        for row in context_rows
        if row is not candidate_row
        and isinstance(row.get("cells"), list)
        and row.get("cells")
        and (
            "单位" in str(row.get("text") or "")
            or any(
                _selected_retrieval_table_text_has_term(str(cell or ""), unit)
                for unit in _STRUCTURED_FACT_TABLE_UNIT_TOKENS
                for cell in row.get("cells") or []
            )
        )
    ]

    for header_row in header_rows:
        header_cells = header_row.get("cells") if isinstance(header_row.get("cells"), list) else []
        for column_index, header_cell in enumerate(header_cells):
            if not _selected_retrieval_table_text_has_term(str(header_cell or ""), field_name):
                continue
            if column_index >= len(candidate_cells):
                continue
            value = str(candidate_cells[column_index] or "").strip()
            value = re.sub(r"^[=:：\-\s]+|[。；;，,\s]+$", "", value).strip()
            if not value or re.fullmatch(r"[-_/—]*", value):
                return "", header_row, None, column_index
            unit_row = None
            unit = ""
            for row in unit_rows:
                row_cells = row.get("cells") if isinstance(row.get("cells"), list) else []
                if column_index < len(row_cells):
                    unit = str(row_cells[column_index] or "").strip()
                    if unit and unit not in {"-", "—", "/"}:
                        unit_row = row
                        break
            return _selected_retrieval_table_append_unit(value, unit), header_row, unit_row, column_index
    return "", None, None, None


def _selected_retrieval_table_field_bindings(
    *,
    structured_plan: dict[str, Any],
    canonical_references: list[dict[str, Any]],
    field_names: list[str],
    context_budget: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    rows = _selected_retrieval_table_rows(canonical_references)
    context_budget = context_budget if isinstance(context_budget, dict) else {}
    parser_or_budget_loss = bool(context_budget.get("truncated"))
    anchor_groups = (
        structured_plan.get("anchor_groups")
        if isinstance(structured_plan.get("anchor_groups"), dict)
        else {}
    )
    row_anchors = _selected_retrieval_dedupe_terms(
        [
            *[
                str(item or "").strip()
                for item in anchor_groups.get("quota_ids", [])
                if str(item or "").strip()
            ],
            *[
                str(item or "").strip()
                for item in anchor_groups.get("row_or_table_labels", [])
                if str(item or "").strip()
            ],
        ]
    )
    candidate_rows = [
        row
        for row in rows
        if any(
            _selected_retrieval_table_text_has_term(str(row.get("text") or ""), anchor)
            for anchor in row_anchors
        )
    ]
    candidate_rows = candidate_rows[:_STRUCTURED_FACT_TABLE_MAX_CANDIDATE_ROWS]

    bindings: dict[str, dict[str, Any]] = {}
    for field_name in field_names:
        supported_values: list[str] = []
        supported_bundles: list[dict[str, Any]] = []
        observed_row = bool(candidate_rows)
        observed_header = False
        observed_unit_gap = False
        expected_units = _selected_retrieval_table_field_units(field_name)

        for candidate_row in candidate_rows:
            context_rows = _selected_retrieval_table_context_rows(
                rows,
                candidate_row,
                max_neighbors=max(0, int(_STRUCTURED_FACT_TABLE_BUNDLE_NEIGHBOR_CHUNKS)),
            )
            row_text = str(candidate_row.get("text") or "")
            direct_value = _selected_retrieval_table_direct_value(row_text, field_name)
            if direct_value:
                if expected_units and not _selected_retrieval_table_value_has_unit(
                    direct_value,
                    expected_units,
                ):
                    observed_unit_gap = True
                    continue
                if direct_value not in supported_values:
                    supported_values.append(direct_value)
                    supported_bundles.append(
                        {
                            "binding": "direct_field_value",
                            "row_text": row_text,
                            "reference_index": candidate_row.get("reference_index"),
                            "chunk_index": candidate_row.get("chunk_index"),
                            "field_mapping": "direct_key_value",
                            "unit_verified": bool(
                                not expected_units
                                or _selected_retrieval_table_value_has_unit(
                                    direct_value,
                                    expected_units,
                                )
                            ),
                        }
                    )
                continue

            cell_value, header_row, unit_row, column_index = _selected_retrieval_table_cell_value(
                field_name=field_name,
                candidate_row=candidate_row,
                context_rows=context_rows,
            )
            if header_row is not None:
                observed_header = True
            if not cell_value:
                continue
            if expected_units and not (
                unit_row is not None
                or _selected_retrieval_table_value_has_unit(cell_value, expected_units)
            ):
                observed_unit_gap = True
                continue
            if cell_value not in supported_values:
                supported_values.append(cell_value)
                supported_bundles.append(
                    {
                        "binding": "table_row_header_unit",
                        "row_text": row_text,
                        "header_text": str(header_row.get("text") or "")
                        if isinstance(header_row, dict)
                        else "",
                        "unit_text": str(unit_row.get("text") or "")
                        if isinstance(unit_row, dict)
                        else "",
                        "reference_index": candidate_row.get("reference_index"),
                        "chunk_index": candidate_row.get("chunk_index"),
                        "column_index": column_index,
                        "unit_verified": bool(
                            unit_row is not None
                            or not expected_units
                            or _selected_retrieval_table_value_has_unit(
                                cell_value,
                                expected_units,
                            )
                        ),
                    }
                )

        if len(supported_values) > 1:
            bindings[field_name] = {
                "value": "",
                "support_state": _STRUCTURED_FACT_CONFLICTING_STATE,
                "confidence_reason": "",
                "limitation_reason": "conflicting_field_values",
                "evidence_bundle": {
                    "candidate_row_count": len(candidate_rows),
                    "values": supported_values,
                },
            }
        elif len(supported_values) == 1:
            bindings[field_name] = {
                "value": supported_values[0],
                "support_state": _STRUCTURED_FACT_SUPPORTED_STATE,
                "confidence_reason": "table_row_header_unit_binding",
                "limitation_reason": "",
                "evidence_bundle": supported_bundles[0] if supported_bundles else {},
            }
        else:
            limitation_reason = "no_anchor_match"
            if observed_row and observed_unit_gap:
                limitation_reason = "missing_unit_row"
            elif observed_row and not observed_header:
                limitation_reason = "missing_table_header"
            elif observed_row:
                limitation_reason = "missing_field_mapping"
            elif parser_or_budget_loss:
                limitation_reason = "budget_truncation"
            support_state = (
                _STRUCTURED_FACT_TRUNCATED_STATE
                if limitation_reason == "budget_truncation"
                else _STRUCTURED_FACT_UNSUPPORTED_STATE
            )
            bindings[field_name] = {
                "value": "",
                "support_state": support_state,
                "confidence_reason": "",
                "limitation_reason": limitation_reason,
                "evidence_bundle": {
                    "candidate_row_count": len(candidate_rows),
                    "row_anchor_count": len(row_anchors),
                    "required_mapping": "row_to_header_to_unit_to_field",
                },
            }
    return bindings


def _selected_retrieval_structured_fact_fields(
    *,
    strategy_used: dict[str, Any],
    query: str,
    canonical_references: list[dict[str, Any]],
    context_budget: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(strategy_used, dict):
        return []
    structured_plan = (
        strategy_used.get("structured_fact_plan")
        if isinstance(strategy_used.get("structured_fact_plan"), dict)
        else {}
    )
    structured_intent = str(strategy_used.get("structured_fact_intent") or "none")
    if not structured_plan or structured_intent == "none":
        return []

    answer_slots = _selected_retrieval_dedupe_terms(
        structured_plan.get("answer_slots")
        if isinstance(structured_plan.get("answer_slots"), list)
        else []
    )
    field_names = list(answer_slots)
    if not field_names and structured_intent == "standard_value":
        field_names = ["standard_value"]
    if not field_names:
        return []

    documents, excerpt_truncated = _selected_retrieval_structured_fact_documents(
        canonical_references
    )
    context_budget = context_budget if isinstance(context_budget, dict) else {}
    budget_truncated = bool(context_budget.get("truncated"))
    parser_or_budget_loss = bool(excerpt_truncated or budget_truncated)
    source_anchor = _selected_retrieval_structured_fact_source_anchor(structured_plan)
    normalized_query = str(query or "")
    fields: list[dict[str, Any]] = []
    anchor_groups = (
        structured_plan.get("anchor_groups")
        if isinstance(structured_plan.get("anchor_groups"), dict)
        else {}
    )
    table_binding_intent = bool(
        structured_intent in {"quota_table_row", "multi_field_slot_extraction"}
        and (
            anchor_groups.get("quota_ids")
            or anchor_groups.get("row_or_table_labels")
        )
    )
    table_field_bindings = (
        _selected_retrieval_table_field_bindings(
            structured_plan=structured_plan,
            canonical_references=canonical_references,
            field_names=field_names,
            context_budget=context_budget,
        )
        if table_binding_intent
        else {}
    )

    for field_name in field_names:
        normalized_field_name = str(field_name or "").strip()
        if not normalized_field_name:
            continue
        values: list[str] = []
        if normalized_field_name == "standard_value":
            standard_value = _selected_retrieval_standard_value_extract(
                structured_plan=structured_plan,
                canonical_references=canonical_references,
                context_budget=context_budget,
            )
            support_state = str(
                standard_value.get("support_state") or _STRUCTURED_FACT_UNSUPPORTED_STATE
            )
            value = str(standard_value.get("value") or "")
            confidence_reason = str(standard_value.get("confidence_reason") or "")
            limitation_reason = str(
                standard_value.get("limitation_reason") or "missing_standard_mapping"
            )
            if support_state != _STRUCTURED_FACT_SUPPORTED_STATE and excerpt_truncated:
                support_state = _STRUCTURED_FACT_TRUNCATED_STATE
                limitation_reason = "parser_loss"
            fields.append(
                {
                    "field_name": normalized_field_name,
                    "value": value,
                    "support_state": support_state,
                    "source_anchor": source_anchor,
                    "source_anchor_kind": "compatibility_text",
                    "source_anchor_note": "compatibility_only_anchor_text",
                    "confidence_reason": confidence_reason,
                    "limitation_reason": limitation_reason,
                    "strategy_name": structured_intent,
                    "evidence_bundle": standard_value.get("bundle_debug")
                    if isinstance(standard_value.get("bundle_debug"), dict)
                    else {},
                }
            )
            continue
        else:
            sentence_anchor_terms = _selected_retrieval_dedupe_terms(
                [
                    *[
                        str(item or "").strip()
                        for item in anchor_groups.get("quota_ids", [])
                        if str(item or "").strip()
                    ],
                    *[
                        str(item or "").strip()
                        for item in anchor_groups.get("row_or_table_labels", [])
                        if str(item or "").strip()
                    ],
                ]
            )
            if table_binding_intent:
                binding = table_field_bindings.get(normalized_field_name) or {}
                support_state = str(
                    binding.get("support_state") or _STRUCTURED_FACT_UNSUPPORTED_STATE
                )
                value = str(binding.get("value") or "")
                fields.append(
                    {
                        "field_name": normalized_field_name,
                        "value": value,
                        "support_state": support_state,
                        "source_anchor": source_anchor,
                        "source_anchor_kind": "compatibility_text",
                        "source_anchor_note": "compatibility_only_anchor_text",
                        "confidence_reason": str(binding.get("confidence_reason") or ""),
                        "limitation_reason": str(
                            binding.get("limitation_reason") or "missing_field_mapping"
                        ),
                        "strategy_name": structured_intent,
                        "evidence_bundle": binding.get("evidence_bundle")
                        if isinstance(binding.get("evidence_bundle"), dict)
                        else {},
                    }
                )
                continue
            else:
                values = _selected_retrieval_structured_field_values(
                    documents,
                    field_name=normalized_field_name,
                    required_sentence_terms=sentence_anchor_terms,
                )

        support_state = _STRUCTURED_FACT_UNSUPPORTED_STATE
        confidence_reason = ""
        limitation_reason = "missing_field_mapping"
        value = values[0] if values else ""

        if len(values) > 1:
            support_state = _STRUCTURED_FACT_CONFLICTING_STATE
            limitation_reason = "conflicting_field_values"
        elif len(values) == 1:
            support_state = _STRUCTURED_FACT_SUPPORTED_STATE
            confidence_reason = (
                "direct_standard_value_evidence"
                if normalized_field_name == "standard_value"
                else "direct_field_value_binding"
            )
            limitation_reason = ""
        elif parser_or_budget_loss:
            support_state = _STRUCTURED_FACT_TRUNCATED_STATE
            limitation_reason = (
                "parser_loss"
                if excerpt_truncated
                else "budget_truncation"
            )
        elif structured_intent == "standard_value":
            limitation_reason = "missing_standard_mapping"
        elif re.search(r"(?:表头|header)", normalized_query, flags=re.IGNORECASE):
            limitation_reason = "missing_table_header"
        elif re.search(r"(?:单位行|unit\s*row)", normalized_query, flags=re.IGNORECASE):
            limitation_reason = "missing_unit_row"

        fields.append(
            {
                "field_name": normalized_field_name,
                "value": value,
                "support_state": support_state,
                "source_anchor": source_anchor,
                "source_anchor_kind": "compatibility_text",
                "source_anchor_note": "compatibility_only_anchor_text",
                "confidence_reason": confidence_reason,
                "limitation_reason": limitation_reason,
                "strategy_name": structured_intent,
            }
        )

    return fields


def _selected_retrieval_apply_structured_precision_guard(
    *,
    status: str,
    strategy_used: dict[str, Any],
    precision_mode: str,
    query: str,
    original_query: str,
    retrieval_round: Any,
    tool_name: str,
    canonical_references: list[dict[str, Any]],
    accepted_outputs: list[dict[str, Any]],
    retrieval_diagnostics: list[dict[str, Any]],
    context_budget: dict[str, Any] | None,
) -> dict[str, Any]:
    fields = _selected_retrieval_structured_fact_fields(
        strategy_used=strategy_used,
        query=query,
        canonical_references=canonical_references,
        context_budget=context_budget,
    )
    if not fields:
        return {
            "status": status,
            "canonical_references": canonical_references,
            "accepted_outputs": accepted_outputs,
            "retrieval_diagnostics": retrieval_diagnostics,
            "structured_fact_fields": [],
            "code": "",
            "terminal_reason": "",
        }

    supported_fields = [
        field
        for field in fields
        if str(field.get("support_state") or "") == _STRUCTURED_FACT_SUPPORTED_STATE
    ]
    diagnostics = list(retrieval_diagnostics or [])
    for field in fields:
        support_state = str(field.get("support_state") or "")
        if support_state == _STRUCTURED_FACT_SUPPORTED_STATE:
            continue
        limitation_reason = str(field.get("limitation_reason") or "missing_field_mapping")
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason=limitation_reason,
                outcome=support_state or _STRUCTURED_FACT_UNSUPPORTED_STATE,
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
                detail={"field_name": str(field.get("field_name") or "")},
            )
        )

    strategy_plan = (
        strategy_used.get("structured_fact_plan")
        if isinstance(strategy_used.get("structured_fact_plan"), dict)
        else {}
    )
    guard_required = bool(
        strategy_plan.get("structured_routing_allowed")
        and (
            strategy_plan.get("has_exact_anchor_signal")
            or strategy_plan.get("has_multi_field_signal")
        )
    )
    guard_enabled = precision_mode != _STRUCTURED_FACT_PRECISION_MODE_DISABLED
    has_non_supported_fields = any(
        str(field.get("support_state") or "") != _STRUCTURED_FACT_SUPPORTED_STATE
        for field in fields
    )
    requires_complete_field_support = bool(
        isinstance(strategy_plan.get("answer_slots"), list)
        and strategy_plan.get("answer_slots")
    )
    limitation_only = bool(
        guard_required
        and (
            not supported_fields
            or (requires_complete_field_support and has_non_supported_fields)
        )
    )
    rejected = bool(
        guard_enabled and limitation_only
    )
    guard_reason = (
        "structured_precision_guard_no_supported_fields"
        if not supported_fields
        else "structured_precision_guard_incomplete_fields"
    )
    if rejected:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="no_evidence",
                reason=guard_reason,
                outcome="blocked",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )

    diagnostics = _selected_retrieval_dedupe_diagnostics(diagnostics)
    structured_outputs = [
        {
            "type": "structured_fact_field",
            "tool_name": tool_name,
            "query": query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "field_name": str(field.get("field_name") or ""),
            "value": str(field.get("value") or ""),
            "support_state": _STRUCTURED_FACT_SUPPORTED_STATE,
            "source_anchor": str(field.get("source_anchor") or ""),
            "source_anchor_kind": "compatibility_text",
            "source_anchor_note": "compatibility_only_anchor_text",
            "confidence_reason": str(field.get("confidence_reason") or ""),
            "limitation_reason": "",
            "strategy_name": str(field.get("strategy_name") or ""),
        }
        for field in supported_fields
    ]
    if rejected:
        return {
            "status": "no_evidence",
            "canonical_references": [],
            "accepted_outputs": [],
            "retrieval_diagnostics": diagnostics,
            "structured_fact_fields": fields,
            "code": guard_reason,
            "terminal_reason": guard_reason,
        }

    if limitation_only and not guard_enabled:
        diagnostics.append(
            _selected_retrieval_compact_diagnostic(
                classification="no_evidence",
                reason="structured_precision_guard_disabled_context_only",
                outcome="no_evidence",
                tool_name=tool_name,
                query=query,
                retrieval_round=retrieval_round,
            )
        )
        return {
            "status": "no_evidence",
            "canonical_references": [],
            "accepted_outputs": [],
            "retrieval_diagnostics": _selected_retrieval_dedupe_diagnostics(diagnostics),
            "structured_fact_fields": fields,
            "code": "",
            "terminal_reason": "structured_precision_guard_disabled_context_only",
        }

    return {
        "status": status,
        "canonical_references": canonical_references,
        "accepted_outputs": [*accepted_outputs, *structured_outputs],
        "retrieval_diagnostics": diagnostics,
        "structured_fact_fields": fields,
        "code": "",
        "terminal_reason": "",
    }


def _selected_retrieval_accepted_outputs(
    canonical_references: list[dict[str, Any]],
    *,
    tool_name: str,
    query: str,
    original_query: str,
    retrieval_round: Any,
) -> list[dict[str, Any]]:
    accepted_outputs: list[dict[str, Any]] = []
    for reference in canonical_references or []:
        if not isinstance(reference, dict):
            continue
        source = reference.get("source") if isinstance(reference.get("source"), dict) else {}
        documents = reference.get("document") if isinstance(reference.get("document"), list) else []
        metadatas = reference.get("metadata") if isinstance(reference.get("metadata"), list) else []
        snippet = str(documents[0] or "").strip() if documents else ""
        metadata = metadatas[0] if metadatas and isinstance(metadatas[0], dict) else {}
        if not snippet:
            continue
        accepted_outputs.append(
            {
                "type": "selected_source_evidence",
                "tool_name": tool_name,
                "query": query,
                "original_query": original_query,
                "retrieval_round": retrieval_round,
                "source": {
                    "id": str(source.get("id") or metadata.get("file_id") or ""),
                    "name": str(source.get("name") or metadata.get("name") or metadata.get("title") or ""),
                    "type": str(source.get("type") or metadata.get("source_type") or ""),
                },
                "snippet": snippet,
                "provenance": {
                    "file_id": str(
                        metadata.get("file_id")
                        or metadata.get("fileId")
                        or source.get("id")
                        or ""
                    ),
                    "knowledge_id": str(metadata.get("knowledge_id") or ""),
                    "document_id": str(metadata.get("document_id") or ""),
                    "retrieval_tool_name": str(
                        metadata.get("retrieval_tool_name") or tool_name
                    ),
                },
            }
        )
    return accepted_outputs


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
    original_query: str = "",
    required_anchors: Optional[list[str]] = None,
    evidence_need: str = "",
    timeout_seconds: Optional[float] = None,
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
    :param original_query: Optional original user query for deterministic round-1 execution.
    :param required_anchors: Optional strict anchors (used only when confidently required).
    :param evidence_need: Optional retrieval intent hint (for strategy/provenance only).
    :param timeout_seconds: Optional hard timeout for the provider call.
    """

    descriptor = _selected_retrieval_descriptor("query_selected_knowledge_files")
    metadata = __metadata__ if isinstance(__metadata__, dict) else {}
    structured_precision_mode = _selected_retrieval_structured_precision_mode(metadata)
    active_source_scope = (
        metadata.get("active_source_scope")
        if isinstance(metadata.get("active_source_scope"), dict)
        else {}
    )
    normalized_query, original_query, enforced_original_query = (
        _selected_retrieval_effective_query(
            query,
            retrieval_round=retrieval_round,
            metadata=metadata,
            explicit_original_query=original_query,
        )
    )
    normalized_timeout_seconds = _selected_retrieval_timeout_seconds(
        timeout_seconds,
        request=__request__,
    )

    def finalize(response: dict[str, Any]) -> dict[str, Any]:
        return _selected_retrieval_worker_contract_lanes(
            response,
            tool_name="query_selected_knowledge_files",
            normalized_timeout_seconds=normalized_timeout_seconds,
        )

    if not normalized_query:
        return finalize({
            "status": "malformed",
            "tool_name": "query_selected_knowledge_files",
            "code": "missing_query",
            "message": "A focused query is required.",
            "retrieval_round": retrieval_round,
            "terminal_reason": "missing_query",
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })
    if __request__ is None:
        return finalize({
            "status": "error",
            "tool_name": "query_selected_knowledge_files",
            "code": "runtime_context_unavailable",
            "message": "Selected-source retrieval runtime context is unavailable.",
            "query": normalized_query,
            "retrieval_round": retrieval_round,
            "terminal_reason": "runtime_context_unavailable",
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

    engine_authority_response = _selected_retrieval_engine_authority_response(
        metadata=metadata,
        tool_name="query_selected_knowledge_files",
        normalized_timeout_seconds=normalized_timeout_seconds,
        query=normalized_query,
        original_query=original_query,
        retrieval_round=retrieval_round,
    )
    if engine_authority_response is not None:
        return engine_authority_response

    selected_candidates = _deepagent_selected_source_candidates(
        files=__files__,
        knowledge=__knowledge__,
        metadata=__metadata__,
    )
    requested_source_ids = []
    for source_id in source_ids or []:
        normalized_source_id = str(source_id or "").strip()
        if normalized_source_id and normalized_source_id not in requested_source_ids:
            requested_source_ids.append(normalized_source_id)

    if not active_source_scope:
        if not selected_candidates:
            return _selected_retrieval_blocked_response(
                descriptor=descriptor,
                tool_name="query_selected_knowledge_files",
                query=normalized_query,
                original_query=original_query,
                retrieval_round=retrieval_round,
                normalized_timeout_seconds=normalized_timeout_seconds,
                diagnostics=[
                    _selected_retrieval_compact_diagnostic(
                        classification="diagnostics",
                        reason="missing_active_source_scope",
                        outcome="blocked",
                        tool_name="query_selected_knowledge_files",
                        query=normalized_query,
                        retrieval_round=retrieval_round,
                    )
                ],
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=0,
                requested_source_ids=requested_source_ids,
                active_source_scope=active_source_scope,
                evidence_need=evidence_need,
                executed_query=normalized_query,
                query_enforced_from_original=bool(enforced_original_query),
            )
        if len(selected_candidates) > 1 and not requested_source_ids:
            return _selected_retrieval_blocked_response(
                descriptor=descriptor,
                tool_name="query_selected_knowledge_files",
                query=normalized_query,
                original_query=original_query,
                retrieval_round=retrieval_round,
                normalized_timeout_seconds=normalized_timeout_seconds,
                diagnostics=[
                    _selected_retrieval_compact_diagnostic(
                        classification="diagnostics",
                        reason="ambiguous_active_source_scope",
                        outcome="blocked",
                        tool_name="query_selected_knowledge_files",
                        query=normalized_query,
                        retrieval_round=retrieval_round,
                    )
                ],
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=0,
                requested_source_ids=requested_source_ids,
                active_source_scope=active_source_scope,
                evidence_need=evidence_need,
                executed_query=normalized_query,
                query_enforced_from_original=bool(enforced_original_query),
            )

    candidates, scope_diagnostics = _selected_retrieval_scope_candidate_filter(
        selected_candidates,
        metadata,
        query=normalized_query,
        retrieval_round=retrieval_round,
        tool_name="query_selected_knowledge_files",
    )
    candidates, authorization_diagnostics = _selected_retrieval_filter_authorized_candidates(
        candidates,
        query=normalized_query,
        retrieval_round=retrieval_round,
        tool_name="query_selected_knowledge_files",
    )
    combined_scope_diagnostics = _selected_retrieval_dedupe_diagnostics(
        [*scope_diagnostics, *authorization_diagnostics]
    )
    if combined_scope_diagnostics and not candidates:
        return _selected_retrieval_blocked_response(
            descriptor=descriptor,
            tool_name="query_selected_knowledge_files",
            query=normalized_query,
            original_query=original_query,
            retrieval_round=retrieval_round,
            normalized_timeout_seconds=normalized_timeout_seconds,
            diagnostics=combined_scope_diagnostics,
            selected_inventory_count=len(selected_candidates),
            scoped_inventory_count=len(candidates),
            requested_source_ids=requested_source_ids,
            active_source_scope=active_source_scope,
            evidence_need=evidence_need,
            executed_query=normalized_query,
            query_enforced_from_original=bool(enforced_original_query),
        )

    if requested_source_ids:
        selected_identity_values: set[str] = set()
        for candidate in selected_candidates:
            selected_identity_values.update(
                _selected_retrieval_source_identity_values(candidate)
            )
        scoped_identity_values: set[str] = set()
        for candidate in candidates:
            scoped_identity_values.update(
                _selected_retrieval_source_identity_values(candidate)
            )
        unknown_ids = [
            source_id
            for source_id in requested_source_ids
            if _selected_retrieval_normalize_identifier(source_id)
            not in selected_identity_values
        ]
        out_of_scope_ids = [
            source_id
            for source_id in requested_source_ids
            if _selected_retrieval_normalize_identifier(source_id)
            in selected_identity_values
            and _selected_retrieval_normalize_identifier(source_id)
            not in scoped_identity_values
        ]
        if unknown_ids or out_of_scope_ids:
            diagnostics = [
                _selected_retrieval_compact_diagnostic(
                    classification="no_evidence",
                    reason="requested_source_out_of_scope",
                    outcome="denied",
                    tool_name="query_selected_knowledge_files",
                    query=normalized_query,
                    retrieval_round=retrieval_round,
                    detail={
                        "unknown_source_ids": unknown_ids,
                        "out_of_scope_source_ids": out_of_scope_ids,
                    },
                )
            ]
            return finalize({
                "status": "permission_denied",
                "tool_name": "query_selected_knowledge_files",
                "code": "requested_source_out_of_scope",
                "query": normalized_query,
                "original_query": original_query,
                "retrieval_round": retrieval_round,
                "retrieval_diagnostics": diagnostics,
                "terminal_reason": "requested_source_out_of_scope",
                "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
            })
        candidates = _filter_deepagent_sources_by_ids(candidates, requested_source_ids)

    if not candidates:
        return finalize({
            "status": "permission_denied" if requested_source_ids else "no_evidence",
            "tool_name": "query_selected_knowledge_files",
            "code": "requested_source_out_of_scope"
            if requested_source_ids
            else "no_selected_sources",
            "query": normalized_query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "retrieval_diagnostics": combined_scope_diagnostics or [],
            "terminal_reason": "requested_source_out_of_scope"
            if requested_source_ids
            else "no_selected_sources",
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

    allowed_identity_values: set[str] = set()
    for candidate in candidates:
        allowed_identity_values.update(_selected_retrieval_source_identity_values(candidate))
    normalized_required_anchors = _selected_retrieval_extract_confident_required_anchors(
        original_query or normalized_query,
        metadata=metadata,
        explicit_required_anchors=required_anchors,
        candidates=candidates,
    )
    structured_terms = _selected_retrieval_structured_terms(normalized_query)
    required_facets = list(
        dict.fromkeys(
            [
                *structured_terms.get("evidence_facets", []),
                *structured_terms.get("topic_terms", []),
                *structured_terms.get("requested_types", []),
            ]
        )
    )
    routing_strategy = _selected_retrieval_normalize_strategy(
        evidence_need=evidence_need,
        query=normalized_query,
        required_anchors=normalized_required_anchors,
        source_ids=requested_source_ids,
        active_source_scope=active_source_scope,
    )
    structured_plan = (
        routing_strategy.get("structured_fact_plan")
        if isinstance(routing_strategy.get("structured_fact_plan"), dict)
        else {}
    )
    structured_lookup_queries = [
        str(value)
        for value in routing_strategy.get("structured_query_variants") or []
        if str(value).strip()
    ]
    structured_lookup_enabled = bool(
        routing_strategy.get("structured_routing_allowed")
        and (
            structured_plan.get("has_exact_anchor_signal")
            or structured_plan.get("has_multi_field_signal")
        )
    )
    structured_lookup_anchors = _selected_retrieval_dedupe_terms(
        [
            *(structured_plan.get("hard_anchors") or []),
            *normalized_required_anchors,
        ]
    )
    effective_evidence_need = (
        str(routing_strategy.get("normalized_evidence_need") or "").strip()
        or evidence_need
    )
    anchor_policy = _selected_retrieval_anchor_policy(normalized_required_anchors)

    if (
        routing_strategy.get("metadata_first_intent")
        and not routing_strategy.get("semantic_chunk_lookup_ok")
    ):
        diagnostics = _selected_retrieval_dedupe_diagnostics(
            [
                _selected_retrieval_compact_diagnostic(
                    classification="no_evidence",
                    reason="metadata_first_targeted_evidence_required",
                    outcome="blocked",
                    tool_name="query_selected_knowledge_files",
                    query=normalized_query,
                    retrieval_round=retrieval_round,
                    detail={
                        "targeted_context_present": bool(
                            routing_strategy.get("targeted_context_present")
                        ),
                        "targeted_context_bounded": bool(
                            routing_strategy.get("targeted_context_bounded")
                        ),
                    },
                ),
                *_selected_retrieval_metadata_first_diagnostics(
                    query=normalized_query,
                    tool_name="query_selected_knowledge_files",
                    retrieval_round=retrieval_round,
                    routing_strategy=routing_strategy,
                    selected_inventory_count=len(selected_candidates),
                    scoped_inventory_count=len(candidates),
                    structured_terms=structured_terms,
                ),
            ]
        )
        strategy_used = _selected_retrieval_with_strategy_metadata(
            _selected_retrieval_strategy_used(
                descriptor=descriptor,
                normalized_required_anchors=normalized_required_anchors,
                anchor_policy=anchor_policy,
                evidence_need=effective_evidence_need,
                original_query=original_query,
                executed_query=normalized_query,
                retrieval_round=retrieval_round,
                query_enforced_from_original=bool(enforced_original_query),
            ),
            routing_strategy,
        )
        return finalize({
            "status": "no_evidence",
            "tool_name": "query_selected_knowledge_files",
            "code": "metadata_first_targeted_evidence_required",
            "query": normalized_query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "canonical_references": [],
            "accepted_outputs": [],
            "retrieval_diagnostics": diagnostics,
            "terminal_reason": "metadata_first_targeted_evidence_required",
            "strategy_used": strategy_used,
            "authorization_context": _selected_retrieval_authorization_context(
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=len(candidates),
                requested_source_ids=requested_source_ids,
                active_source_scope=active_source_scope,
            ),
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

    from open_webui.retrieval.utils import get_sources_from_items

    provider_queries = [normalized_query]
    prefilter_diagnostics: list[dict[str, Any]] = []
    compatibility_hybrid_events: list[str] = []
    exact_candidates: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    def _compatibility_provenance_payload() -> dict[str, Any]:
        if not compatibility_hybrid_events:
            return {}
        return {
            "compatibility_hybrid_events": list(compatibility_hybrid_events),
        }
    if structured_lookup_enabled:
        provider_queries = structured_lookup_queries or [normalized_query]
        exact_lookup_result = await _selected_retrieval_run_local_exact_lookup(
            get_sources_from_items=get_sources_from_items,
            request=__request__,
            user_model=__user_model__,
            candidates=candidates,
            queries=provider_queries,
            anchors=structured_lookup_anchors,
            timeout_seconds=normalized_timeout_seconds,
            tool_name="query_selected_knowledge_files",
            query=normalized_query,
            retrieval_round=retrieval_round,
            allowed_identity_values=allowed_identity_values,
        )
        prefilter_diagnostics.extend(exact_lookup_result.get("diagnostics") or [])
        exact_candidates = [
            item
            for item in exact_lookup_result.get("exact_candidates") or []
            if isinstance(item, dict)
        ]
        sources = [
            item
            for item in exact_lookup_result.get("sources") or []
            if isinstance(item, dict)
        ]

    if not sources:
        try:
            provider_call = get_sources_from_items(
                request=__request__,
                items=candidates,
                queries=provider_queries,
                embedding_function=lambda text, prefix: __request__.app.state.EMBEDDING_FUNCTION(
                    text, prefix=prefix, user=__user_model__
                ),
                k=max(
                    1,
                    min(
                        int(k or descriptor.get("default_k") or 5),
                        int(descriptor.get("max_k") or 8),
                    ),
                ),
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
            sources = await asyncio.wait_for(
                provider_call,
                timeout=normalized_timeout_seconds,
            )
            compatibility_provenance = getattr(
                sources, "compatibility_provenance", {}
            )
            if isinstance(compatibility_provenance, dict):
                for event in compatibility_provenance.get("events", []) or []:
                    event_text = str(event or "").strip()
                    if event_text and event_text not in compatibility_hybrid_events:
                        compatibility_hybrid_events.append(event_text)
            prefilter_diagnostics = _selected_retrieval_dedupe_diagnostics(
                [
                    *prefilter_diagnostics,
                    *[
                        item
                        for item in getattr(sources, "compatibility_diagnostics", []) or []
                        if isinstance(item, dict)
                    ],
                ]
            )
        except asyncio.CancelledError:
            return _selected_retrieval_cancelled_response(
                tool_name="query_selected_knowledge_files",
                query=normalized_query,
                original_query=original_query,
                retrieval_round=retrieval_round,
                normalized_timeout_seconds=normalized_timeout_seconds,
                descriptor=descriptor,
                normalized_required_anchors=normalized_required_anchors,
                anchor_policy=anchor_policy,
                evidence_need=effective_evidence_need,
                executed_query=normalized_query,
                query_enforced_from_original=bool(enforced_original_query),
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=len(candidates),
                requested_source_ids=requested_source_ids,
                active_source_scope=active_source_scope,
            )
        except Exception as exc:
            timeout_types = (TimeoutError, asyncio.TimeoutError)
            timeout = isinstance(exc, timeout_types)
            diagnostic = _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="retrieval_timeout" if timeout else "retrieval_provider_error",
                outcome="timeout" if timeout else "malformed",
                tool_name="query_selected_knowledge_files",
                query=normalized_query,
                retrieval_round=retrieval_round,
            )
            return finalize({
                "status": "timeout" if timeout else "error",
                "tool_name": "query_selected_knowledge_files",
                "code": "retrieval_timeout" if timeout else "retrieval_provider_error",
                "query": normalized_query,
                "original_query": original_query,
                "retrieval_round": retrieval_round,
                "retrieval_diagnostics": [diagnostic],
                "terminal_reason": "retrieval_timeout"
                if timeout
                else "retrieval_provider_error",
                "strategy_used": _selected_retrieval_with_strategy_metadata(
                    _selected_retrieval_strategy_used(
                        descriptor=descriptor,
                        normalized_required_anchors=normalized_required_anchors,
                        anchor_policy=anchor_policy,
                        evidence_need=effective_evidence_need,
                        original_query=original_query,
                        executed_query=normalized_query,
                        retrieval_round=retrieval_round,
                        query_enforced_from_original=bool(enforced_original_query),
                    ),
                    routing_strategy,
                ),
                "authorization_context": _selected_retrieval_authorization_context(
                    selected_inventory_count=len(selected_candidates),
                    scoped_inventory_count=len(candidates),
                    requested_source_ids=requested_source_ids,
                    active_source_scope=active_source_scope,
                ),
                "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
            })
    if not sources:
        return finalize({
            "status": "no_evidence",
            "tool_name": "query_selected_knowledge_files",
            "code": "empty_retrieval_result",
            "query": normalized_query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "retrieval_diagnostics": prefilter_diagnostics,
            "terminal_reason": "empty_retrieval_result",
            "provenance": _compatibility_provenance_payload(),
            "strategy_used": _selected_retrieval_with_strategy_metadata(
                _selected_retrieval_strategy_used(
                    descriptor=descriptor,
                    normalized_required_anchors=normalized_required_anchors,
                    anchor_policy=anchor_policy,
                    evidence_need=effective_evidence_need,
                    original_query=original_query,
                    executed_query=normalized_query,
                    retrieval_round=retrieval_round,
                    query_enforced_from_original=bool(enforced_original_query),
                ),
                routing_strategy,
            ),
            "authorization_context": _selected_retrieval_authorization_context(
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=len(candidates),
                requested_source_ids=requested_source_ids,
                active_source_scope=active_source_scope,
            ),
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

    sources, diagnostics = _filter_selected_retrieval_sources_by_query(
        sources,
        query=(
            " ".join(normalized_required_anchors or required_facets)
            if exact_candidates
            else normalized_query
        ),
        required_anchors=normalized_required_anchors,
        required_facets=required_facets,
        metadata_first_intent=bool(routing_strategy.get("metadata_first_intent")),
        allow_anchor_bundle_join=(
            str(routing_strategy.get("structured_fact_intent") or "").strip()
            in {"standard_value", "quota_table_row", "multi_field_slot_extraction"}
        ),
        allowed_identity_values=allowed_identity_values,
        tool_name="query_selected_knowledge_files",
        retrieval_round=retrieval_round,
    )
    diagnostics = _selected_retrieval_dedupe_diagnostics(
        [*prefilter_diagnostics, *diagnostics]
    )
    if routing_strategy.get("metadata_first_intent"):
        diagnostics = _selected_retrieval_dedupe_diagnostics(
            [
                *diagnostics,
                *_selected_retrieval_metadata_first_diagnostics(
                    query=normalized_query,
                    tool_name="query_selected_knowledge_files",
                    retrieval_round=retrieval_round,
                    routing_strategy=routing_strategy,
                    selected_inventory_count=len(selected_candidates),
                    scoped_inventory_count=len(candidates),
                    structured_terms=structured_terms,
                ),
            ]
        )
    unsupported_claim_reason = next(
        (
            str(item.get("reason") or "").strip()
            for item in diagnostics
            if isinstance(item, dict)
            and str(item.get("reason") or "").strip().startswith("unsupported_")
        ),
        "",
    )
    if routing_strategy.get("metadata_first_intent") and unsupported_claim_reason:
        strategy_used = _selected_retrieval_with_exact_candidates(
            _selected_retrieval_with_strategy_metadata(
                _selected_retrieval_strategy_used(
                    descriptor=descriptor,
                    normalized_required_anchors=normalized_required_anchors,
                    anchor_policy=anchor_policy,
                    evidence_need=effective_evidence_need,
                    original_query=original_query,
                    executed_query=normalized_query,
                    retrieval_round=retrieval_round,
                    query_enforced_from_original=bool(enforced_original_query),
                ),
                routing_strategy,
            ),
            exact_candidates,
        )
        return finalize(
            {
                "status": "no_evidence",
                "tool_name": "query_selected_knowledge_files",
                "code": unsupported_claim_reason,
                "query": normalized_query,
                "original_query": original_query,
                "retrieval_round": retrieval_round,
                "retrieval_diagnostics": diagnostics,
                "terminal_reason": unsupported_claim_reason,
                "provenance": _compatibility_provenance_payload(),
                "strategy_used": strategy_used,
                "authorization_context": _selected_retrieval_authorization_context(
                    selected_inventory_count=len(selected_candidates),
                    scoped_inventory_count=len(candidates),
                    requested_source_ids=requested_source_ids,
                    active_source_scope=active_source_scope,
                ),
                "retry_policy": _selected_retrieval_retry_policy(
                    normalized_timeout_seconds
                ),
            }
        )
    if not sources:
        strategy_used = _selected_retrieval_with_exact_candidates(
            _selected_retrieval_with_strategy_metadata(
                _selected_retrieval_strategy_used(
                    descriptor=descriptor,
                    normalized_required_anchors=normalized_required_anchors,
                    anchor_policy=anchor_policy,
                    evidence_need=effective_evidence_need,
                    original_query=original_query,
                    executed_query=normalized_query,
                    retrieval_round=retrieval_round,
                    query_enforced_from_original=bool(enforced_original_query),
                ),
                routing_strategy,
            ),
            exact_candidates,
        )
        return finalize({
            "status": "no_evidence",
            "tool_name": "query_selected_knowledge_files",
            "code": "weak_or_empty_retrieval_result",
            "query": normalized_query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "retrieval_diagnostics": diagnostics,
            "terminal_reason": "weak_or_empty_retrieval_result",
            "provenance": _compatibility_provenance_payload(),
            "strategy_used": strategy_used,
            "authorization_context": _selected_retrieval_authorization_context(
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=len(candidates),
                requested_source_ids=requested_source_ids,
                active_source_scope=active_source_scope,
            ),
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

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
                if original_query:
                    metadata.setdefault("original_query", original_query)
                metadata.setdefault(
                    "query_enforced_from_original",
                    bool(enforced_original_query),
                )

    compact_sources, context_budget = _compact_deepagent_reference_sources(sources)
    strategy_used = _selected_retrieval_with_exact_candidates(
        _selected_retrieval_with_strategy_metadata(
            _selected_retrieval_strategy_used(
                descriptor=descriptor,
                normalized_required_anchors=normalized_required_anchors,
                anchor_policy=anchor_policy,
                evidence_need=effective_evidence_need,
                original_query=original_query,
                executed_query=normalized_query,
                retrieval_round=retrieval_round,
                query_enforced_from_original=bool(enforced_original_query),
            ),
            routing_strategy,
        ),
        exact_candidates,
    )
    accepted_outputs = _selected_retrieval_accepted_outputs(
        compact_sources,
        tool_name="query_selected_knowledge_files",
        query=normalized_query,
        original_query=original_query,
        retrieval_round=retrieval_round,
    )
    precision_guard = _selected_retrieval_apply_structured_precision_guard(
        status="success",
        strategy_used=strategy_used,
        precision_mode=structured_precision_mode,
        query=normalized_query,
        original_query=original_query,
        retrieval_round=retrieval_round,
        tool_name="query_selected_knowledge_files",
        canonical_references=compact_sources,
        accepted_outputs=accepted_outputs,
        retrieval_diagnostics=diagnostics,
        context_budget=context_budget,
    )
    result = {
        "status": str(precision_guard.get("status") or "success"),
        "tool_name": "query_selected_knowledge_files",
        "query": normalized_query,
        "original_query": original_query,
        "query_enforced_from_original": bool(enforced_original_query),
        "retrieval_round": retrieval_round,
        "canonical_references": precision_guard.get("canonical_references") or [],
        "accepted_outputs": precision_guard.get("accepted_outputs") or [],
        "structured_fact_fields": precision_guard.get("structured_fact_fields") or [],
        "result_count": len(sources),
        "context_budget": context_budget,
        "strategy_used": strategy_used,
        "authorization_context": _selected_retrieval_authorization_context(
            selected_inventory_count=len(selected_candidates),
            scoped_inventory_count=len(candidates),
            requested_source_ids=requested_source_ids,
            active_source_scope=active_source_scope,
        ),
        "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        "terminal_reason": str(precision_guard.get("terminal_reason") or "success"),
        "provenance": _compatibility_provenance_payload(),
    }
    guarded_code = str(precision_guard.get("code") or "").strip()
    if guarded_code:
        result["code"] = guarded_code
    guarded_diagnostics = precision_guard.get("retrieval_diagnostics")
    if isinstance(guarded_diagnostics, list) and guarded_diagnostics:
        result["retrieval_diagnostics"] = guarded_diagnostics
    return finalize(result)


async def read_selected_file(
    source_id: str,
    query: str = "",
    retrieval_round: int = 1,
    original_query: str = "",
    required_anchors: Optional[list[str]] = None,
    evidence_need: str = "",
    timeout_seconds: Optional[float] = None,
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
    :param original_query: Optional original user query for deterministic round-1 execution.
    :param required_anchors: Optional strict anchors (used only when confidently required).
    :param evidence_need: Optional retrieval intent hint (for strategy/provenance only).
    :param timeout_seconds: Optional hard timeout for the provider call.
    """

    descriptor = _selected_retrieval_descriptor("read_selected_file")
    metadata = __metadata__ if isinstance(__metadata__, dict) else {}
    structured_precision_mode = _selected_retrieval_structured_precision_mode(metadata)
    normalized_source_id = str(source_id or "").strip()
    normalized_timeout_seconds = _selected_retrieval_timeout_seconds(
        timeout_seconds,
        request=__request__,
    )

    def finalize(response: dict[str, Any]) -> dict[str, Any]:
        return _selected_retrieval_worker_contract_lanes(
            response,
            tool_name="read_selected_file",
            normalized_timeout_seconds=normalized_timeout_seconds,
        )

    if not normalized_source_id:
        return finalize({
            "status": "malformed",
            "tool_name": "read_selected_file",
            "code": "missing_source_id",
            "message": "A selected source id is required.",
            "retrieval_round": retrieval_round,
            "terminal_reason": "missing_source_id",
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })
    if __request__ is None:
        return finalize({
            "status": "error",
            "tool_name": "read_selected_file",
            "code": "runtime_context_unavailable",
            "source_id": normalized_source_id,
            "retrieval_round": retrieval_round,
            "terminal_reason": "runtime_context_unavailable",
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

    active_source_scope = (
        metadata.get("active_source_scope")
        if isinstance(metadata.get("active_source_scope"), dict)
        else {}
    )
    normalized_query, original_query, enforced_original_query = (
        _selected_retrieval_effective_query(
            query,
            retrieval_round=retrieval_round,
            metadata=metadata,
            explicit_original_query=original_query,
        )
    )
    engine_authority_response = _selected_retrieval_engine_authority_response(
        metadata=metadata,
        tool_name="read_selected_file",
        normalized_timeout_seconds=normalized_timeout_seconds,
        query=normalized_query,
        original_query=original_query,
        retrieval_round=retrieval_round,
        source_id=normalized_source_id,
    )
    if engine_authority_response is not None:
        return engine_authority_response

    selected_candidates = _deepagent_selected_source_candidates(
        files=__files__,
        knowledge=__knowledge__,
        metadata=__metadata__,
    )
    scoped_candidates, scope_diagnostics = _selected_retrieval_scope_candidate_filter(
        selected_candidates,
        metadata,
        query=normalized_query or normalized_source_id,
        retrieval_round=retrieval_round,
        tool_name="read_selected_file",
    )
    scoped_candidates, authorization_diagnostics = (
        _selected_retrieval_filter_authorized_candidates(
            scoped_candidates,
            query=normalized_query or normalized_source_id,
            retrieval_round=retrieval_round,
            tool_name="read_selected_file",
        )
    )
    combined_scope_diagnostics = _selected_retrieval_dedupe_diagnostics(
        [*scope_diagnostics, *authorization_diagnostics]
    )
    if combined_scope_diagnostics and not scoped_candidates:
        return _selected_retrieval_blocked_response(
            descriptor=descriptor,
            tool_name="read_selected_file",
            query=normalized_query,
            original_query=original_query,
            retrieval_round=retrieval_round,
            normalized_timeout_seconds=normalized_timeout_seconds,
            diagnostics=combined_scope_diagnostics,
            selected_inventory_count=len(selected_candidates),
            scoped_inventory_count=len(scoped_candidates),
            requested_source_ids=[normalized_source_id],
            active_source_scope=active_source_scope,
            evidence_need=evidence_need,
            executed_query=normalized_query or normalized_source_id,
            query_enforced_from_original=bool(enforced_original_query),
            source_id=normalized_source_id,
        )

    selected_identity_values: set[str] = set()
    for candidate in selected_candidates:
        selected_identity_values.update(_selected_retrieval_source_identity_values(candidate))
    scoped_identity_values: set[str] = set()
    for candidate in scoped_candidates:
        scoped_identity_values.update(_selected_retrieval_source_identity_values(candidate))

    normalized_requested_id = _selected_retrieval_normalize_identifier(
        normalized_source_id
    )
    unknown_id = normalized_requested_id not in selected_identity_values
    out_of_scope_id = (
        normalized_requested_id in selected_identity_values
        and normalized_requested_id not in scoped_identity_values
    )
    if unknown_id or out_of_scope_id:
        diagnostics = [
            _selected_retrieval_compact_diagnostic(
                classification="no_evidence",
                reason="requested_source_out_of_scope",
                outcome="denied",
                tool_name="read_selected_file",
                query=normalized_query,
                retrieval_round=retrieval_round,
                detail={
                    "unknown_source_ids": [normalized_source_id] if unknown_id else [],
                    "out_of_scope_source_ids": (
                        [normalized_source_id] if out_of_scope_id else []
                    ),
                },
            )
        ]
        return finalize({
            "status": "permission_denied",
            "tool_name": "read_selected_file",
            "code": "requested_file_out_of_scope",
            "source_id": normalized_source_id,
            "query": normalized_query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "retrieval_diagnostics": diagnostics,
            "terminal_reason": "requested_file_out_of_scope",
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

    candidates = _filter_deepagent_sources_by_ids(scoped_candidates, [normalized_source_id])
    if len(candidates) != 1:
        return finalize({
            "status": "permission_denied" if not candidates else "malformed",
            "tool_name": "read_selected_file",
            "code": "requested_file_out_of_scope"
            if not candidates
            else "ambiguous_selected_source",
            "source_id": normalized_source_id,
            "query": normalized_query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "retrieval_diagnostics": combined_scope_diagnostics or [],
            "terminal_reason": "requested_file_out_of_scope"
            if not candidates
            else "ambiguous_selected_source",
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })
    allowed_identity_values = _selected_retrieval_source_identity_values(candidates[0])

    from open_webui.retrieval.utils import get_sources_from_items

    source_item = {**candidates[0], "context": "full"}
    provider_query = str(normalized_query or normalized_source_id).strip()
    normalized_required_anchors = _selected_retrieval_extract_confident_required_anchors(
        original_query or provider_query,
        metadata=metadata,
        explicit_required_anchors=required_anchors,
        candidates=candidates,
    )
    structured_terms = _selected_retrieval_structured_terms(provider_query)
    required_facets = list(
        dict.fromkeys(
            [
                *structured_terms.get("evidence_facets", []),
                *structured_terms.get("topic_terms", []),
                *structured_terms.get("requested_types", []),
            ]
        )
    )
    routing_strategy = _selected_retrieval_normalize_strategy(
        evidence_need=evidence_need,
        query=provider_query,
        required_anchors=normalized_required_anchors,
        source_ids=[normalized_source_id],
        active_source_scope=active_source_scope,
    )
    structured_plan = (
        routing_strategy.get("structured_fact_plan")
        if isinstance(routing_strategy.get("structured_fact_plan"), dict)
        else {}
    )
    structured_lookup_queries = [
        str(value)
        for value in routing_strategy.get("structured_query_variants") or []
        if str(value).strip()
    ]
    structured_lookup_enabled = bool(
        routing_strategy.get("structured_routing_allowed")
        and (
            structured_plan.get("has_exact_anchor_signal")
            or structured_plan.get("has_multi_field_signal")
        )
    )
    structured_lookup_anchors = _selected_retrieval_dedupe_terms(
        [
            *(structured_plan.get("hard_anchors") or []),
            *normalized_required_anchors,
        ]
    )
    anchor_policy = _selected_retrieval_anchor_policy(normalized_required_anchors)
    if (
        routing_strategy.get("metadata_first_intent")
        and not routing_strategy.get("semantic_chunk_lookup_ok")
    ):
        return finalize(
            {
                "status": "no_evidence",
                "tool_name": "read_selected_file",
                "code": "metadata_first_targeted_evidence_required",
                "source_id": normalized_source_id,
                "query": provider_query,
                "original_query": original_query,
                "retrieval_round": retrieval_round,
                "retrieval_diagnostics": _selected_retrieval_dedupe_diagnostics(
                    [
                        _selected_retrieval_compact_diagnostic(
                            classification="no_evidence",
                            reason="metadata_first_targeted_evidence_required",
                            outcome="blocked",
                            tool_name="read_selected_file",
                            query=provider_query,
                            retrieval_round=retrieval_round,
                        ),
                        *_selected_retrieval_metadata_first_diagnostics(
                            query=provider_query,
                            tool_name="read_selected_file",
                            retrieval_round=retrieval_round,
                            routing_strategy=routing_strategy,
                            selected_inventory_count=len(selected_candidates),
                            scoped_inventory_count=len(candidates),
                            structured_terms=structured_terms,
                        ),
                    ]
                ),
                "terminal_reason": "metadata_first_targeted_evidence_required",
                "strategy_used": _selected_retrieval_with_strategy_metadata(
                    _selected_retrieval_strategy_used(
                        descriptor=descriptor,
                        normalized_required_anchors=normalized_required_anchors,
                        anchor_policy=anchor_policy,
                        evidence_need=evidence_need,
                        original_query=original_query,
                        executed_query=provider_query,
                        retrieval_round=retrieval_round,
                        query_enforced_from_original=bool(enforced_original_query),
                    ),
                    routing_strategy,
                ),
                "authorization_context": _selected_retrieval_authorization_context(
                    selected_inventory_count=len(selected_candidates),
                    scoped_inventory_count=len(candidates),
                    requested_source_ids=[normalized_source_id],
                    active_source_scope=active_source_scope,
                ),
                "retry_policy": _selected_retrieval_retry_policy(
                    normalized_timeout_seconds
                ),
            }
        )
    provider_queries = [provider_query]
    prefilter_diagnostics: list[dict[str, Any]] = []
    exact_candidates: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if structured_lookup_enabled:
        provider_queries = structured_lookup_queries or [provider_query]
        exact_lookup_result = await _selected_retrieval_run_local_exact_lookup(
            get_sources_from_items=get_sources_from_items,
            request=__request__,
            user_model=__user_model__,
            candidates=[source_item],
            queries=provider_queries,
            anchors=structured_lookup_anchors,
            timeout_seconds=normalized_timeout_seconds,
            tool_name="read_selected_file",
            query=provider_query,
            retrieval_round=retrieval_round,
            allowed_identity_values=allowed_identity_values,
        )
        prefilter_diagnostics.extend(exact_lookup_result.get("diagnostics") or [])
        exact_candidates = [
            item
            for item in exact_lookup_result.get("exact_candidates") or []
            if isinstance(item, dict)
        ]
        sources = [
            item
            for item in exact_lookup_result.get("sources") or []
            if isinstance(item, dict)
        ]

    if not sources:
        try:
            provider_call = get_sources_from_items(
                request=__request__,
                items=[source_item],
                queries=provider_queries,
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
            sources = await asyncio.wait_for(
                provider_call,
                timeout=normalized_timeout_seconds,
            )
        except asyncio.CancelledError:
            return _selected_retrieval_cancelled_response(
                tool_name="read_selected_file",
                query=provider_query,
                original_query=original_query,
                retrieval_round=retrieval_round,
                normalized_timeout_seconds=normalized_timeout_seconds,
                descriptor=descriptor,
                normalized_required_anchors=normalized_required_anchors,
                anchor_policy=anchor_policy,
                evidence_need=evidence_need,
                executed_query=provider_query,
                query_enforced_from_original=bool(enforced_original_query),
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=len(candidates),
                requested_source_ids=[normalized_source_id],
                active_source_scope=active_source_scope,
                source_id=normalized_source_id,
            )
        except Exception as exc:
            timeout_types = (TimeoutError, asyncio.TimeoutError)
            timeout = isinstance(exc, timeout_types)
            diagnostic = _selected_retrieval_compact_diagnostic(
                classification="diagnostics",
                reason="retrieval_timeout" if timeout else "retrieval_provider_error",
                outcome="timeout" if timeout else "malformed",
                tool_name="read_selected_file",
                query=provider_query,
                retrieval_round=retrieval_round,
                source_id=normalized_source_id,
            )
            return finalize({
                "status": "timeout" if timeout else "error",
                "tool_name": "read_selected_file",
                "code": "retrieval_timeout" if timeout else "retrieval_provider_error",
                "source_id": normalized_source_id,
                "query": provider_query,
                "original_query": original_query,
                "retrieval_round": retrieval_round,
                "retrieval_diagnostics": [diagnostic],
                "terminal_reason": "retrieval_timeout"
                if timeout
                else "retrieval_provider_error",
                "strategy_used": _selected_retrieval_strategy_used(
                    descriptor=descriptor,
                    normalized_required_anchors=normalized_required_anchors,
                    anchor_policy=anchor_policy,
                    evidence_need=evidence_need,
                    original_query=original_query,
                    executed_query=provider_query,
                    retrieval_round=retrieval_round,
                    query_enforced_from_original=bool(enforced_original_query),
                ),
                "authorization_context": _selected_retrieval_authorization_context(
                    selected_inventory_count=len(selected_candidates),
                    scoped_inventory_count=len(candidates),
                    requested_source_ids=[normalized_source_id],
                    active_source_scope=active_source_scope,
                ),
                "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
            })
    if not sources:
        return finalize({
            "status": "no_evidence",
            "tool_name": "read_selected_file",
            "code": "empty_retrieval_result",
            "source_id": normalized_source_id,
            "query": provider_query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "retrieval_diagnostics": prefilter_diagnostics,
            "terminal_reason": "empty_retrieval_result",
            "strategy_used": _selected_retrieval_strategy_used(
                descriptor=descriptor,
                normalized_required_anchors=normalized_required_anchors,
                anchor_policy=anchor_policy,
                evidence_need=evidence_need,
                original_query=original_query,
                executed_query=provider_query,
                retrieval_round=retrieval_round,
                query_enforced_from_original=bool(enforced_original_query),
            ),
            "authorization_context": _selected_retrieval_authorization_context(
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=len(candidates),
                requested_source_ids=[normalized_source_id],
                active_source_scope=active_source_scope,
            ),
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

    sources, diagnostics = _filter_selected_retrieval_sources_by_query(
        sources,
        query=(
            " ".join(normalized_required_anchors or required_facets)
            if exact_candidates
            else provider_query
        ),
        required_anchors=normalized_required_anchors,
        required_facets=required_facets,
        metadata_first_intent=bool(routing_strategy.get("metadata_first_intent")),
        allow_anchor_bundle_join=(
            str(routing_strategy.get("structured_fact_intent") or "").strip()
            in {"standard_value", "quota_table_row", "multi_field_slot_extraction"}
        ),
        allowed_identity_values=allowed_identity_values,
        tool_name="read_selected_file",
        retrieval_round=retrieval_round,
    )
    diagnostics = _selected_retrieval_dedupe_diagnostics(
        [*prefilter_diagnostics, *diagnostics]
    )
    if routing_strategy.get("metadata_first_intent"):
        diagnostics = _selected_retrieval_dedupe_diagnostics(
            [
                *diagnostics,
                *_selected_retrieval_metadata_first_diagnostics(
                    query=provider_query,
                    tool_name="read_selected_file",
                    retrieval_round=retrieval_round,
                    routing_strategy=routing_strategy,
                    selected_inventory_count=len(selected_candidates),
                    scoped_inventory_count=len(candidates),
                    structured_terms=structured_terms,
                ),
            ]
        )
    unsupported_claim_reason = next(
        (
            str(item.get("reason") or "").strip()
            for item in diagnostics
            if isinstance(item, dict)
            and str(item.get("reason") or "").strip().startswith("unsupported_")
        ),
        "",
    )
    if routing_strategy.get("metadata_first_intent") and unsupported_claim_reason:
        strategy_used = _selected_retrieval_with_exact_candidates(
            _selected_retrieval_with_strategy_metadata(
                _selected_retrieval_strategy_used(
                    descriptor=descriptor,
                    normalized_required_anchors=normalized_required_anchors,
                    anchor_policy=anchor_policy,
                    evidence_need=evidence_need,
                    original_query=original_query,
                    executed_query=provider_query,
                    retrieval_round=retrieval_round,
                    query_enforced_from_original=bool(enforced_original_query),
                ),
                routing_strategy,
            ),
            exact_candidates,
        )
        return finalize(
            {
                "status": "no_evidence",
                "tool_name": "read_selected_file",
                "code": unsupported_claim_reason,
                "source_id": normalized_source_id,
                "query": provider_query,
                "original_query": original_query,
                "retrieval_round": retrieval_round,
                "retrieval_diagnostics": diagnostics,
                "terminal_reason": unsupported_claim_reason,
                "strategy_used": strategy_used,
                "authorization_context": _selected_retrieval_authorization_context(
                    selected_inventory_count=len(selected_candidates),
                    scoped_inventory_count=len(candidates),
                    requested_source_ids=[normalized_source_id],
                    active_source_scope=active_source_scope,
                ),
                "retry_policy": _selected_retrieval_retry_policy(
                    normalized_timeout_seconds
                ),
            }
        )
    if not sources:
        strategy_used = _selected_retrieval_with_exact_candidates(
            _selected_retrieval_with_strategy_metadata(
                _selected_retrieval_strategy_used(
                    descriptor=descriptor,
                    normalized_required_anchors=normalized_required_anchors,
                    anchor_policy=anchor_policy,
                    evidence_need=evidence_need,
                    original_query=original_query,
                    executed_query=provider_query,
                    retrieval_round=retrieval_round,
                    query_enforced_from_original=bool(enforced_original_query),
                ),
                routing_strategy,
            ),
            exact_candidates,
        )
        return finalize({
            "status": "no_evidence",
            "tool_name": "read_selected_file",
            "code": "weak_or_empty_retrieval_result",
            "source_id": normalized_source_id,
            "query": provider_query,
            "original_query": original_query,
            "retrieval_round": retrieval_round,
            "retrieval_diagnostics": diagnostics,
            "terminal_reason": "weak_or_empty_retrieval_result",
            "strategy_used": strategy_used,
            "authorization_context": _selected_retrieval_authorization_context(
                selected_inventory_count=len(selected_candidates),
                scoped_inventory_count=len(candidates),
                requested_source_ids=[normalized_source_id],
                active_source_scope=active_source_scope,
            ),
            "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        })

    for source in sources:
        if not isinstance(source, dict):
            continue
        for metadata in source.get("metadata") or []:
            if isinstance(metadata, dict):
                metadata.setdefault("retrieval_tool_name", "read_selected_file")
                metadata.setdefault("retrieval_round", retrieval_round)
                metadata.setdefault("query", provider_query)
                if original_query:
                    metadata.setdefault("original_query", original_query)
                metadata.setdefault(
                    "query_enforced_from_original",
                    bool(enforced_original_query),
                )

    compact_sources, context_budget = _compact_deepagent_reference_sources(
        sources,
        max_sources=int(descriptor.get("max_sources") or 1),
        max_chunks=int(descriptor.get("max_chunks") or 8),
        max_chars_per_chunk=int(descriptor.get("max_chars_per_chunk") or DEEPAGENT_READ_MAX_CHARS_PER_CHUNK),
        max_total_chars=int(descriptor.get("max_total_chars") or DEEPAGENT_READ_MAX_TOTAL_CHARS),
    )
    strategy_used = _selected_retrieval_with_exact_candidates(
        _selected_retrieval_with_strategy_metadata(
            _selected_retrieval_strategy_used(
                descriptor=descriptor,
                normalized_required_anchors=normalized_required_anchors,
                anchor_policy=anchor_policy,
                evidence_need=evidence_need,
                original_query=original_query,
                executed_query=provider_query,
                retrieval_round=retrieval_round,
                query_enforced_from_original=bool(enforced_original_query),
            ),
            routing_strategy,
        ),
        exact_candidates,
    )
    accepted_outputs = _selected_retrieval_accepted_outputs(
        compact_sources,
        tool_name="read_selected_file",
        query=provider_query,
        original_query=original_query,
        retrieval_round=retrieval_round,
    )
    precision_guard = _selected_retrieval_apply_structured_precision_guard(
        status="success",
        strategy_used=strategy_used,
        precision_mode=structured_precision_mode,
        query=provider_query,
        original_query=original_query,
        retrieval_round=retrieval_round,
        tool_name="read_selected_file",
        canonical_references=compact_sources,
        accepted_outputs=accepted_outputs,
        retrieval_diagnostics=diagnostics,
        context_budget=context_budget,
    )
    result = {
        "status": str(precision_guard.get("status") or "success"),
        "tool_name": "read_selected_file",
        "source_id": normalized_source_id,
        "query": provider_query,
        "original_query": original_query,
        "query_enforced_from_original": bool(enforced_original_query),
        "retrieval_round": retrieval_round,
        "canonical_references": precision_guard.get("canonical_references") or [],
        "accepted_outputs": precision_guard.get("accepted_outputs") or [],
        "structured_fact_fields": precision_guard.get("structured_fact_fields") or [],
        "result_count": len(sources),
        "context_budget": context_budget,
        "strategy_used": strategy_used,
        "authorization_context": _selected_retrieval_authorization_context(
            selected_inventory_count=len(selected_candidates),
            scoped_inventory_count=len(candidates),
            requested_source_ids=[normalized_source_id],
            active_source_scope=active_source_scope,
        ),
        "retry_policy": _selected_retrieval_retry_policy(normalized_timeout_seconds),
        "terminal_reason": str(precision_guard.get("terminal_reason") or "success"),
    }
    guarded_code = str(precision_guard.get("code") or "").strip()
    if guarded_code:
        result["code"] = guarded_code
    guarded_diagnostics = precision_guard.get("retrieval_diagnostics")
    if isinstance(guarded_diagnostics, list) and guarded_diagnostics:
        result["retrieval_diagnostics"] = guarded_diagnostics
    return finalize(result)


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
