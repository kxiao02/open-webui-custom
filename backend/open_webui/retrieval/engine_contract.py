"""Open WebUI retrieval-engine contract helpers for selected-source bridging."""

from __future__ import annotations

import copy
from typing import Any


_RETRIEVAL_ENGINE_OBSERVE_RAW_PATH_KEYS = {
    "absolute_path",
    "file_path",
    "fs_path",
    "local_path",
    "path",
    "raw_path",
    "server_path",
    "source_path",
    "tmp_path",
    "upload_path",
}


def _retrieval_engine_authority_state(result: Any) -> str:
    status = str(getattr(result, "status", "") or "").strip().lower()
    accepted_outputs = getattr(result, "accepted_outputs", ()) or ()
    references = getattr(result, "references", ()) or ()
    if status in {"success", "partial"} and accepted_outputs and references:
        return "authoritative"
    if status in {"denied", "blocked"} or _retrieval_engine_result_has_scope_failure(
        result
    ):
        return "fail_closed"
    return "diagnostics"


def _retrieval_engine_runtime_mode(authority_state: Any) -> str:
    state = str(authority_state or "").strip().lower()
    if state == "authoritative":
        return "retrieval_engine_authority"
    if state == "fail_closed":
        return "retrieval_engine_fail_closed"
    if state == "diagnostics":
        return "retrieval_engine_diagnostics"
    return "compatibility_fallback"


def _retrieval_engine_bypass_reason(authority_state: Any) -> str:
    state = str(authority_state or "").strip().lower()
    if state == "authoritative":
        return "retrieval_engine_authority_succeeded"
    if state == "fail_closed":
        return "retrieval_engine_fail_closed"
    if state == "diagnostics":
        return "retrieval_engine_diagnostics_only"
    return "compatibility_fallback"


def _retrieval_engine_result_has_scope_failure(result: Any) -> bool:
    for diagnostic in getattr(result, "diagnostics", ()) or ():
        code = str(getattr(diagnostic, "code", "") or "").strip().lower()
        details = getattr(diagnostic, "details", {})
        failed_gates = (
            details.get("failed_gates", ()) if isinstance(details, dict) else ()
        )
        if code in {"source_scope_violation", "source_identity_mismatch"}:
            return True
        if any(
            str(gate) in {"source_scope", "source_identity"} for gate in failed_gates
        ):
            return True
    return False


