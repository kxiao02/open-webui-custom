"""Open WebUI retrieval-engine contract helpers for selected-source bridging."""

from __future__ import annotations

import copy
import os
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

ENGINE_CONTRACT_VERSION = "open_webui_selected_source_engine_contract_v1"
ENGINE_CONTRACT_BOUNDARY_OWNER = "open_webui.retrieval.engine_contract"
ENGINE_CONTRACT_ENGINE_VERSION = "open_webui_selected_source_engine_v1"
ENGINE_INVOCATION_TIMEOUT_SECONDS = 25.0

ENGINE_TERMINAL_STATUSES = {
    "success",
    "partial",
    "no_evidence",
    "denied",
    "blocked",
    "malformed",
    "timeout",
    "error",
}

ENGINE_FALLBACK_REASONS = {
    "adapter_unavailable",
    "request_construction_error",
    "unsupported_source_shape",
    "operator_disabled",
}

SELECTED_SOURCE_ENGINE_OPERATOR_DISABLE_ENV = (
    "RETRIEVAL_ENGINE_SELECTED_SOURCE_DISABLED"
)
_OPERATOR_DISABLE_TRUTHY = {"1", "true", "yes", "on"}

ENGINE_ANSWER_POLICIES = {
    "success": "answer_from_accepted_evidence",
    "partial": "answer_with_limitations",
    "no_evidence": "report_no_evidence",
    "denied": "refuse_scope_or_permission",
    "blocked": "refuse_scope_or_permission",
    "timeout": "report_retrieval_error",
    "error": "report_retrieval_error",
    "malformed": "report_retrieval_error",
}


def selected_source_engine_operator_disabled() -> bool:
    """Operator kill switch: disable selected-source engine invocation globally."""
    return (
        os.environ.get(SELECTED_SOURCE_ENGINE_OPERATOR_DISABLE_ENV, "")
        .strip()
        .lower()
        in _OPERATOR_DISABLE_TRUTHY
    )


def answer_policy_for_terminal_status(status: Any) -> str:
    return ENGINE_ANSWER_POLICIES.get(
        _normalized_status(status), "report_retrieval_error"
    )


def _normalized_text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_status(value: Any) -> str:
    return _normalized_text(value).lower()


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _source_scope_echo(
    *,
    active_source_scope: dict[str, Any] | None = None,
    authorization_context: dict[str, Any] | None = None,
    selected_files: list[dict] | None = None,
) -> dict[str, Any]:
    active_source_scope = active_source_scope if isinstance(active_source_scope, dict) else {}
    authorization_context = (
        authorization_context if isinstance(authorization_context, dict) else {}
    )
    requested_source_ids = authorization_context.get("requested_source_ids")
    if not isinstance(requested_source_ids, list):
        requested_source_ids = active_source_scope.get("source_ids")
    if not isinstance(requested_source_ids, list):
        requested_source_ids = [
            item.get("id")
            for item in (selected_files or [])
            if isinstance(item, dict) and _normalized_text(item.get("id"))
        ]
    source_ids = list(
        dict.fromkeys(
            _normalized_text(item)
            for item in (requested_source_ids or [])
            if _normalized_text(item)
        )
    )
    return _retrieval_engine_observe_safe_mapping(
        {
            "status": (
                _normalized_text(
                    active_source_scope.get("status")
                    or authorization_context.get("active_source_scope_state")
                )
                or "none"
            ),
            "source_ids": source_ids,
            "authorization_generation": _normalized_text(
                authorization_context.get("authorization_generation")
            )
            or ENGINE_CONTRACT_ENGINE_VERSION,
        }
    )


