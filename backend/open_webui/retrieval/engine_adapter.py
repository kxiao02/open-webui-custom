"""Passive Open WebUI boundary for the standalone retrieval engine."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence


def _ensure_retrieval_engine_importable() -> None:
    try:
        import retrieval_engine  # noqa: F401

        return
    except ModuleNotFoundError as exc:
        if exc.name != "retrieval_engine":
            raise

    repo_root = Path(__file__).resolve().parents[4]
    engine_root = repo_root / "retrieval-engine"
    if engine_root.exists():
        sys.path.insert(0, str(engine_root))


_ensure_retrieval_engine_importable()

from retrieval_engine import (  # noqa: E402
    AuthorizedScope,
    Candidate,
    ConnectorCapabilityDeclaration,
    ConnectorResponse,
    RetrievalDiagnostic,
    RetrievalPlan,
    RetrievalRequest,
    SourceCapability,
    SourceDescriptor,
)
from retrieval_engine.contracts import WorkbookConnectorRequest  # noqa: E402


_CAPABILITY_NAMES: Mapping[str, SourceCapability] = {
    capability.value: capability for capability in SourceCapability
}
_WORKBOOK_CONTENT_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_RAW_PATH_KEYS = {
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


@dataclass(frozen=True)
class OpenWebUIRetrievalAdapterResult:
    """Host-shaped request package; it intentionally has no accepted evidence lanes."""

    request: RetrievalRequest
    connectors: tuple[Any, ...] = ()
    diagnostics: tuple[RetrievalDiagnostic, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OpenWebUIVectorStoreConnector:
    """Thin vector-store handle adapter that never performs retrieval at construction."""

    vector_handles: Mapping[str, Mapping[str, Any]]
    vector_config: Mapping[str, Any] = field(default_factory=dict)
    connector_id: str = "open_webui_vector_store"

    def discover_capabilities(
        self,
        sources: tuple[SourceDescriptor, ...],
    ) -> dict[str, ConnectorCapabilityDeclaration]:
        declarations: dict[str, ConnectorCapabilityDeclaration] = {}
        for source in sources:
            handle = self.vector_handles.get(source.source_id)
            if not handle:
                continue

            declared_capabilities = _capabilities_from_values(
                handle.get("capabilities")
                or self.vector_config.get("capabilities")
                or ("vector",)
            )
            capabilities = frozenset(
                declared_capabilities
                or source.declared_capabilities
                or (SourceCapability.VECTOR,)
            )
            declarations[source.source_id] = ConnectorCapabilityDeclaration(
                capabilities=capabilities,
                canonical_text_available=bool(handle.get("canonical_text_available")),
                canonical_range_available=bool(
                    handle.get("canonical_range_available")
                ),
                index_generation=(
                    _string_or_none(handle.get("index_generation"))
                    or _string_or_none(self.vector_config.get("index_generation"))
                ),
                contextual_embedding_metadata=_mapping_or_empty(
                    handle.get("embedding")
                    or handle.get("embedding_model_settings")
                    or self.vector_config.get("embedding")
                    or self.vector_config.get("embedding_model_settings")
                ),
                contextual_index_metadata={
                    **_mapping_or_empty(self.vector_config),
                    **_mapping_or_empty(handle),
                },
                source_scope_enforcement_mode="pre_filter",
                ranking_signals=frozenset({"semantic_score"}),
                supports_structured_query=bool(
                    handle.get("supports_structured_query")
                    or self.vector_config.get("supports_structured_query")
                ),
            )
        return declarations

    def generate_candidates(
        self,
        request: RetrievalRequest,
        plan: RetrievalPlan,
        sources: tuple[SourceDescriptor, ...],
    ) -> ConnectorResponse:
        return ConnectorResponse(
            diagnostics=(
                RetrievalDiagnostic(
                    code="host_vector_retrieval_not_executed",
                    message=(
                        "Open WebUI vector handles were adapted for planning only; "
                        "runtime retrieval is not wired in this slice."
                    ),
                    kind="candidate",
                    severity="info",
                    phase="host_adapter",
                    details={
                        "connector_id": self.connector_id,
                        "source_ids": tuple(source.source_id for source in sources),
                    },
                ),
            )
        )

    def hydrate_source_anchor(
        self,
        request: RetrievalRequest,
        plan: RetrievalPlan,
        candidate: Candidate,
        sources: tuple[SourceDescriptor, ...],
    ) -> ConnectorResponse:
        return ConnectorResponse(
            diagnostics=(
                RetrievalDiagnostic(
                    code="host_source_anchor_hydration_not_executed",
                    message=(
                        "Open WebUI source-anchor hydration is represented but not "
                        "wired to runtime retrieval in this slice."
                    ),
                    kind="candidate",
                    severity="info",
                    phase="host_adapter",
                    source_id=candidate.source_id,
                    candidate_id=candidate.candidate_id,
                ),
            )
        )


@dataclass(frozen=True)
class OpenWebUIWorkbookConnector:
    """Passive workbook-tool boundary constrained to authorized workbook source ids."""

    workbook_handles: Mapping[str, Mapping[str, Any]]
    workbook_contracts: Mapping[str, Any] = field(default_factory=dict)
    connector_id: str = "open_webui_workbook"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workbook_handles",
            {
                source_id: _safe_mapping(handle)
                for source_id, handle in self.workbook_handles.items()
            },
        )

    def discover_capabilities(
        self,
        sources: tuple[SourceDescriptor, ...],
    ) -> dict[str, ConnectorCapabilityDeclaration]:
        source_ids = {source.source_id for source in sources}
        return {
            source_id: ConnectorCapabilityDeclaration(
                capabilities=frozenset(
                    {SourceCapability.WORKBOOK_RANGE, SourceCapability.SOURCE_ANCHORS}
                ),
                canonical_range_available=True,
                source_scope_enforcement_mode="pre_filter",
                supports_structured_query=True,
            )
            for source_id in self.workbook_handles
            if source_id in source_ids
        }

    def generate_candidates(
        self,
        request: RetrievalRequest,
        plan: RetrievalPlan,
        sources: tuple[SourceDescriptor, ...],
    ) -> ConnectorResponse:
        source_ids = {source.source_id for source in sources}
        diagnostics: list[RetrievalDiagnostic] = []
        candidates: list[Candidate] = []
        workbook_request = getattr(plan, "workbook_request", None)
        operation = _workbook_operation_from_request(workbook_request)

        for source_id, handle in self.workbook_handles.items():
            if source_id not in source_ids:
                continue

            contract = self.workbook_contracts.get(source_id)
            if contract is None:
                diagnostics.append(
                    RetrievalDiagnostic(
                        code="host_workbook_contract_not_configured",
                        message=(
                            "Workbook source was adapted, but no workbook tool "
                            "contract is configured for runtime execution."
                        ),
                        kind="capability",
                        severity="warning",
                        phase="host_adapter",
                        source_id=source_id,
                        details={
                            "connector_id": self.connector_id,
                            "workbook_operation": operation,
                            "workbook_handle": handle,
                        },
                    )
                )
                continue

            response = _invoke_workbook_contract(
                contract=contract,
                source_id=source_id,
                workbook_handle=handle,
                workbook_request=workbook_request,
                operation=operation,
            )
            candidates.extend(_sanitize_candidates(response.candidates))
            diagnostics.extend(_sanitize_diagnostics(response.diagnostics))

        return ConnectorResponse(
            candidates=tuple(candidates),
            diagnostics=tuple(diagnostics),
        )

    def hydrate_source_anchor(
        self,
        request: RetrievalRequest,
        plan: RetrievalPlan,
        candidate: Candidate,
        sources: tuple[SourceDescriptor, ...],
    ) -> ConnectorResponse:
        return ConnectorResponse(
            diagnostics=(
                RetrievalDiagnostic(
                    code="host_workbook_anchor_hydration_not_executed",
                    message=(
                        "Workbook source-anchor hydration is represented but not "
                        "wired to production runtime in this slice."
                    ),
                    kind="candidate",
                    severity="info",
                    phase="host_adapter",
                    source_id=candidate.source_id,
                    candidate_id=candidate.candidate_id,
                    details={"connector_id": self.connector_id},
                ),
            )
        )


def build_retrieval_engine_request(
    *,
    query: str,
    selected_file_ids: Sequence[str] = (),
    selected_files: Sequence[Any] = (),
    active_source_scope: Mapping[str, Any] | None = None,
    knowledge_selections: Sequence[Any] = (),
    authorization_decision: str | Mapping[str, Any] = "authorized",
    authorization_generation: str | None = None,
    vector_store_handles: Mapping[str, Mapping[str, Any]] | None = None,
    vector_config: Mapping[str, Any] | None = None,
    workbook_handles: Mapping[str, Mapping[str, Any]] | None = None,
    workbook_contracts: Mapping[str, Any] | None = None,
    knowflow_descriptors: Mapping[str, Mapping[str, Any]] | None = None,
    requested_budget_tokens: int = 4096,
    execution_context: Mapping[str, Any] | None = None,
) -> OpenWebUIRetrievalAdapterResult:
    """Translate Open WebUI selected-source state into retrieval-engine inputs."""

    diagnostics: list[RetrievalDiagnostic] = []
    selected_ids = _stable_ids(selected_file_ids)
    sources = _source_descriptors(
        selected_files=selected_files,
        knowledge_selections=knowledge_selections,
        knowflow_descriptors=knowflow_descriptors or {},
        diagnostics=diagnostics,
    )
    known_source_ids = {source.source_id for source in sources}

    for source_id in selected_ids:
        if source_id not in known_source_ids:
            diagnostics.append(
                _diagnostic(
                    code="missing_selected_file",
                    message=f"Selected file {source_id!r} was not available to adapt.",
                    severity="error",
                    source_id=source_id,
                    details={"source_id": source_id},
                )
            )

    scope = _authorized_scope(
        active_source_scope=active_source_scope,
        authorization_decision=authorization_decision,
        fallback_source_ids=selected_ids or tuple(source.source_id for source in sources),
        diagnostics=diagnostics,
    )

    if scope.decision == "authorized":
        missing_scope_ids = tuple(
            source_id for source_id in scope.source_ids if source_id not in known_source_ids
        )
        if missing_scope_ids:
            diagnostics.append(
                _diagnostic(
                    code="unresolved_selected_source_scope",
                    message="Authorized selected-source scope included unknown sources.",
                    severity="error",
                    details={"missing_source_ids": missing_scope_ids},
                )
            )
            scope = AuthorizedScope(
                decision="blocked",
                source_ids=(),
                reason="unresolved_selected_source_scope",
            )

    authorized_sources = (
        tuple(source for source in sources if source.source_id in set(scope.source_ids))
        if scope.decision == "authorized"
        else ()
    )

    vector_handles = vector_store_handles or {}
    if scope.decision == "authorized":
        for source in authorized_sources:
            requires_vector = (
                SourceCapability.VECTOR in source.declared_capabilities
                or bool(source.metadata.get("vector_store"))
            )
            if requires_vector and source.source_id not in vector_handles:
                diagnostics.append(
                    _diagnostic(
                        code="missing_vector_store_handle",
                        message=(
                            "Selected source declares vector retrieval but no vector "
                            "store handle was provided."
                        ),
                        kind="capability",
                        severity="warning",
                        source_id=source.source_id,
                    )
                )

    context = {
        **(execution_context or {}),
        "authorization_generation": authorization_generation,
        "host_adapter": "open_webui",
        "host_adapter_mode": "passive",
    }
    request = RetrievalRequest(
        query=query,
        scope=scope,
        sources=authorized_sources,
        requested_budget_tokens=requested_budget_tokens,
        execution_context=context,
    )

    connectors: list[Any] = []
    if scope.decision == "authorized" and vector_handles:
        connectors.append(
            OpenWebUIVectorStoreConnector(
                vector_handles=vector_handles,
                vector_config=vector_config or {},
            )
        )

    workbook_handle_subset = _authorized_workbook_handles(
        authorized_sources=authorized_sources,
        workbook_handles=workbook_handles or {},
        diagnostics=diagnostics,
    )
    if scope.decision == "authorized" and workbook_handle_subset:
        connectors.append(
            OpenWebUIWorkbookConnector(
                workbook_handles=workbook_handle_subset,
                workbook_contracts=workbook_contracts or {},
            )
        )

    return OpenWebUIRetrievalAdapterResult(
        request=request,
        connectors=tuple(connectors),
        diagnostics=tuple(diagnostics),
        metadata={
            "selected_file_ids": selected_ids,
            "source_ids": tuple(source.source_id for source in authorized_sources),
            "workbook_source_ids": tuple(workbook_handle_subset),
            "blocked": scope.decision != "authorized",
        },
    )


def _source_descriptors(
    *,
    selected_files: Sequence[Any],
    knowledge_selections: Sequence[Any],
    knowflow_descriptors: Mapping[str, Mapping[str, Any]],
    diagnostics: list[RetrievalDiagnostic],
) -> tuple[SourceDescriptor, ...]:
    descriptors: list[SourceDescriptor] = []
    seen: set[str] = set()

    for file_item in selected_files:
        item = _as_mapping(file_item)
        source_id = _string_or_none(item.get("id"))
        if not source_id:
            diagnostics.append(
                _diagnostic(
                    code="unsupported_source_descriptor",
                    message="Selected file descriptor is missing an id.",
                    severity="error",
                    details={"source_type": "file"},
                )
            )
            continue
        if source_id in seen:
            continue
        seen.add(source_id)
        descriptors.append(_file_source_descriptor(item, knowflow_descriptors))

    for knowledge_item in knowledge_selections:
        item = _as_mapping(knowledge_item)
        source_id = _string_or_none(item.get("id"))
        if not source_id:
            diagnostics.append(
                _diagnostic(
                    code="unsupported_source_descriptor",
                    message="Knowledge descriptor is missing an id.",
                    severity="error",
                    details={"source_type": "knowledge"},
                )
            )
            continue
        if source_id in seen:
            continue
        seen.add(source_id)
        descriptors.append(_knowledge_source_descriptor(item, knowflow_descriptors))

    return tuple(descriptors)


def _file_source_descriptor(
    file_item: Mapping[str, Any],
    knowflow_descriptors: Mapping[str, Mapping[str, Any]],
) -> SourceDescriptor:
    source_id = str(file_item["id"])
    meta = _mapping_or_empty(file_item.get("meta"))
    data = _mapping_or_empty(file_item.get("data"))
    content_type = (
        _string_or_none(file_item.get("content_type"))
        or _string_or_none(meta.get("content_type"))
    )
    label = (
        _string_or_none(file_item.get("filename"))
        or _string_or_none(file_item.get("name"))
        or source_id
    )
    knowflow = _mapping_or_empty(knowflow_descriptors.get(source_id))
    capabilities = {
        SourceCapability.METADATA_INVENTORY,
        *(_capabilities_from_values(meta.get("capabilities"))),
        *(_capabilities_from_values(file_item.get("capabilities"))),
        *(_capabilities_from_values(knowflow.get("capabilities"))),
    }
    if data.get("content"):
        capabilities.update(
            {
                SourceCapability.EXACT_TEXT,
                SourceCapability.KEYWORD,
                SourceCapability.FULL_CONTEXT,
            }
        )
    if meta.get("vector_store") or file_item.get("collection_name") or knowflow:
        capabilities.add(SourceCapability.VECTOR)
    is_workbook = _is_workbook_source(file_item)
    if is_workbook:
        capabilities.update({SourceCapability.WORKBOOK_RANGE, SourceCapability.SOURCE_ANCHORS})

    return SourceDescriptor(
        source_id=source_id,
        label=label,
        source_type="workbook" if is_workbook else "file",
        declared_capabilities=frozenset(capabilities),
        metadata={
            "host_resource_type": "file",
            "file_id": source_id,
            "filename": label,
            "content_type": content_type,
            "hash": file_item.get("hash"),
            "meta": _safe_mapping(meta),
            "collection_name": file_item.get("collection_name"),
            "knowflow": knowflow,
            "workbook": _safe_mapping(
                {
                    "file_id": source_id,
                    "filename": label,
                    "content_type": content_type,
                    "workbook_descriptor_id": (
                        meta.get("workbook_descriptor_id")
                        or file_item.get("workbook_descriptor_id")
                    ),
                    "storage_provider_handle": (
                        meta.get("storage_provider_handle")
                        or file_item.get("storage_provider_handle")
                    ),
                }
            )
            if is_workbook
            else {},
        },
    )


def _knowledge_source_descriptor(
    knowledge_item: Mapping[str, Any],
    knowflow_descriptors: Mapping[str, Mapping[str, Any]],
) -> SourceDescriptor:
    source_id = str(knowledge_item["id"])
    meta = _mapping_or_empty(knowledge_item.get("meta"))
    knowflow = _mapping_or_empty(knowflow_descriptors.get(source_id))
    capabilities = {
        SourceCapability.METADATA_INVENTORY,
        SourceCapability.VECTOR,
        *(_capabilities_from_values(meta.get("capabilities"))),
        *(_capabilities_from_values(knowledge_item.get("capabilities"))),
        *(_capabilities_from_values(knowflow.get("capabilities"))),
    }
    return SourceDescriptor(
        source_id=source_id,
        label=_string_or_none(knowledge_item.get("name")) or source_id,
        source_type="knowledge",
        declared_capabilities=frozenset(capabilities),
        metadata={
            "host_resource_type": "knowledge",
            "knowledge_id": source_id,
            "description": knowledge_item.get("description"),
            "meta": meta,
            "file_ids": tuple(
                _stable_ids(
                    file_item.get("id")
                    for file_item in knowledge_item.get("files", ())
                    if isinstance(file_item, Mapping)
                )
            ),
            "knowflow": knowflow,
        },
    )


def _authorized_scope(
    *,
    active_source_scope: Mapping[str, Any] | None,
    authorization_decision: str | Mapping[str, Any],
    fallback_source_ids: tuple[str, ...],
    diagnostics: list[RetrievalDiagnostic],
) -> AuthorizedScope:
    decision = _authorization_decision(authorization_decision)
    reason = _authorization_reason(authorization_decision)
    if decision in {"denied", "blocked"}:
        diagnostics.append(
            _diagnostic(
                code=f"host_scope_{decision}",
                message=f"Host authorization marked selected-source scope {decision}.",
                kind="authorization",
                severity="error",
                details={"reason": reason},
            )
        )
        return AuthorizedScope(decision=decision, source_ids=(), reason=reason)

    scope = active_source_scope or {}
    status = _string_or_none(scope.get("status"))
    if status in {"denied", "blocked"}:
        diagnostics.append(
            _diagnostic(
                code=f"active_source_scope_{status}",
                message=f"Active selected-source scope is {status}.",
                kind="authorization",
                severity="error",
                details={"reason": scope.get("reason")},
            )
        )
        return AuthorizedScope(
            decision="denied" if status == "denied" else "blocked",
            source_ids=(),
            reason=_string_or_none(scope.get("reason")) or status,
        )

    if status and status not in {"resolved", "authorized"}:
        diagnostics.append(
            _diagnostic(
                code="active_source_scope_unresolved",
                message="Active selected-source scope is not resolved.",
                kind="authorization",
                severity="error",
                details={"status": status, "reason": scope.get("reason")},
            )
        )
        return AuthorizedScope(
            decision="blocked",
            source_ids=(),
            reason="active_source_scope_unresolved",
        )

    source_ids = _stable_ids(scope.get("source_ids") or fallback_source_ids)
    if not source_ids:
        diagnostics.append(
            _diagnostic(
                code="active_source_scope_empty",
                message="No selected-source ids were authorized for retrieval.",
                kind="authorization",
                severity="error",
            )
        )
        return AuthorizedScope(
            decision="blocked",
            source_ids=(),
            reason="active_source_scope_empty",
        )

    return AuthorizedScope(
        decision="authorized",
        source_ids=source_ids,
        reason=reason or _string_or_none(scope.get("reason")),
    )


def _authorization_decision(value: str | Mapping[str, Any]) -> str:
    if isinstance(value, Mapping):
        decision = _string_or_none(value.get("decision")) or "authorized"
    else:
        decision = _string_or_none(value) or "authorized"
    if decision not in {"authorized", "denied", "blocked"}:
        return "blocked"
    return decision


def _authorization_reason(value: str | Mapping[str, Any]) -> str | None:
    if isinstance(value, Mapping):
        return _string_or_none(value.get("reason"))
    return None


def _capabilities_from_values(value: Any) -> frozenset[SourceCapability]:
    if value is None:
        return frozenset()
    values = value if isinstance(value, (list, tuple, set, frozenset)) else (value,)
    capabilities: set[SourceCapability] = set()
    for item in values:
        if isinstance(item, SourceCapability):
            capabilities.add(item)
            continue
        capability = _CAPABILITY_NAMES.get(str(item))
        if capability is not None:
            capabilities.add(capability)
    return frozenset(capabilities)


def _authorized_workbook_handles(
    *,
    authorized_sources: tuple[SourceDescriptor, ...],
    workbook_handles: Mapping[str, Mapping[str, Any]],
    diagnostics: list[RetrievalDiagnostic],
) -> Mapping[str, Mapping[str, Any]]:
    if not workbook_handles:
        return {}

    authorized_by_id = {source.source_id: source for source in authorized_sources}
    handles: dict[str, Mapping[str, Any]] = {}
    for source_id, handle in workbook_handles.items():
        source = authorized_by_id.get(source_id)
        if source is None:
            continue
        if SourceCapability.WORKBOOK_RANGE not in source.declared_capabilities:
            diagnostics.append(
                _diagnostic(
                    code="unsupported_workbook_source",
                    message="Workbook routing was requested for a non-XLSX source.",
                    kind="capability",
                    severity="error",
                    source_id=source_id,
                )
            )
            continue
        handles[source_id] = _safe_mapping(handle)

    for source in authorized_sources:
        if (
            SourceCapability.WORKBOOK_RANGE in source.declared_capabilities
            and source.source_id not in handles
        ):
            diagnostics.append(
                _diagnostic(
                    code="missing_workbook_handle",
                    message=(
                        "Selected XLSX source has no safe workbook handle for "
                        "workbook-tool routing."
                    ),
                    kind="capability",
                    severity="warning",
                    source_id=source.source_id,
                )
            )

    return handles


def _invoke_workbook_contract(
    *,
    contract: Any,
    source_id: str,
    workbook_handle: Mapping[str, Any],
    workbook_request: Any,
    operation: Mapping[str, Any],
) -> ConnectorResponse:
    build_request = getattr(contract, "build_workbook_request", None)
    if callable(build_request):
        result = build_request(
            source_id=source_id,
            workbook_handle=workbook_handle,
            workbook_request=workbook_request,
            operation=operation,
        )
    elif callable(contract):
        result = contract(
            source_id=source_id,
            workbook_handle=workbook_handle,
            workbook_request=workbook_request,
            operation=operation,
        )
    else:
        result = None

    if isinstance(result, ConnectorResponse):
        return result

    return ConnectorResponse(
        diagnostics=(
            RetrievalDiagnostic(
                code="host_workbook_contract_request_built",
                message="Workbook tool contract request was built by the host adapter.",
                kind="candidate",
                severity="info",
                phase="host_adapter",
                source_id=source_id,
                details={
                    "workbook_operation": operation,
                    "contract_request": _safe_value(result),
                },
            ),
        )
    )


def _workbook_operation_from_request(workbook_request: Any) -> Mapping[str, Any]:
    if workbook_request is None:
        workbook_request = WorkbookConnectorRequest(needs_manifest=True)
    return _safe_mapping(
        {
            "manifest": bool(getattr(workbook_request, "needs_manifest", False)),
            "sheet_preview": bool(
                getattr(workbook_request, "needs_sheet_preview", False)
            ),
            "sheets": tuple(getattr(workbook_request, "sheet_names", ()) or ()),
            "ranges": tuple(getattr(workbook_request, "ranges", ()) or ()),
            "cells": tuple(getattr(workbook_request, "cells", ()) or ()),
            "table_refs": tuple(getattr(workbook_request, "table_refs", ()) or ()),
            "filter_rows": tuple(getattr(workbook_request, "filter_rows", ()) or ()),
            "formula_state": bool(
                getattr(workbook_request, "needs_formula_state", False)
            ),
            "cache_state": bool(getattr(workbook_request, "needs_cache_state", False)),
            "coverage_state": bool(
                getattr(workbook_request, "needs_coverage_state", False)
            ),
            "source_anchor_hydration": bool(
                getattr(workbook_request, "needs_source_anchor_hydration", False)
            ),
        }
    )


def _sanitize_candidates(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    return tuple(
        replace(
            candidate,
            source_anchor=_safe_mapping(candidate.source_anchor),
            provenance=_safe_mapping(candidate.provenance),
            diagnostics=_sanitize_diagnostics(candidate.diagnostics),
        )
        for candidate in candidates
    )


def _sanitize_diagnostics(
    diagnostics: tuple[RetrievalDiagnostic, ...],
) -> tuple[RetrievalDiagnostic, ...]:
    return tuple(
        replace(diagnostic, details=_safe_mapping(diagnostic.details))
        for diagnostic in diagnostics
    )


def _is_workbook_source(file_item: Mapping[str, Any]) -> bool:
    meta = _mapping_or_empty(file_item.get("meta"))
    filename = (
        _string_or_none(file_item.get("filename"))
        or _string_or_none(file_item.get("name"))
        or ""
    ).lower()
    content_type = (
        _string_or_none(file_item.get("content_type"))
        or _string_or_none(meta.get("content_type"))
        or ""
    ).lower()
    return filename.endswith((".xls", ".xlsx")) or content_type in _WORKBOOK_CONTENT_TYPES


def _safe_mapping(value: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key, raw_value in value.items():
        key_text = str(key)
        if key_text.lower() in _RAW_PATH_KEYS:
            continue
        safe[key_text] = _safe_value(raw_value)
    return safe


def _safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, tuple):
        return tuple(_safe_value(item) for item in value)
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, set):
        return tuple(sorted(str(_safe_value(item)) for item in value))
    if isinstance(value, str) and _looks_like_raw_path(value):
        return "[redacted_path]"
    return value


def _looks_like_raw_path(value: str) -> bool:
    text = value.strip()
    if text.startswith(("/api/v1/files/", "/openai/v1/files/", "/v1/files/")):
        return False
    return text.startswith(("/", "\\")) or (
        len(text) > 2 and text[1] == ":" and text[2] in {"\\", "/"}
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, Mapping) else {}
    return {}


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _stable_ids(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, Iterable) and not isinstance(
        values, (str, bytes, Mapping)
    ):
        items = values
    else:
        items = (values,)
    seen: set[str] = set()
    stable: list[str] = []
    for item in items:
        source_id = _string_or_none(item)
        if source_id and source_id not in seen:
            seen.add(source_id)
            stable.append(source_id)
    return tuple(stable)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _diagnostic(
    *,
    code: str,
    message: str,
    kind: str = "authorization",
    severity: str = "warning",
    source_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> RetrievalDiagnostic:
    return RetrievalDiagnostic(
        code=code,
        message=message,
        kind=kind,
        severity=severity,
        phase="host_adapter",
        source_id=source_id,
        details=details or {},
    )
