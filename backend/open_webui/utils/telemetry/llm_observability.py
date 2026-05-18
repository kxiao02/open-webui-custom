import json
import logging
import os
from collections import Counter
from typing import Any


log = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _observability_enabled() -> bool:
    return (
        os.getenv("OPEN_WEBUI_LLM_OBSERVABILITY", "").strip().lower()
        in _TRUE_VALUES
    )


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def diagnostic_reason_codes(diagnostics: Any) -> list[str]:
    if not isinstance(diagnostics, list):
        return []

    reasons: set[str] = set()
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        reason = (
            _safe_text(diagnostic.get("reason"))
            or _safe_text(diagnostic.get("reason_code"))
            or _safe_text(diagnostic.get("code"))
        )
        if reason:
            reasons.add(reason)
    return sorted(reasons)


def diagnostic_classification_counts(diagnostics: Any) -> dict[str, int]:
    if not isinstance(diagnostics, list):
        return {}

    counts: Counter[str] = Counter()
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        classification = (
            _safe_text(diagnostic.get("classification"))
            or _safe_text(diagnostic.get("status"))
            or "unknown"
        )
        counts[classification] += 1
    return dict(sorted(counts.items()))


def summarize_source_scope(scope: Any) -> dict[str, Any]:
    if not isinstance(scope, dict) or not scope:
        return {
            "scope_status": "none",
            "scope_source_set_mode": "",
            "scope_reason": "",
            "scope_confidence": "",
            "scope_source_count": 0,
        }

    source_ids = scope.get("source_ids")
    sources = scope.get("sources")
    if isinstance(source_ids, list):
        source_count = len(source_ids)
    elif isinstance(sources, list):
        source_count = len(sources)
    else:
        source_count = 0

    return {
        "scope_status": _safe_text(scope.get("status")),
        "scope_source_set_mode": _safe_text(scope.get("source_set_mode")),
        "scope_reason": _safe_text(scope.get("reason")),
        "scope_confidence": _safe_text(scope.get("confidence")),
        "scope_source_count": source_count,
    }


def observe_llm_event(event_name: str, payload: Any | None = None) -> None:
    if not _observability_enabled():
        return

    try:
        serialized = json.dumps(payload or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = json.dumps({"unserializable": True}, ensure_ascii=False)
    log.info("llm_observability event=%s payload=%s", event_name, serialized)