def _apply_engine_contract_trust_fields(
    contract: dict[str, Any],
    *,
    active_source_scope: dict[str, Any] | None = None,
    selected_files: list[dict] | None = None,
) -> dict[str, Any]:
    prepared = copy.deepcopy(contract)
    authorization_context = (
        prepared.get("authorization_context")
        if isinstance(prepared.get("authorization_context"), dict)
        else {}
    )
    context_budget = (
        prepared.get("context_budget")
        if isinstance(prepared.get("context_budget"), dict)
        else {}
    )
    budget = context_budget.get("budget") if isinstance(context_budget, dict) else {}
    budget = budget if isinstance(budget, dict) else {}
    engine_version = (
        _normalized_text(prepared.get("engine_version"))
        or _normalized_text(budget.get("engine_version"))
        or ENGINE_CONTRACT_ENGINE_VERSION
    )
    prepared["contract_version"] = ENGINE_CONTRACT_VERSION
    prepared["engine_version"] = engine_version
    prepared["boundary_owned"] = True
    prepared["boundary_owner"] = ENGINE_CONTRACT_BOUNDARY_OWNER
    prepared["source_scope_echo"] = _source_scope_echo(
        active_source_scope=active_source_scope,
        authorization_context=authorization_context,
        selected_files=selected_files,
    )
    provenance = (
        copy.deepcopy(prepared.get("provenance"))
        if isinstance(prepared.get("provenance"), dict)
        else {}
    )
    provenance["contract_version"] = ENGINE_CONTRACT_VERSION
    provenance["engine_version"] = engine_version
    provenance["boundary_owned"] = True
    provenance["boundary_owner"] = ENGINE_CONTRACT_BOUNDARY_OWNER
    provenance["retrieval_runtime_mode"] = "engine_owned"
    prepared["provenance"] = provenance
    return prepared


def _engine_runtime_mode_for_contract(contract: dict[str, Any]) -> str:
    status = _normalized_status(contract.get("status"))
    references = _list_of_dicts(contract.get("references"))
    accepted_outputs = _list_of_dicts(contract.get("accepted_outputs"))
    if status in {"success", "partial"} and references and accepted_outputs:
        return "retrieval_engine_authority"
    if status in {"denied", "blocked", "error"} or _normalized_status(
        contract.get("terminal_reason")
    ) == "invalid_engine_contract":
        return "retrieval_engine_fail_closed"
    return "retrieval_engine_diagnostics"


def _engine_bypass_reason_for_runtime(runtime_mode: str) -> str:
    if runtime_mode == "retrieval_engine_authority":
        return "retrieval_engine_authority_succeeded"
    if runtime_mode == "retrieval_engine_fail_closed":
        return "retrieval_engine_fail_closed"
    return "retrieval_engine_diagnostics_only"


def _invalid_engine_contract_diagnostic(
    *,
    invalid_reasons: list[str] | None = None,
) -> dict[str, Any]:
    return _retrieval_engine_observe_safe_value(
        {
            "kind": "retrieval_engine",
            "classification": "no_evidence",
            "reason": "invalid_engine_contract",
            "outcome": "error",
            "candidate_index": -1,
            "provenance": {
                "worker_kind": "selected_source_retrieval",
                "tool_name": "retrieval_engine_selected_source_retrieval",
                "status": "error",
                "terminal_reason": "invalid_engine_contract",
                "selected_source_runtime_mode": "retrieval_engine_fail_closed",
                "retrieval_runtime_mode": "engine_owned",
                "engine_owned": True,
                "engine_authority": False,
                "compatibility_fallback": False,
                "failure_class": "invalid_engine_contract",
                "contract_version": ENGINE_CONTRACT_VERSION,
                "engine_version": ENGINE_CONTRACT_ENGINE_VERSION,
                "boundary_owned": True,
                "boundary_owner": ENGINE_CONTRACT_BOUNDARY_OWNER,
            },
            "detail": {
                "invalid_reasons": list(
                    dict.fromkeys(invalid_reasons or ["invalid_engine_contract"])
                )
            },
        }
    )