def _retrieval_engine_result_first_pass_contract(
    *,
    result: Any,
    adapter_diagnostics: Any,
    selected_files: list[dict],
    diagnostics: list[dict[str, Any]],
    plan_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    status = str(getattr(result, "status", "") or "error").strip().lower()
    terminal_reason = str(getattr(result, "terminal_reason", "") or "success").strip()
    if status != "success" and terminal_reason == "success":
        terminal_reason = status
    authority_state = _retrieval_engine_authority_state(result)
    runtime_mode = _retrieval_engine_runtime_mode(authority_state)
    bypass_reason = _retrieval_engine_bypass_reason(authority_state)
    source_by_id = {
        str(file_item.get("id") or "").strip(): file_item
        for file_item in selected_files
        if isinstance(file_item, dict) and str(file_item.get("id") or "").strip()
    }
    references, source_cards = _retrieval_engine_reference_dicts(result, source_by_id)
    accepted_outputs = _retrieval_engine_accepted_output_dicts(result, references)
    contract_diagnostics = _retrieval_engine_contract_diagnostics(
        result,
        adapter_diagnostics,
    )
    authorization_context = _retrieval_engine_authorization_context(
        result,
        selected_files,
    )
    context_budget = _retrieval_engine_context_budget(result)
    compact_budget = _retrieval_engine_compact_budget_provenance(context_budget)
    return {
        "accepted_outputs": accepted_outputs,
        "references": references,
        "sources": source_cards,
        "diagnostics": contract_diagnostics,
        "status": status,
        "terminal_reason": terminal_reason,
        "authorization_context": authorization_context,
        "retry_policy": _retrieval_engine_retry_policy(result),
        "context_budget": context_budget,
        "provenance": {
            "worker_kind": "selected_source_retrieval",
            "tool_name": "retrieval_engine_selected_source_retrieval",
            "status": status,
            "terminal_reason": terminal_reason,
            "engine_authority": authority_state == "authoritative",
            "middleware_strategy_bypassed": True,
            "middleware_strategy_bypass_reason": bypass_reason,
            "selected_source_runtime_mode": runtime_mode,
            "engine_owned": authority_state != "fallback",
            "compatibility_fallback": authority_state == "fallback",
            "context_budget": copy.deepcopy(context_budget),
            "plan_summary": plan_summary or {},
            "reason_codes": [
                item.get("reason")
                for item in contract_diagnostics
                if isinstance(item, dict) and item.get("reason")
            ],
            "compact_retrieval_provenance": {
                "runtime_mode": runtime_mode,
                "engine_owned": authority_state != "fallback",
                "engine_authority": authority_state == "authoritative",
                "middleware_strategy_bypassed": True,
                "strategy": {
                    "evidence_shape": (plan_summary or {}).get("evidence_shape") or "",
                    "required_capabilities": list(
                        (plan_summary or {}).get("required_capabilities") or []
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
                    "reference_count": len(references),
                    "accepted_output_count": len(accepted_outputs),
                },
                "budget": compact_budget,
                "material_limitations": [
                    item.get("reason")
                    for item in contract_diagnostics
                    if isinstance(item, dict) and item.get("classification") != "phase"
                ],
            },
        },
        "observe_diagnostics": diagnostics[:12],
    }


def _retrieval_engine_reference_dicts(
    result: Any,
    source_by_id: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    outputs = list(getattr(result, "accepted_outputs", ()) or ())
    references = list(getattr(result, "references", ()) or ())
    reference_dicts: list[dict] = []
    source_cards: list[dict] = []
    for index, reference in enumerate(references):
        source_id = str(getattr(reference, "source_id", "") or "").strip()
        if not source_id:
            continue
        source_file = source_by_id.get(source_id, {})
        label = str(
            getattr(reference, "label", "")
            or source_file.get("filename")
            or source_file.get("name")
            or source_id
        )
        output = outputs[index] if index < len(outputs) else None
        snippet = str(getattr(output, "text", "") or "").strip()
        if not snippet:
            continue
        output_id = str(getattr(output, "output_id", "") or "").strip()
        reference_id = str(getattr(reference, "reference_id", "") or "").strip()
        source_anchor = _retrieval_engine_observe_safe_mapping(
            getattr(reference, "source_anchor", {}) or {}
        )
        location_metadata = _retrieval_engine_reference_location_metadata(
            source_id=source_id,
            reference_id=reference_id,
            output_id=output_id,
            source_anchor=source_anchor,
            degraded_location=bool(getattr(reference, "degraded_location", False)),
        )
        source = {
            "id": source_id,
            "name": label,
            "type": str(source_file.get("type") or "file"),
            "url": f"/api/v1/files/{source_id}/content",
        }
        metadata = {
            "source": source_id,
            "name": label,
            "file_id": source_id,
            "retrieval_engine_reference_id": reference_id,
            "retrieval_engine_output_id": output_id,
            "engine_authority": True,
            **location_metadata,
        }
        reference_dict = {
            "source": source,
            "document": [snippet],
            "metadata": [metadata],
            "provenance": {
                "tool_name": "retrieval_engine_selected_source_retrieval",
                "retrieval_round": 1,
                "engine_authority": True,
                "reference_id": reference_id,
                "output_id": output_id,
                "source_anchor": source_anchor,
                "degraded_location": bool(getattr(reference, "degraded_location", False)),
            },
        }
        reference_dicts.append(_retrieval_engine_observe_safe_value(reference_dict))
        source_cards.append(_retrieval_engine_observe_safe_value(reference_dict))
    return reference_dicts, source_cards


def _retrieval_engine_reference_location_metadata(
    *,
    source_id: str,
    reference_id: str,
    output_id: str,
    source_anchor: dict[str, Any],
    degraded_location: bool,
) -> dict[str, Any]:
    anchor = source_anchor if isinstance(source_anchor, dict) else {}
    metadata: dict[str, Any] = {
        "source_id": source_id,
        "reference_id": reference_id,
        "output_id": output_id,
        "source_anchor": anchor,
    }
    anchor_kind = str(anchor.get("kind") or "").strip()
    if anchor_kind:
        metadata["source_anchor_kind"] = anchor_kind

    for key in (
        "chunk_id",
        "chunk",
        "chunk_index",
        "page",
        "text_span",
        "span",
        "start",
        "end",
        "sheet",
        "range",
        "cell",
        "table",
        "row",
        "field",
        "fields",
        "provider_region",
        "region",
        "region_id",
        "source_id",
    ):
        value = anchor.get(key)
        if value not in (None, "", [], {}):
            metadata[key] = value

    if "text_span" not in metadata:
        start = anchor.get("start")
        end = anchor.get("end")
        if start not in (None, "") and end not in (None, ""):
            metadata["text_span"] = {"start": start, "end": end}

    has_exact_geometry = _retrieval_engine_anchor_has_exact_geometry(anchor)
    explicit_degraded = bool(degraded_location or not has_exact_geometry)
    metadata["degraded_location"] = explicit_degraded
    metadata["source_location"] = {
        "state": "degraded" if explicit_degraded else "anchored",
        "degraded": explicit_degraded,
        "reason": (
            "engine_marked_degraded"
            if degraded_location
            else "exact_geometry_unavailable"
            if not has_exact_geometry
            else "source_anchor_available"
        ),
        "has_exact_geometry": has_exact_geometry,
    }
    if anchor_kind:
        metadata["source_location"]["anchor_kind"] = anchor_kind
    return _retrieval_engine_observe_safe_mapping(metadata)


def _retrieval_engine_anchor_has_exact_geometry(source_anchor: dict[str, Any]) -> bool:
    anchor = source_anchor if isinstance(source_anchor, dict) else {}
    if anchor.get("page") not in (None, ""):
        return True
    if anchor.get("text_span") not in (None, "", [], {}):
        return True
    if anchor.get("span") not in (None, "", [], {}):
        return True
    if anchor.get("start") not in (None, "") and anchor.get("end") not in (None, ""):
        return True
    if anchor.get("sheet") not in (None, "") and (
        anchor.get("range") not in (None, "") or anchor.get("cell") not in (None, "")
    ):
        return True
    if anchor.get("table") not in (None, "") and anchor.get("row") not in (None, ""):
        return True
    if (
        anchor.get("provider_region") not in (None, "")
        or anchor.get("region") not in (None, "")
        or anchor.get("region_id") not in (None, "")
    ):
        return True
    return False


def _retrieval_engine_accepted_output_dicts(
    result: Any,
    references: list[dict],
) -> list[dict]:
    accepted: list[dict] = []
    outputs = list(getattr(result, "accepted_outputs", ()) or ())
    for index, output in enumerate(outputs):
        reference = references[index] if index < len(references) else {}
        source = (
            reference.get("source")
            if isinstance(reference.get("source"), dict)
            else {}
        )
        reference_metadata = (
            reference.get("metadata")[0]
            if isinstance(reference.get("metadata"), list)
            and reference.get("metadata")
            and isinstance(reference.get("metadata")[0], dict)
            else {}
        )
        snippet = str(getattr(output, "text", "") or "").strip()
        if not snippet or not source:
            continue
        accepted.append(
            _retrieval_engine_observe_safe_value(
                {
                    "output_id": str(getattr(output, "output_id", "") or ""),
                    "type": "selected_source_evidence",
                    "source": {
                        "id": str(source.get("id") or ""),
                        "name": str(source.get("name") or ""),
                        "type": str(source.get("type") or "file"),
                    },
                    "snippet": snippet,
                    "field_states": dict(getattr(output, "field_states", {}) or {}),
                    "provenance": {
                        "tool_name": "retrieval_engine_selected_source_retrieval",
                        "retrieval_round": 1,
                        "engine_authority": True,
                        "output_id": str(getattr(output, "output_id", "") or ""),
                        "reference_id": str(
                            reference_metadata.get("retrieval_engine_reference_id") or ""
                        ),
                        "source_anchor": _retrieval_engine_observe_safe_mapping(
                            reference_metadata.get("source_anchor") or {}
                        ),
                        "evidence_bundle_ids": list(
                            getattr(output, "evidence_bundle_ids", ()) or ()
                        ),
                    },
                }
            )
        )
    return accepted


def _retrieval_engine_active_source_scope_state(result: Any) -> str:
    scope = getattr(getattr(result, "request", None), "scope", None)
    decision = str(getattr(scope, "decision", "") or "").strip().lower()
    if decision == "authorized":
        return "resolved"
    return decision or "none"


def _retrieval_engine_authorization_context(
    result: Any,
    selected_files: list[dict],
) -> dict[str, Any]:
    scope = getattr(getattr(result, "request", None), "scope", None)
    request = getattr(result, "request", None)
    execution_context = getattr(request, "execution_context", None)
    if not isinstance(execution_context, dict):
        execution_context = {}

    selected_source_ids: list[str] = []
    for file_item in selected_files:
        if not isinstance(file_item, dict):
            continue
        file_id = str(file_item.get("id") or "").strip()
        if file_id and file_id not in selected_source_ids:
            selected_source_ids.append(file_id)

    scoped_source_ids = [
        str(source_id)
        for source_id in (getattr(scope, "source_ids", ()) or ())
        if str(source_id).strip()
    ]
    requested_source_ids = scoped_source_ids or list(selected_source_ids)
    authorization_reason = str(getattr(scope, "reason", "") or "").strip()
    authorization_generation = str(
        execution_context.get("authorization_generation") or ""
    ).strip()

    context: dict[str, Any] = {
        "selected_inventory_count": len(selected_source_ids),
        "scoped_inventory_count": len(requested_source_ids),
        "requested_source_ids": requested_source_ids,
        "active_source_scope_state": _retrieval_engine_active_source_scope_state(result),
        "active_source_scope": {"source_ids": requested_source_ids},
        "authorization_decision": str(getattr(scope, "decision", "") or "").strip()
        or "none",
    }
    if authorization_reason:
        context["authorization_reason"] = authorization_reason
    if authorization_generation:
        context["authorization_generation"] = authorization_generation
    return _retrieval_engine_observe_safe_mapping(context)


def _retrieval_engine_context_budget(result: Any) -> dict[str, Any]:
    request = getattr(result, "request", None)
    execution_context = getattr(request, "execution_context", None)
    if not isinstance(execution_context, dict):
        execution_context = {}
    budget = getattr(result, "budget", None)
    if not isinstance(budget, dict):
        budget = {}

    source_cards = budget.get("source_cards")
    if not isinstance(source_cards, (list, tuple)):
        source_cards = ()
    ordinary_prompt_context = budget.get("ordinary_prompt_context")
    if not isinstance(ordinary_prompt_context, (list, tuple)):
        ordinary_prompt_context = ()

    truncated_accepted_bundle_count = int(
        budget.get("truncated_accepted_bundles") or 0
    )
    omitted_accepted_bundle_count = int(budget.get("omitted_accepted_bundles") or 0)
    context_budget = {
        "requested_budget_tokens": int(
            budget.get("requested_tokens")
            or getattr(request, "requested_budget_tokens", 0)
            or 0
        ),
        "estimated_used_tokens": int(budget.get("estimated_used_tokens") or 0),
        "accepted_output_count": int(
            budget.get("accepted_outputs")
            or len(getattr(result, "accepted_outputs", ()) or ())
        ),
        "reference_count": int(
            budget.get("references") or len(getattr(result, "references", ()) or ())
        ),
        "candidate_accepted_bundle_count": int(
            budget.get("candidate_accepted_bundles")
            or len(getattr(result, "evidence_bundles", ()) or ())
        ),
        "truncated_accepted_bundle_count": truncated_accepted_bundle_count,
        "omitted_accepted_bundle_count": omitted_accepted_bundle_count,
        "truncated": bool(
            truncated_accepted_bundle_count
            or omitted_accepted_bundle_count
            or str(getattr(result, "terminal_reason", "") or "").strip().lower()
            == "budget_limited"
        ),
        "budget": {
            "policy": str(budget.get("policy") or "").strip(),
            "budget_policy": str(
                execution_context.get("budget_policy") or ""
            ).strip(),
            "return_policy": str(
                execution_context.get("return_policy") or ""
            ).strip(),
            "ranking_policy": str(
                execution_context.get("ranking_policy") or ""
            ).strip(),
            "engine_version": str(
                execution_context.get("engine_version") or ""
            ).strip(),
            "authorization_generation": str(
                execution_context.get("authorization_generation") or ""
            ).strip(),
            "cache_eligible": bool(budget.get("cache_eligible"))
            if "cache_eligible" in budget
            else False,
            "cache_miss_reason": str(budget.get("cache_miss_reason") or "").strip(),
            "cache_missing_dimensions": [
                str(item)
                for item in (budget.get("cache_missing_dimensions") or ())
                if str(item).strip()
            ],
            "source_card_count": len(source_cards),
            "ordinary_prompt_context_count": len(ordinary_prompt_context),
        },
    }
    return _retrieval_engine_observe_safe_mapping(context_budget)


def _retrieval_engine_compact_budget_provenance(
    context_budget: dict[str, Any],
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
    return _retrieval_engine_observe_safe_mapping(compact)


def _retrieval_engine_contract_diagnostics(
    result: Any,
    adapter_diagnostics: Any,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    status = str(getattr(result, "status", "") or "").strip().lower()
    terminal_reason = (
        str(getattr(result, "terminal_reason", "") or "").strip()
        or status
        or "unknown"
    )
    authority_state = _retrieval_engine_authority_state(result)
    runtime_mode = _retrieval_engine_runtime_mode(authority_state)
    for diagnostic in [
        *(adapter_diagnostics or ()),
        *(getattr(result, "diagnostics", ()) or ()),
    ]:
        code = str(getattr(diagnostic, "code", "") or "").strip()
        if not code:
            continue
        kind = str(getattr(diagnostic, "kind", "") or "").strip()
        classification = "phase" if kind == "phase" else "diagnostics"
        if status in {
            "no_evidence",
            "blocked",
            "denied",
            "timeout",
            "malformed",
            "error",
        }:
            classification = "no_evidence" if kind != "phase" else "phase"
        diagnostics.append(
            _retrieval_engine_observe_safe_value(
                {
                    "kind": "retrieval_engine",
                    "classification": classification,
                    "reason": code,
                    "outcome": status or "unknown",
                    "candidate_index": -1,
                    "provenance": {
                        "worker_kind": "selected_source_retrieval",
                        "tool_name": "retrieval_engine_selected_source_retrieval",
                        "status": status or "unknown",
                        "terminal_reason": terminal_reason,
                        "selected_source_runtime_mode": runtime_mode,
                        "engine_authority_state": authority_state,
                        "engine_owned": authority_state != "fallback",
                        "engine_authority": authority_state == "authoritative",
                        "compatibility_fallback": authority_state == "fallback",
                    },
                    "detail": {
                        "severity": str(getattr(diagnostic, "severity", "") or ""),
                        "phase": str(getattr(diagnostic, "phase", "") or ""),
                        "source_id": str(getattr(diagnostic, "source_id", "") or ""),
                        "candidate_id": str(
                            getattr(diagnostic, "candidate_id", "") or ""
                        ),
                        "details": _retrieval_engine_observe_safe_mapping(
                            getattr(diagnostic, "details", {}) or {}
                        ),
                    },
                }
            )
        )
    return diagnostics[:24]


def _retrieval_engine_retry_policy(result: Any) -> dict[str, Any]:
    retry_policy = getattr(getattr(result, "request", None), "retry_policy", None)
    return {
        "max_retries": max(int(getattr(retry_policy, "max_attempts", 1) or 1) - 1, 0),
        "retries_attempted": 0,
        "retry_allowed": bool(getattr(retry_policy, "retryable", False)),
        "timeout_seconds": (
            float(getattr(retry_policy, "timeout_ms", 0) or 0) / 1000
            if getattr(retry_policy, "timeout_ms", None)
            else None
        ),
    }


def _retrieval_engine_observe_safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, raw_value in value.items():
        key_text = str(key)
        if key_text.lower() in _RETRIEVAL_ENGINE_OBSERVE_RAW_PATH_KEYS:
            continue
        safe[key_text] = _retrieval_engine_observe_safe_value(raw_value)
    return safe


def _retrieval_engine_observe_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _retrieval_engine_observe_safe_mapping(value)
    if isinstance(value, tuple):
        return [_retrieval_engine_observe_safe_value(item) for item in value]
    if isinstance(value, list):
        return [_retrieval_engine_observe_safe_value(item) for item in value]
    if isinstance(value, set):
        return sorted(str(_retrieval_engine_observe_safe_value(item)) for item in value)
    if isinstance(value, str) and _retrieval_engine_observe_looks_like_raw_path(value):
        return "[redacted_path]"
    return value


def _retrieval_engine_observe_looks_like_raw_path(value: str) -> bool:
    text = value.strip()
    if text.startswith(("/api/v1/files/", "/openai/v1/files/", "/v1/files/")):
        return False
    return text.startswith(("/", "\\")) or (
        len(text) > 2 and text[1] == ":" and text[2] in {"\\", "/"}
    )