def build_engine_error_contract(
    *,
    terminal_reason: str,
    diagnostics: list[dict[str, Any]] | None = None,
    active_source_scope: dict[str, Any] | None = None,
    selected_files: list[dict] | None = None,
    failure_class: str = "engine_error",
    status: str = "error",
) -> dict[str, Any]:
    reason = _normalized_text(terminal_reason) or "engine_error"
    effective_status = _normalized_text(status) or "error"
    diagnostic_items = [
        item for item in (diagnostics or []) if isinstance(item, dict)
    ] or [
        {
            "kind": "retrieval_engine",
            "classification": "no_evidence",
            "reason": reason,
            "outcome": effective_status,
            "candidate_index": -1,
        }
    ]
    contract = {
        "accepted_outputs": [],
        "references": [],
        "sources": [],
        "diagnostics": diagnostic_items,
        "status": effective_status,
        "terminal_reason": reason,
        "authorization_context": {
            "active_source_scope_state": _normalized_text(
                (active_source_scope or {}).get("status")
            )
            or "none",
            "requested_source_ids": list(
                dict.fromkeys(
                    _normalized_text(item.get("id"))
                    for item in (selected_files or [])
                    if isinstance(item, dict) and _normalized_text(item.get("id"))
                )
            ),
            "authorization_generation": ENGINE_CONTRACT_ENGINE_VERSION,
        },
        "retry_policy": {
            "max_retries": 0,
            "retries_attempted": 0,
            "retry_allowed": False,
            "timeout_seconds": None,
        },
        "context_budget": {
            "requested_budget_tokens": 0,
            "estimated_used_tokens": 0,
            "accepted_output_count": 0,
            "reference_count": 0,
            "candidate_accepted_bundle_count": 0,
            "truncated_accepted_bundle_count": 0,
            "omitted_accepted_bundle_count": 0,
            "truncated": False,
            "budget": {
                "budget_policy": "selected_source_first_pass",
                "return_policy": "accepted_evidence_only",
                "ranking_policy": "selected_file_text_cutover",
                "engine_version": ENGINE_CONTRACT_ENGINE_VERSION,
                "authorization_generation": ENGINE_CONTRACT_ENGINE_VERSION,
            },
        },
        "provenance": {
            "worker_kind": "selected_source_retrieval",
            "tool_name": "retrieval_engine_selected_source_retrieval",
            "status": effective_status,
            "terminal_reason": reason,
            "engine_authority": False,
            "middleware_strategy_bypassed": True,
            "middleware_strategy_bypass_reason": "retrieval_engine_fail_closed",
            "selected_source_runtime_mode": "retrieval_engine_fail_closed",
            "retrieval_runtime_mode": "engine_owned",
            "engine_owned": True,
            "compatibility_fallback": False,
            "failure_class": failure_class,
        },
    }
    return _apply_engine_contract_trust_fields(
        contract,
        active_source_scope=active_source_scope,
        selected_files=selected_files,
    )


def _invalid_engine_contract(
    *,
    invalid_reasons: list[str],
    active_source_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_engine_error_contract(
        terminal_reason="invalid_engine_contract",
        diagnostics=[
            _invalid_engine_contract_diagnostic(invalid_reasons=invalid_reasons)
        ],
        active_source_scope=active_source_scope,
        selected_files=[],
        failure_class="invalid_engine_contract",
    )


def _contract_source_ids(contract: dict[str, Any]) -> list[str]:
    echo = contract.get("source_scope_echo")
    echo = echo if isinstance(echo, dict) else {}
    source_ids = echo.get("source_ids")
    if not isinstance(source_ids, list):
        authorization_context = contract.get("authorization_context")
        authorization_context = (
            authorization_context if isinstance(authorization_context, dict) else {}
        )
        source_ids = authorization_context.get("requested_source_ids")
    return [
        _normalized_text(item)
        for item in (source_ids or [])
        if _normalized_text(item)
    ]


def _expected_source_ids(active_source_scope: dict[str, Any] | None) -> list[str]:
    active_source_scope = active_source_scope if isinstance(active_source_scope, dict) else {}
    source_ids = active_source_scope.get("source_ids")
    return [
        _normalized_text(item)
        for item in (source_ids or [])
        if _normalized_text(item)
    ]


def validate_engine_contract(
    contract: Any,
    *,
    active_source_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a boundary-owned selected-source engine contract."""

    invalid_reasons: list[str] = []
    if not isinstance(contract, dict):
        invalid_reasons.append("contract_not_mapping")
        contract = {}
    payload = copy.deepcopy(contract)
    status = _normalized_status(payload.get("status"))
    terminal_reason = _normalized_text(payload.get("terminal_reason"))
    provenance = payload.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}

    if payload.get("contract_version") != ENGINE_CONTRACT_VERSION:
        invalid_reasons.append("missing_or_invalid_contract_version")
    if payload.get("engine_version") != ENGINE_CONTRACT_ENGINE_VERSION:
        invalid_reasons.append("missing_or_invalid_engine_version")
    if payload.get("boundary_owned") is not True or provenance.get("boundary_owned") is not True:
        invalid_reasons.append("missing_boundary_owned_marker")
    if (
        payload.get("boundary_owner") != ENGINE_CONTRACT_BOUNDARY_OWNER
        or provenance.get("boundary_owner") != ENGINE_CONTRACT_BOUNDARY_OWNER
    ):
        invalid_reasons.append("missing_boundary_owner")
    if status not in ENGINE_TERMINAL_STATUSES:
        invalid_reasons.append("unknown_terminal_status")
    if status != "success" and not terminal_reason:
        invalid_reasons.append("missing_terminal_reason")

    references = _list_of_dicts(payload.get("references"))
    accepted_outputs = _list_of_dicts(payload.get("accepted_outputs"))
    sources = _list_of_dicts(payload.get("sources"))
    if status not in {"success", "partial"} and (
        references or accepted_outputs or sources
    ):
        invalid_reasons.append("accepted_evidence_on_non_accepting_status")

    source_scope_echo = payload.get("source_scope_echo")
    if not isinstance(source_scope_echo, dict):
        invalid_reasons.append("missing_source_scope_echo")
    contract_source_ids = _contract_source_ids(payload)
    expected_source_ids = _expected_source_ids(active_source_scope)
    if expected_source_ids and sorted(contract_source_ids) != sorted(expected_source_ids):
        invalid_reasons.append("source_scope_mismatch")

    valid = not invalid_reasons
    effective_contract = payload if valid else _invalid_engine_contract(
        invalid_reasons=invalid_reasons,
        active_source_scope=active_source_scope,
    )
    runtime_mode = _engine_runtime_mode_for_contract(effective_contract)
    terminal_status = _normalized_status(effective_contract.get("status")) or "error"
    effective_terminal_reason = (
        _normalized_text(effective_contract.get("terminal_reason"))
        or terminal_status
    )
    return {
        "valid": valid,
        "contract_present": bool(contract),
        "contract": effective_contract,
        "terminal_status": terminal_status,
        "terminal_reason": effective_terminal_reason,
        "selected_source_runtime_mode": runtime_mode,
        "bypass_reason": _engine_bypass_reason_for_runtime(runtime_mode),
        "failure_class": "" if valid else "invalid_engine_contract",
        "invalid_reasons": list(dict.fromkeys(invalid_reasons)),
    }


def read_engine_contract_for_turn(metadata: dict | None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in (
        "retrieval_engine_first_pass_contract",
        "retrieval_engine_contract",
        "selected_source_retrieval_engine_contract",
    ):
        value = metadata.get(key)
        if isinstance(value, dict):
            return copy.deepcopy(value)
    return {}


def _validate_boundary_attempt_scope(
    *,
    attempt: dict[str, Any],
    turn_id: str,
    message_id: str,
) -> list[str]:
    """Validate that a boundary attempt record is bound to the current turn/message scope.

    Strict all-or-nothing contract:
    - Caller MUST provide both turn_id and message_id.
    - Attempt MUST carry both turn_id and message_id.
    - Both IDs MUST match exactly.
    Partial binding or missing caller scope fails closed.
    """
    invalid_reasons: list[str] = []
    normalized_turn = _normalized_text(turn_id)
    normalized_message = _normalized_text(message_id)

    attempt_turn = _normalized_text(attempt.get("turn_id"))
    attempt_message = _normalized_text(attempt.get("message_id"))

    if not normalized_turn or not normalized_message:
        invalid_reasons.append("missing_current_turn_scope_binding")
    if not attempt_turn or not attempt_message:
        invalid_reasons.append("missing_boundary_attempt_scope_binding")

    if invalid_reasons:
        return invalid_reasons

    if normalized_turn != attempt_turn:
        invalid_reasons.append("stale_boundary_attempt_turn_mismatch")
    if normalized_message != attempt_message:
        invalid_reasons.append("stale_boundary_attempt_message_mismatch")

    return invalid_reasons


def classify_engine_attempt(
    *,
    package: dict[str, Any] | None = None,
    metadata: dict | None = None,
    active_source_scope: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
    turn_id: str = "",
    message_id: str = "",
) -> dict[str, Any]:
    package = package if isinstance(package, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    authority = package.get("authority")
    authority = authority if isinstance(authority, dict) else {}
    attempt = package.get("attempt")
    if not isinstance(attempt, dict):
        attempt = metadata.get("retrieval_engine_attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    contract_source = ""
    raw_contract = contract if isinstance(contract, dict) else None
    if raw_contract is not None:
        contract_source = "argument"
    if raw_contract is None:
        package_contract = package.get("contract")
        raw_contract = package_contract if isinstance(package_contract, dict) else None
        if raw_contract is not None:
            contract_source = "package"
    if raw_contract is None:
        metadata_contract = read_engine_contract_for_turn(metadata)
        raw_contract = metadata_contract if metadata_contract else None
        if raw_contract is not None:
            contract_source = "metadata"

    if raw_contract is not None:
        validation = validate_engine_contract(
            raw_contract,
            active_source_scope=active_source_scope,
        )
        if contract_source == "metadata" and (
            attempt.get("boundary_owned") is not True
            or attempt.get("boundary_owner") != ENGINE_CONTRACT_BOUNDARY_OWNER
            or attempt.get("engine_invoked") is not True
            or attempt.get("contract_version") != ENGINE_CONTRACT_VERSION
            or attempt.get("engine_version") != ENGINE_CONTRACT_ENGINE_VERSION
        ):
            trust_reasons: list[str] = []
            if attempt.get("boundary_owner") != ENGINE_CONTRACT_BOUNDARY_OWNER:
                trust_reasons.append("forged_boundary_attempt_metadata")
            if attempt.get("boundary_owned") is not True:
                trust_reasons.append("missing_boundary_attempt_metadata")
            if attempt.get("engine_invoked") is not True:
                trust_reasons.append("uninvoked_boundary_attempt")
            if attempt.get("contract_version") != ENGINE_CONTRACT_VERSION:
                trust_reasons.append("boundary_attempt_contract_version_mismatch")
            if attempt.get("engine_version") != ENGINE_CONTRACT_ENGINE_VERSION:
                trust_reasons.append("boundary_attempt_engine_version_mismatch")
            invalid_reasons = [
                *validation.get("invalid_reasons", []),
                *trust_reasons,
            ]
            validation = {
                "valid": False,
                "contract_present": True,
                "contract": _invalid_engine_contract(
                    invalid_reasons=invalid_reasons,
                    active_source_scope=active_source_scope,
                ),
                "terminal_status": "error",
                "terminal_reason": "invalid_engine_contract",
                "selected_source_runtime_mode": "retrieval_engine_fail_closed",
                "bypass_reason": "retrieval_engine_fail_closed",
                "failure_class": "invalid_engine_contract",
                "invalid_reasons": list(dict.fromkeys(invalid_reasons)),
            }
        elif contract_source == "metadata" and validation.get("valid"):
            scope_invalid_reasons = _validate_boundary_attempt_scope(
                attempt=attempt,
                turn_id=turn_id,
                message_id=message_id,
            )
            if scope_invalid_reasons:
                invalid_reasons = [
                    *validation.get("invalid_reasons", []),
                    *scope_invalid_reasons,
                ]
                validation = {
                    "valid": False,
                    "contract_present": True,
                    "contract": _invalid_engine_contract(
                        invalid_reasons=invalid_reasons,
                        active_source_scope=active_source_scope,
                    ),
                    "terminal_status": "error",
                    "terminal_reason": "invalid_engine_contract",
                    "selected_source_runtime_mode": "retrieval_engine_fail_closed",
                    "bypass_reason": "retrieval_engine_fail_closed",
                    "failure_class": "invalid_engine_contract",
                    "invalid_reasons": list(dict.fromkeys(invalid_reasons)),
                }
        state = "engine_owned" if validation["valid"] else "fail_closed"
        answer_policy = (
            answer_policy_for_terminal_status(validation["terminal_status"])
            if validation["valid"]
            else "report_retrieval_error"
        )
        return {
            **validation,
            "state": state,
            "engine_owned": True,
            "engine_invoked": True,
            "fallback_eligible": False,
            "runtime_mode": "engine_owned",
            "retrieval_runtime_mode": "engine_owned",
            "fallback_reason": "",
            "answer_policy": answer_policy,
            "attempt_metadata": {
                "engine_invoked": True,
                "contract_present": True,
                "contract_valid": bool(validation["valid"]),
                "contract_version": ENGINE_CONTRACT_VERSION,
                "engine_version": ENGINE_CONTRACT_ENGINE_VERSION,
                "terminal_status": validation["terminal_status"],
                "terminal_reason": validation["terminal_reason"],
                "runtime_mode": "engine_owned",
                "retrieval_runtime_mode": "engine_owned",
                "selected_source_runtime_mode": validation[
                    "selected_source_runtime_mode"
                ],
                "fallback_reason": "",
                "answer_policy": answer_policy,
                "failure_class": validation["failure_class"],
                "invalid_reasons": validation["invalid_reasons"],
            },
        }

    fallback_reason = (
        _normalized_status(attempt.get("fallback_reason"))
        or _normalized_status(attempt.get("failure_class"))
        or _normalized_status(authority.get("reason"))
    )
    fallback_eligible = fallback_reason in ENGINE_FALLBACK_REASONS and not bool(
        attempt.get("engine_invoked")
    )
    failure_class = fallback_reason or "engine_contract_absent"
    return {
        "valid": False,
        "contract_present": False,
        "contract": {},
        "terminal_status": "",
        "terminal_reason": "",
        "selected_source_runtime_mode": "compatibility_fallback",
        "bypass_reason": "",
        "failure_class": failure_class,
        "invalid_reasons": [],
        "state": "fallback_eligible" if fallback_eligible else "no_contract",
        "engine_owned": False,
        "engine_invoked": False,
        "fallback_eligible": fallback_eligible,
        "runtime_mode": "legacy_fallback" if fallback_eligible else "unavailable",
        "retrieval_runtime_mode": "legacy_fallback" if fallback_eligible else "unavailable",
        "fallback_reason": fallback_reason if fallback_eligible else "",
        "answer_policy": "",
        "attempt_metadata": {
            "engine_invoked": False,
            "contract_present": False,
            "contract_valid": False,
            "contract_version": ENGINE_CONTRACT_VERSION,
            "engine_version": ENGINE_CONTRACT_ENGINE_VERSION,
            "terminal_status": "",
            "terminal_reason": "",
            "runtime_mode": "legacy_fallback" if fallback_eligible else "unavailable",
            "retrieval_runtime_mode": "legacy_fallback" if fallback_eligible else "unavailable",
            "selected_source_runtime_mode": "compatibility_fallback",
            "fallback_reason": fallback_reason if fallback_eligible else "",
            "answer_policy": "",
            "failure_class": failure_class,
            "invalid_reasons": [],
        },
    }


def persist_engine_attempt(
    metadata: dict,
    classification: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    attempt = copy.deepcopy(classification.get("attempt_metadata") or {})
    attempt["boundary_owned"] = True
    attempt["boundary_owner"] = ENGINE_CONTRACT_BOUNDARY_OWNER
    turn_id = _normalized_text(metadata.get("turn_id"))
    message_id = _normalized_text(metadata.get("message_id"))
    if turn_id:
        attempt.setdefault("turn_id", turn_id)
    if message_id:
        attempt.setdefault("message_id", message_id)
    metadata["retrieval_engine_attempt"] = attempt
    contract = classification.get("contract")
    if classification.get("engine_owned") and isinstance(contract, dict) and contract:
        metadata["retrieval_engine_first_pass_contract"] = copy.deepcopy(contract)
    return attempt


_SELECTED_SOURCE_TELEMETRY_COUNTERS: dict[str, int] = {}


def _source_card_origin_is_legacy(card: Any) -> bool:
    if not isinstance(card, dict):
        return False
    if _normalized_status(card.get("origin")) == "legacy_fallback":
        return True
    source = card.get("source")
    return (
        isinstance(source, dict)
        and _normalized_status(source.get("origin")) == "legacy_fallback"
    )


def record_selected_source_retrieval_telemetry(
    *,
    classification: dict[str, Any] | None,
    active_sources: list[Any] | None = None,
    source_shape: str = "local_file_text",
) -> dict[str, Any]:
    """Aggregate per-turn selected-source retrieval telemetry.

    Counts runtime mode, terminal status, fallback reason, and source shape,
    and flags the boundary violation where any present engine contract
    coexists with legacy-origin selected-source cards."""
    classification = classification if isinstance(classification, dict) else {}
    contract = classification.get("contract")
    contract = contract if isinstance(contract, dict) else {}
    references = _list_of_dicts(contract.get("references"))
    legacy_cards = [
        card for card in (active_sources or []) if _source_card_origin_is_legacy(card)
    ]
    contract_present = bool(classification.get("contract_present"))
    boundary_violation = bool(contract_present and legacy_cards)
    runtime_mode = (
        _normalized_status(classification.get("retrieval_runtime_mode"))
        or "unavailable"
    )
    terminal_status = _normalized_status(classification.get("terminal_status"))
    fallback_reason = _normalized_status(classification.get("fallback_reason"))
    shape = _normalized_status(source_shape) or "local_file_text"
    snapshot = {
        "runtime_mode": runtime_mode,
        "selected_source_runtime_mode": _normalized_status(
            classification.get("selected_source_runtime_mode")
        ),
        "terminal_status": terminal_status,
        "fallback_reason": fallback_reason,
        "source_shape": shape,
        "accepted_reference_count": len(references),
        "legacy_origin_source_card_count": len(legacy_cards),
        "contract_present": contract_present,
        "contract_valid": bool(classification.get("valid")),
        "answer_policy": _normalized_text(classification.get("answer_policy")),
        "boundary_violation": boundary_violation,
    }
    counter_key = "|".join(
        [
            runtime_mode,
            terminal_status or "none",
            fallback_reason or "none",
            shape,
            f"violation={int(boundary_violation)}",
        ]
    )
    _SELECTED_SOURCE_TELEMETRY_COUNTERS[counter_key] = (
        _SELECTED_SOURCE_TELEMETRY_COUNTERS.get(counter_key, 0) + 1
    )
    snapshot["counter_key"] = counter_key
    snapshot["counter_value"] = _SELECTED_SOURCE_TELEMETRY_COUNTERS[counter_key]
    return snapshot


def selected_source_retrieval_telemetry_counters() -> dict[str, int]:
    return dict(_SELECTED_SOURCE_TELEMETRY_COUNTERS)


def reset_selected_source_retrieval_telemetry() -> None:
    _SELECTED_SOURCE_TELEMETRY_COUNTERS.clear()


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
    contract = {
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
            "retrieval_runtime_mode": "engine_owned",
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
    return _apply_engine_contract_trust_fields(
        contract,
        selected_files=selected_files,
    )


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
