import copy
import asyncio
import json
from types import SimpleNamespace

import open_webui.retrieval.utils as retrieval_utils
import open_webui.utils.middleware as middleware
from open_webui.retrieval.engine_adapter import (
    build_retrieval_engine_request,
    build_selected_source_retrieval_engine_package,
)
from open_webui.retrieval.engine_contract import (
    ENGINE_ANSWER_POLICIES,
    ENGINE_FALLBACK_REASONS,
    ENGINE_INVOCATION_TIMEOUT_SECONDS,
    ENGINE_TERMINAL_STATUSES,
    build_engine_error_contract,
    classify_engine_attempt,
    record_selected_source_retrieval_telemetry,
    reset_selected_source_retrieval_telemetry,
    selected_source_engine_operator_disabled,
    selected_source_retrieval_telemetry_counters,
    _retrieval_engine_authority_state,
    _retrieval_engine_reference_dicts,
    _retrieval_engine_result_first_pass_contract,
)
from retrieval_engine import (
    AcceptedOutput,
    AuthorizedScope,
    Candidate,
    ConnectorResponse,
    Reference,
    RetrievalDiagnostic,
    RetrievalRequest,
    RetrievalResult,
    SourceCapability,
)
from retrieval_engine.contracts import WorkbookConnectorRequest
from open_webui.utils.middleware import (
    Chats,
    _attach_generated_files_to_output,
    chat_completion_files_handler,
    _selected_source_scope_is_ambiguous,
    _merge_reference_sidecar_into_metadata,
    _is_media_file_item,
    _is_image_file_item,
    _gate_retrieval_sources,
    _filter_inline_sources_for_selected_files,
    _completion_sources_for_persistence,
    _filter_uncited_no_evidence_source_cards,
    _constrain_retrieval_queries,
    _compare_knowflow_retrieval_against_neutral_contract,
    _build_assistant_reference_seed_metadata,
    _build_assistant_reference_persistence_metadata,
    _merge_persisted_and_response_sources,
    _build_chat_completion_payload,
    _is_selected_source_metadata_first_diagnostics_only,
    _selected_source_requires_stable_limitation_response,
    _apply_selected_source_diagnostics_only_content_guard,
    _enforce_selected_source_metadata_first_final_consistency,
    _append_selected_source_limitation_guard,
    _resolve_active_source_scope,
    _retrieval_engine_observe_selected_source_lane,
    _retrieval_engine_selected_source_lane_package,
    apply_source_context_to_messages,
    handle_responses_streaming_event,
)
from open_webui.utils.task import (
    build_session_user_memory_prompt,
    build_relevant_prior_user_facts_block,
    extract_session_user_facts,
    query_generation_template,
)
from open_webui.utils.tools import query_selected_knowledge_files, read_selected_file

# Controlled fixtures from retrieval-engine/tests/fixtures/local_file_text/
try:
    from tests.fixtures.local_file_text.contracts import (
        engine_authority_contract as _engine_authority_contract_fixture,
        engine_result_stub as _engine_result_stub_fixture,
        engine_contract_bypass_legacy_fixture as _engine_contract_bypass_legacy_pack_fixture,
    )
    from tests.fixtures.local_file_text.sources import (
        local_file_source as _local_file_source_fixture,
        local_file_reference as _local_file_reference_fixture,
    )
    _HAS_FIXTURE_PACK = True
except ImportError:
    _HAS_FIXTURE_PACK = False


def _local_file_source(
    *,
    file_id: str = "file-1",
    name: str = "atlas-note.txt",
    content: str = "Project Atlas note. Owner: Lina Chen. Launch date: 2026-11-03.",
) -> dict:
    return {
        "source": {
            "id": file_id,
            "name": name,
            "url": f"/api/v1/files/{file_id}/content",
            "type": "file",
        },
        "document": [content],
        "metadata": [
            {
                "source": file_id,
                "name": name,
                "file_id": file_id,
            }
        ],
    }


def _resolved_active_source_scope(
    *,
    file_id: str = "file-1",
    name: str = "atlas-note.txt",
    reason: str = "explicit_anchor",
) -> dict:
    return {
        "status": "resolved",
        "source_set_mode": "single",
        "source_ids": [file_id],
        "sources": [{"id": file_id, "name": name, "type": "file"}],
        "reason": reason,
        "confidence": "high",
        "expires_on": "new_upload_or_explicit_change",
    }


def _web_tool_output(
    *,
    tool_name: str,
    tool_args: dict,
    payload: dict,
) -> list[dict]:
    return [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": tool_name,
            "arguments": json.dumps(tool_args, ensure_ascii=False),
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco_1",
            "call_id": "call_1",
            "output": [
                {
                    "type": "input_text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ],
            "status": "completed",
        },
    ]


def _retrieval_engine_result_for_reference(
    *,
    source_anchor: dict,
    text: str = "Accepted engine evidence.",
    source_id: str = "alpha-file",
    reference_id: str = "reference:alpha",
    output_id: str = "accepted:alpha",
    degraded_location: bool = False,
) -> RetrievalResult:
    return RetrievalResult(
        status="success",
        request=RetrievalRequest(
            query="engine query",
            scope=AuthorizedScope(decision="authorized", source_ids=(source_id,)),
            sources=(),
        ),
        accepted_outputs=(
            AcceptedOutput(
                output_id=output_id,
                text=text,
                evidence_bundle_ids=("bundle:alpha",),
            ),
        ),
        references=(
            Reference(
                reference_id=reference_id,
                source_id=source_id,
                label="alpha-policy.txt",
                source_anchor=source_anchor,
                degraded_location=degraded_location,
            ),
        ),
    )


def test_retrieval_engine_adapter_builds_authorized_selected_file_scope():
    result = build_retrieval_engine_request(
        query="What is the Atlas launch date?",
        selected_file_ids=("file-1", "file-2"),
        selected_files=(
            {
                "id": "file-1",
                "filename": "atlas-note.txt",
                "hash": "hash-1",
                "data": {"content": "Project Atlas launches on 2026-11-03."},
                "meta": {"content_type": "text/plain", "vector_store": True},
            },
            {
                "id": "file-2",
                "filename": "off-scope.txt",
                "data": {"content": "Do not retrieve this file."},
                "meta": {"content_type": "text/plain", "vector_store": True},
            },
        ),
        active_source_scope={
            "status": "resolved",
            "source_ids": ["file-1"],
            "reason": "explicit_anchor",
        },
        authorization_generation="auth-gen-1",
        vector_store_handles={
            "file-1": {
                "collection_name": "file-file-1",
                "index_generation": "idx-1",
                "capabilities": ["vector"],
            },
        },
    )

    assert result.request.scope.decision == "authorized"
    assert result.request.scope.source_ids == ("file-1",)
    assert [source.source_id for source in result.request.sources] == ["file-1"]
    assert result.request.execution_context["authorization_generation"] == "auth-gen-1"
    assert result.connectors

    declarations = result.connectors[0].discover_capabilities(result.request.sources)
    assert tuple(declarations) == ("file-1",)
    assert declarations["file-1"].index_generation == "idx-1"


def test_retrieval_engine_adapter_fails_closed_before_connector_construction():
    denied = build_retrieval_engine_request(
        query="What is in the selected file?",
        selected_file_ids=("file-1",),
        selected_files=({"id": "file-1", "filename": "atlas-note.txt"},),
        active_source_scope={"status": "resolved", "source_ids": ["file-1"]},
        authorization_decision={"decision": "denied", "reason": "policy_denied"},
        vector_store_handles={"file-1": {"collection_name": "file-file-1"}},
    )

    assert denied.request.scope.decision == "denied"
    assert denied.request.sources == ()
    assert denied.connectors == ()
    assert {diagnostic.code for diagnostic in denied.diagnostics} == {
        "host_scope_denied"
    }

    unresolved = build_retrieval_engine_request(
        query="What is in the selected file?",
        selected_file_ids=("file-1",),
        selected_files=({"id": "file-1", "filename": "atlas-note.txt"},),
        active_source_scope={
            "status": "unresolved",
            "source_ids": ["file-1"],
            "reason": "ambiguous_selected_source",
        },
        vector_store_handles={"file-1": {"collection_name": "file-file-1"}},
    )

    assert unresolved.request.scope.decision == "blocked"
    assert unresolved.request.sources == ()
    assert unresolved.connectors == ()
    assert "active_source_scope_unresolved" in {
        diagnostic.code for diagnostic in unresolved.diagnostics
    }

    missing = build_retrieval_engine_request(
        query="What is in the selected file?",
        selected_file_ids=("missing-file",),
        selected_files=(),
        active_source_scope={"status": "resolved", "source_ids": ["missing-file"]},
        vector_store_handles={"missing-file": {"collection_name": "file-missing"}},
    )

    assert missing.request.scope.decision == "blocked"
    assert missing.request.sources == ()
    assert missing.connectors == ()
    assert {"missing_selected_file", "unresolved_selected_source_scope"}.issubset(
        {diagnostic.code for diagnostic in missing.diagnostics}
    )


def test_retrieval_engine_adapter_preserves_knowledge_knowflow_and_vector_metadata():
    result = build_retrieval_engine_request(
        query="Summarize the refinery knowledge base.",
        selected_file_ids=("file-1",),
        selected_files=(
            {
                "id": "file-1",
                "filename": "refinery.pdf",
                "meta": {"vector_store": True},
            },
        ),
        knowledge_selections=(
            {
                "id": "knowledge-1",
                "name": "Refinery Knowledge",
                "description": "Operations corpus",
                "files": [{"id": "file-1"}],
            },
        ),
        active_source_scope={
            "status": "resolved",
            "source_ids": ["knowledge-1"],
        },
        vector_store_handles={
            "knowledge-1": {
                "collection_name": "knowledge-1",
                "capabilities": ["vector", "metadata_inventory"],
                "embedding_model_settings": {"model": "test-embedding"},
            },
        },
        vector_config={"backend": "noop"},
        knowflow_descriptors={
            "knowledge-1": {
                "dataset_id": "kf-dataset-1",
                "capabilities": ["vector", "metadata_inventory"],
                "index_generation": "knowflow-idx-9",
            },
        },
    )

    assert [source.source_id for source in result.request.sources] == ["knowledge-1"]
    source = result.request.sources[0]
    assert source.metadata["knowledge_id"] == "knowledge-1"
    assert source.metadata["file_ids"] == ("file-1",)
    assert source.metadata["knowflow"]["dataset_id"] == "kf-dataset-1"
    assert result.connectors[0].vector_handles["knowledge-1"]["collection_name"] == (
        "knowledge-1"
    )

    declarations = result.connectors[0].discover_capabilities(result.request.sources)
    declaration = declarations["knowledge-1"]
    assert declaration.contextual_embedding_metadata == {"model": "test-embedding"}
    assert declaration.contextual_index_metadata["backend"] == "noop"


def test_retrieval_engine_adapter_is_candidate_only_boundary():
    result = build_retrieval_engine_request(
        query="What is the Atlas launch date?",
        selected_file_ids=("file-1",),
        selected_files=(
            {
                "id": "file-1",
                "filename": "atlas-note.txt",
                "meta": {"vector_store": True},
            },
        ),
        active_source_scope={"status": "resolved", "source_ids": ["file-1"]},
        vector_store_handles={"file-1": {"collection_name": "file-file-1"}},
    )

    assert not hasattr(result, "accepted_outputs")
    assert not hasattr(result, "references")
    assert not hasattr(result, "source_cards")

    response = result.connectors[0].generate_candidates(
        request=result.request,
        plan=SimpleNamespace(source_ids=("file-1",)),
        sources=result.request.sources,
    )
    assert response.candidates == ()
    assert not hasattr(response, "accepted_outputs")
    assert not hasattr(response, "references")
    assert response.diagnostics[0].code == "host_vector_retrieval_not_executed"


def test_selected_source_engine_package_normalizes_selected_files_in_adapter():
    raw_path = "/srv/open-webui/uploads/private/atlas-note.txt"
    package = build_selected_source_retrieval_engine_package(
        query="What is the Atlas launch date?",
        selected_files=(
            {
                "id": "file-1",
                "filename": "atlas-note.txt",
                "path": raw_path,
                "meta": {"path": raw_path, "content_type": "text/plain"},
                "data": {"content": "Project Atlas launches on 2026-11-03."},
            },
            {
                "id": "file-1",
                "filename": "atlas-note-duplicate.txt",
                "data": {"content": "Duplicate should be ignored."},
            },
        ),
        active_source_scope={
            "status": "resolved",
            "source_ids": ["file-1"],
            "reason": "explicit_anchor",
        },
        authorization_generation="auth-gen-1",
        execution_context={"engine_version": "open_webui_selected_source_engine_v1"},
    )

    assert package is not None
    assert package.unsupported_reason == ""
    assert len(package.observed_files) == 1
    assert package.observed_files[0]["id"] == "file-1"
    assert package.observed_files[0]["data"]["content"] == "Project Atlas launches on 2026-11-03."
    assert raw_path not in repr(package.observed_files)
    assert package.adapter_result is not None
    assert package.adapter_result.request.scope.source_ids == ("file-1",)
    assert len(package.runtime_connectors) == 3


def test_selected_source_engine_package_reports_unsupported_source_shape():
    package = build_selected_source_retrieval_engine_package(
        query="What is the Atlas launch date?",
        selected_files=(
            {
                "id": "file-1",
                "filename": "atlas-note.txt",
                "meta": {"content_type": "text/plain"},
            },
        ),
        active_source_scope={
            "status": "resolved",
            "source_ids": ["file-1"],
            "reason": "explicit_anchor",
        },
        authorization_generation="auth-gen-1",
        execution_context={"engine_version": "open_webui_selected_source_engine_v1"},
    )

    assert package is not None
    assert package.unsupported_reason == "unsupported_source_shape"
    assert package.adapter_result is None
    assert package.runtime_connectors == ()
    assert package.observed_files[0]["id"] == "file-1"


def test_retrieval_engine_observe_lane_runs_without_legacy_retrieval_helper(
    monkeypatch,
):
    async def fail_legacy_retrieval(*_args, **_kwargs):
        raise AssertionError("engine observe must not call legacy retrieval")

    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fail_legacy_retrieval,
    )

    observe = _retrieval_engine_observe_selected_source_lane(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "path": raw_path,
                "meta": {"path": raw_path, "content_type": "text/plain"},
                "data": {
                    "content": (
                        "Transformer grounding aligns model outputs with source text."
                    )
                },
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    assert observe["mode"] == "observe_parallel"
    assert observe["authority"] == "retrieval_engine"
    assert observe["status"] == "success"
    assert observe["terminal_reason"] == "success"
    assert observe["counts"]["accepted_output_count"] >= 1
    assert observe["counts"]["reference_count"] >= 1
    assert observe["plan_summary"]["evidence_shape"] == "narrow_chunk"
    assert observe["plan_summary"]["source_ids"] == ["alpha-file"]
    assert observe["plan_summary"]["first_pass_evidence_state"] == "accepted"
    assert observe["comparison"]["user_visible_authority"] == "retrieval_engine"
    assert observe["comparison"]["legacy_reference_count"] == 1
    assert "accepted_outputs" not in observe
    assert "references" not in observe
    assert "source_cards" not in observe
    assert raw_path not in repr(observe)


def test_retrieval_engine_observe_failure_is_diagnostic_only(monkeypatch):
    def fail_adapter(*_args, **_kwargs):
        raise RuntimeError("adapter unavailable at /srv/private/should-not-leak")

    monkeypatch.setattr(
        "open_webui.retrieval.engine_adapter.build_retrieval_engine_request",
        fail_adapter,
    )

    observe = _retrieval_engine_observe_selected_source_lane(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {
                    "content": (
                        "Transformer grounding aligns model outputs with source text."
                    )
                },
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    assert observe["status"] == "error"
    assert observe["terminal_reason"] == "request_construction_error"
    assert observe["counts"]["accepted_output_count"] == 0
    assert observe["counts"]["reference_count"] == 0
    assert observe["comparison"]["legacy_status"] == "success"
    assert observe["comparison"]["legacy_reference_count"] == 1
    assert observe["diagnostics"][0]["code"] == (
        "retrieval_engine_request_construction_failed"
    )
    assert "accepted_outputs" not in observe
    assert "references" not in observe
    assert "source_cards" not in observe
    assert "/srv/private/should-not-leak" not in repr(observe)


def test_selected_source_engine_post_invocation_exception_returns_typed_error_contract(
    monkeypatch,
):
    class RaisingEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _request):
            raise RuntimeError("engine failed after invocation")

    monkeypatch.setattr("retrieval_engine.RetrievalEngine", RaisingEngine)

    package = _retrieval_engine_selected_source_lane_package(
        prompt="What does the selected file say?",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "type": "text",
                "data": {"content": "Alpha selected file text."},
                "meta": {"content_type": "text/plain"},
            }
        ],
        active_source_scope=_resolved_active_source_scope(
            file_id="alpha-file",
            name="alpha-policy.txt",
        ),
        legacy_status="pending",
        legacy_terminal_reason="pending",
        legacy_sources=[],
        legacy_references=[],
        legacy_accepted_outputs=[],
    )

    assert isinstance(package, dict)
    contract = package.get("contract")
    assert isinstance(contract, dict)
    assert contract["status"] == "error"
    assert contract["terminal_reason"] == "engine_observe_error"
    assert contract["accepted_outputs"] == []
    assert contract["references"] == []
    assert contract["sources"] == []
    assert package["authority"]["state"] == "engine_owned"


class _FakeWorkbookContract:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def build_workbook_request(
        self,
        *,
        source_id,
        workbook_handle,
        workbook_request,
        operation,
    ):
        self.calls.append(
            {
                "source_id": source_id,
                "workbook_handle": workbook_handle,
                "workbook_request": workbook_request,
                "operation": operation,
            }
        )
        if self.response is not None:
            return self.response
        return {
            "tool": "xlsx_read_workbook",
            "source_id": source_id,
            "file_id": workbook_handle["file_id"],
            "operation": operation,
            "path": "/srv/open-webui/uploads/should-not-leak.xlsx",
        }


def test_retrieval_engine_adapter_builds_authorized_xlsx_workbook_boundary():
    raw_path = "/srv/open-webui/uploads/private/budget.xlsx"
    fake_contract = _FakeWorkbookContract(
        response=ConnectorResponse(
            candidates=(
                Candidate(
                    candidate_id="wb-candidate-1",
                    source_id="workbook-1",
                    capability=SourceCapability.WORKBOOK_RANGE,
                    body="Summary B2 = 42",
                    source_anchor={
                        "sheet": "Summary",
                        "cell": "B2",
                        "server_path": raw_path,
                    },
                    provenance={"file_path": raw_path, "operation": "range"},
                ),
            ),
            diagnostics=(
                RetrievalDiagnostic(
                    code="fake_workbook_contract_called",
                    message="fake workbook call",
                    phase="host_adapter",
                    source_id="workbook-1",
                    details={"path": raw_path, "sheet": "Summary"},
                ),
            ),
        )
    )
    result = build_retrieval_engine_request(
        query="What is the value in Summary!B2?",
        selected_file_ids=("workbook-1", "workbook-2"),
        selected_files=(
            {
                "id": "workbook-1",
                "filename": "budget.xlsx",
                "path": raw_path,
                "meta": {
                    "content_type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                    "path": raw_path,
                    "workbook_descriptor_id": "wb-desc-1",
                },
            },
            {
                "id": "workbook-2",
                "filename": "unselected.xlsx",
                "path": "/srv/open-webui/uploads/private/unselected.xlsx",
                "meta": {
                    "content_type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                },
            },
        ),
        active_source_scope={"status": "resolved", "source_ids": ["workbook-1"]},
        workbook_handles={
            "workbook-1": {
                "file_id": "workbook-1",
                "filename": "budget.xlsx",
                "storage_provider_handle": "files:workbook-1",
                "server_path": raw_path,
            },
            "workbook-2": {
                "file_id": "workbook-2",
                "filename": "unselected.xlsx",
                "server_path": "/srv/open-webui/uploads/private/unselected.xlsx",
            },
        },
        workbook_contracts={"workbook-1": fake_contract},
    )

    assert [source.source_id for source in result.request.sources] == ["workbook-1"]
    source = result.request.sources[0]
    assert source.source_type == "workbook"
    assert SourceCapability.WORKBOOK_RANGE in source.declared_capabilities
    assert SourceCapability.SOURCE_ANCHORS in source.declared_capabilities
    assert raw_path not in repr(result.request)

    workbook_connector = next(
        connector
        for connector in result.connectors
        if connector.connector_id == "open_webui_workbook"
    )
    assert tuple(workbook_connector.workbook_handles) == ("workbook-1",)
    assert raw_path not in repr(workbook_connector.workbook_handles)

    declarations = workbook_connector.discover_capabilities(result.request.sources)
    assert tuple(declarations) == ("workbook-1",)
    assert SourceCapability.WORKBOOK_RANGE in declarations["workbook-1"].capabilities

    response = workbook_connector.generate_candidates(
        request=result.request,
        plan=SimpleNamespace(
            workbook_request=WorkbookConnectorRequest(
                needs_manifest=True,
                needs_sheet_preview=True,
                sheet_names=("Summary",),
                ranges=("B2:C3",),
                cells=("B2",),
                table_refs=("RevenueTable",),
                filter_rows=("Region=North",),
                needs_formula_state=True,
                needs_cache_state=True,
                needs_coverage_state=True,
                needs_source_anchor_hydration=True,
            )
        ),
        sources=result.request.sources,
    )

    assert fake_contract.calls[0]["source_id"] == "workbook-1"
    assert fake_contract.calls[0]["workbook_handle"] == {
        "file_id": "workbook-1",
        "filename": "budget.xlsx",
        "storage_provider_handle": "files:workbook-1",
    }
    operation = fake_contract.calls[0]["operation"]
    assert operation["manifest"] is True
    assert operation["sheet_preview"] is True
    assert operation["sheets"] == ("Summary",)
    assert operation["ranges"] == ("B2:C3",)
    assert operation["cells"] == ("B2",)
    assert operation["table_refs"] == ("RevenueTable",)
    assert operation["filter_rows"] == ("Region=North",)
    assert operation["formula_state"] is True
    assert operation["cache_state"] is True
    assert operation["coverage_state"] is True
    assert operation["source_anchor_hydration"] is True

    assert response.candidates[0].source_anchor == {"sheet": "Summary", "cell": "B2"}
    assert response.candidates[0].provenance == {"operation": "range"}
    assert response.diagnostics[0].details == {"sheet": "Summary"}
    assert raw_path not in repr(response)
    assert not hasattr(response, "accepted_outputs")
    assert not hasattr(response, "references")
    assert not hasattr(response, "source_cards")


def test_retrieval_engine_adapter_blocks_workbook_routing_when_scope_not_authorized():
    workbook_file = {
        "id": "workbook-1",
        "filename": "budget.xlsx",
        "meta": {
            "content_type": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        },
    }
    handle = {"workbook-1": {"file_id": "workbook-1", "filename": "budget.xlsx"}}

    denied = build_retrieval_engine_request(
        query="Read Summary!B2",
        selected_file_ids=("workbook-1",),
        selected_files=(workbook_file,),
        active_source_scope={"status": "resolved", "source_ids": ["workbook-1"]},
        authorization_decision={"decision": "denied", "reason": "policy_denied"},
        workbook_handles=handle,
    )
    assert denied.request.scope.decision == "denied"
    assert not any(
        connector.connector_id == "open_webui_workbook"
        for connector in denied.connectors
    )

    unresolved = build_retrieval_engine_request(
        query="Read Summary!B2",
        selected_file_ids=("workbook-1",),
        selected_files=(workbook_file,),
        active_source_scope={
            "status": "unresolved",
            "source_ids": ["workbook-1"],
            "reason": "ambiguous_workbook",
        },
        workbook_handles=handle,
    )
    assert unresolved.request.scope.decision == "blocked"
    assert not any(
        connector.connector_id == "open_webui_workbook"
        for connector in unresolved.connectors
    )

    missing = build_retrieval_engine_request(
        query="Read Summary!B2",
        selected_file_ids=("missing-workbook",),
        selected_files=(),
        active_source_scope={"status": "resolved", "source_ids": ["missing-workbook"]},
        workbook_handles={
            "missing-workbook": {
                "file_id": "missing-workbook",
                "filename": "missing.xlsx",
            }
        },
    )
    assert missing.request.scope.decision == "blocked"
    assert not any(
        connector.connector_id == "open_webui_workbook"
        for connector in missing.connectors
    )


def test_retrieval_engine_adapter_rejects_non_xlsx_workbook_routing():
    result = build_retrieval_engine_request(
        query="Read Summary!B2",
        selected_file_ids=("note-1",),
        selected_files=(
            {
                "id": "note-1",
                "filename": "notes.txt",
                "meta": {"content_type": "text/plain"},
            },
        ),
        active_source_scope={"status": "resolved", "source_ids": ["note-1"]},
        workbook_handles={"note-1": {"file_id": "note-1", "filename": "notes.txt"}},
    )

    assert result.request.scope.decision == "authorized"
    assert not any(
        connector.connector_id == "open_webui_workbook"
        for connector in result.connectors
    )
    assert "unsupported_workbook_source" in {
        diagnostic.code for diagnostic in result.diagnostics
    }


def test_retrieval_engine_text_reference_normalizes_to_reference_card_with_anchor():
    raw_path = "/srv/open-webui/uploads/private/alpha.txt"
    result = _retrieval_engine_result_for_reference(
        source_anchor={
            "kind": "text_span",
            "source_id": "alpha-file",
            "chunk_id": "chunk-7",
            "page": 4,
            "start": 12,
            "end": 38,
            "server_path": raw_path,
        },
        text="Transformer grounding aligns outputs with source text.",
        reference_id="reference:chunk-7",
        output_id="accepted:chunk-7",
    )

    references, source_cards = _retrieval_engine_reference_dicts(
        result,
        {
            "alpha-file": {
                "id": "alpha-file",
                "filename": "alpha-policy.txt",
                "type": "file",
            }
        },
    )

    assert references == source_cards
    reference = references[0]
    metadata = reference["metadata"][0]
    provenance = reference["provenance"]
    assert reference["source"]["id"] == "alpha-file"
    assert metadata["chunk_id"] == "chunk-7"
    assert metadata["page"] == 4
    assert metadata["text_span"] == {"start": 12, "end": 38}
    assert metadata["source_id"] == "alpha-file"
    assert metadata["reference_id"] == "reference:chunk-7"
    assert metadata["output_id"] == "accepted:chunk-7"
    assert metadata["source_location"]["state"] == "anchored"
    assert provenance["source_anchor"]["chunk_id"] == "chunk-7"
    assert raw_path not in repr(references)


def test_retrieval_engine_workbook_reference_preserves_sheet_range_and_cell():
    result = _retrieval_engine_result_for_reference(
        source_id="workbook-1",
        source_anchor={
            "kind": "workbook_range",
            "source_id": "workbook-1",
            "sheet": "Summary",
            "range": "B2:C3",
            "cell": "B2",
        },
    )

    references, _ = _retrieval_engine_reference_dicts(
        result,
        {"workbook-1": {"id": "workbook-1", "filename": "budget.xlsx", "type": "file"}},
    )

    metadata = references[0]["metadata"][0]
    assert metadata["sheet"] == "Summary"
    assert metadata["range"] == "B2:C3"
    assert metadata["cell"] == "B2"
    assert metadata["source_anchor"]["sheet"] == "Summary"
    assert metadata["source_location"]["state"] == "anchored"


def test_retrieval_engine_table_reference_preserves_structured_anchor_metadata():
    result = _retrieval_engine_result_for_reference(
        source_anchor={
            "kind": "row",
            "source_id": "table-source",
            "table": "Table 4",
            "row": 7,
            "field": "labor",
            "provider_region": "body",
        },
        source_id="table-source",
    )

    references, _ = _retrieval_engine_reference_dicts(
        result,
        {"table-source": {"id": "table-source", "filename": "quota-table.pdf"}},
    )

    metadata = references[0]["metadata"][0]
    assert metadata["table"] == "Table 4"
    assert metadata["row"] == 7
    assert metadata["field"] == "labor"
    assert metadata["provider_region"] == "body"
    assert metadata["source_anchor"]["table"] == "Table 4"
    assert metadata["source_location"]["state"] == "anchored"


def test_retrieval_engine_degraded_location_is_explicit_without_invented_geometry():
    result = _retrieval_engine_result_for_reference(
        source_anchor={
            "kind": "chunk",
            "source_id": "alpha-file",
            "chunk_id": "chunk-without-geometry",
        },
        degraded_location=True,
    )

    references, _ = _retrieval_engine_reference_dicts(
        result,
        {"alpha-file": {"id": "alpha-file", "filename": "alpha-policy.txt"}},
    )

    metadata = references[0]["metadata"][0]
    assert metadata["chunk_id"] == "chunk-without-geometry"
    assert metadata["degraded_location"] is True
    assert metadata["source_location"]["state"] == "degraded"
    assert metadata["source_location"]["reason"] == "engine_marked_degraded"
    assert "page" not in metadata
    assert "range" not in metadata
    assert "text_span" not in metadata


def test_retrieval_engine_diagnostics_only_result_does_not_create_reference_cards():
    result = RetrievalResult(
        status="no_evidence",
        terminal_reason="no_accepted_evidence",
        request=RetrievalRequest(
            query="engine query",
            scope=AuthorizedScope(decision="authorized", source_ids=("alpha-file",)),
            sources=(),
        ),
        candidates=(
            Candidate(
                candidate_id="rejected-1",
                source_id="alpha-file",
                capability=SourceCapability.EXACT_TEXT,
                body="Rejected diagnostic-only body.",
                source_anchor={"kind": "diagnostic", "source_id": "alpha-file"},
                weak=True,
            ),
        ),
        diagnostics=(
            RetrievalDiagnostic(
                code="weak_candidate",
                message="weak candidate",
                kind="acceptance",
                source_id="alpha-file",
                candidate_id="rejected-1",
            ),
        ),
    )

    references, source_cards = _retrieval_engine_reference_dicts(
        result,
        {"alpha-file": {"id": "alpha-file", "filename": "alpha-policy.txt"}},
    )

    assert references == []
    assert source_cards == []


def test_historical_selected_source_reference_reads_without_engine_authority():
    references = Chats.build_canonical_references(
        {
            "source": {
                "id": "legacy-file",
                "name": "legacy.txt",
                "type": "file",
            },
            "document": ["Historical selected-source evidence."],
            "metadata": [
                {
                    "source": "legacy-file",
                    "file_id": "legacy-file",
                    "chunk_id": "legacy-chunk",
                }
            ],
        }
    )

    assert len(references) == 1
    reference = references[0]
    assert reference["source"]["id"] == "legacy-file"
    assert reference["provenance"]["chunk_id"] == "legacy-chunk"
    assert "engine_authority" not in reference
    assert "engine_authority" not in reference.get("provenance", {})


def test_response_completed_keeps_streamed_tool_items_when_final_output_drops_them():
    current_output = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "pdf_create_document",
            "arguments": '{"title":"Test"}',
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco_1",
            "call_id": "call_1",
            "output": [{"type": "input_text", "text": '{"status":"ok"}'}],
            "status": "completed",
        },
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "PDF ready"}],
        },
    ]

    completed_event = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "PDF ready"}],
                }
            ],
            "usage": {"total_tokens": 42},
        },
    }

    new_output, metadata = handle_responses_streaming_event(
        completed_event, current_output
    )

    assert [item["type"] for item in new_output] == [
        "function_call",
        "function_call_output",
        "message",
    ]
    assert metadata == {"usage": {"total_tokens": 42}, "done": True}


def test_response_completed_uses_final_output_when_it_keeps_tool_items():
    current_output = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "pdf_create_document",
            "arguments": '{"title":"Draft"}',
            "status": "in_progress",
        }
    ]

    final_output = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "pdf_create_document",
            "arguments": '{"title":"Final"}',
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco_1",
            "call_id": "call_1",
            "output": [{"type": "input_text", "text": '{"status":"ok"}'}],
            "status": "completed",
        },
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "PDF ready"}],
        },
    ]

    completed_event = {
        "type": "response.completed",
        "response": {
            "output": final_output,
            "usage": {"total_tokens": 7},
        },
    }

    new_output, metadata = handle_responses_streaming_event(
        completed_event, current_output
    )

    assert new_output == final_output
    assert metadata == {"usage": {"total_tokens": 7}, "done": True}


def test_attach_generated_files_to_latest_function_call_output_for_streaming_delta():
    output = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "pdf_create_document",
            "arguments": '{"title":"Streaming"}',
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco_1",
            "call_id": "call_1",
            "output": [{"type": "input_text", "text": '{"status":"ok"}'}],
            "status": "completed",
        },
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "in_progress",
            "content": [{"type": "output_text", "text": "PDF ready"}],
        },
    ]

    generated_files = [
        {
            "id": "generated-1",
            "url": "/v1/generated-files/generated-1.pdf",
            "name": "streamed.pdf",
            "size": 1024,
            "content_type": "application/pdf",
        }
    ]

    updated_output = _attach_generated_files_to_output(output, generated_files)
    _, payload = _build_chat_completion_payload(
        updated_output, fallback_content="PDF ready"
    )

    tool_result_item = next(
        item for item in payload["output"] if item.get("type") == "function_call_output"
    )

    assert tool_result_item["files"] == generated_files


def test_build_relevant_prior_user_facts_block_keeps_salient_follow_up_fact():
    messages = [
        {
            "role": "user",
            "content": "我的工龄满了30年，今年请了10天病假，还能再休多少天？",
        },
        {"role": "assistant", "content": "根据制度，我先帮您看病假与工龄对应关系。"},
        {"role": "user", "content": "我请了一个月病假，是否影响我的薪酬"},
    ]

    facts_block = build_relevant_prior_user_facts_block(messages)

    assert "工龄满了30年" in facts_block
    assert "prior_user_facts" in facts_block


def test_query_generation_template_appends_relevant_prior_user_facts():
    messages = [
        {
            "role": "user",
            "content": "我的工龄满了30年，今年请了10天病假，还能再休多少天？",
        },
        {"role": "assistant", "content": "我来帮您核对制度。"},
        {"role": "user", "content": "我请了一个月病假，是否影响我的薪酬"},
    ]

    rendered = query_generation_template(
        "### Chat History:\n{{MESSAGES:END:6}}",
        messages,
    )

    assert "Relevant Prior User Facts" in rendered
    assert "工龄满了30年" in rendered


def test_query_generation_template_carries_forward_general_technical_context():
    messages = [
        {
            "role": "user",
            "content": "My server is Ubuntu 22.04 on ARM64, and nginx started returning 502s right after I deployed build 1.14.3.",
        },
        {"role": "assistant", "content": "I can help you narrow that down."},
        {"role": "user", "content": "Would increasing the timeout fix it?"},
    ]

    rendered = query_generation_template(
        "### Chat History:\n{{MESSAGES:END:6}}",
        messages,
    )

    assert "Relevant Prior User Facts" in rendered
    assert "Ubuntu 22.04" in rendered
    assert "ARM64" in rendered
    assert "1.14.3" in rendered


def test_extract_session_user_facts_keeps_general_concrete_context():
    messages = [
        {"role": "user", "content": "Can you help me debug this?"},
        {
            "role": "user",
            "content": "My server is Ubuntu 22.04 on ARM64, and nginx started returning 502s right after I deployed build 1.14.3.",
        },
        {"role": "assistant", "content": "Sure."},
        {"role": "user", "content": "I also have a staging URL at https://staging.example.com."},
    ]

    facts = extract_session_user_facts(messages)

    assert any("Ubuntu 22.04" in fact["text"] for fact in facts)
    assert any("https://staging.example.com" in fact["text"] for fact in facts)
    assert all("Can you help me debug this?" != fact["text"] for fact in facts)


def test_build_session_user_memory_prompt_uses_relevant_stored_facts():
    messages = [
        {
            "role": "user",
            "content": "My server is Ubuntu 22.04 on ARM64, and nginx started returning 502s right after I deployed build 1.14.3.",
        },
        {"role": "assistant", "content": "I can help narrow that down."},
        {"role": "user", "content": "Would increasing the timeout fix it?"},
    ]
    stored_facts = [
        {
            "text": "My server is Ubuntu 22.04 on ARM64, and nginx started returning 502s right after I deployed build 1.14.3.",
            "confidence": "high",
        },
        {
            "text": "I am planning a vacation in July.",
            "confidence": "medium",
        },
    ]

    prompt = build_session_user_memory_prompt(messages, stored_facts=stored_facts)

    assert "Session User Memory" in prompt
    assert "Ubuntu 22.04" in prompt
    assert "ARM64" in prompt
    assert "1.14.3" in prompt
    assert "vacation in July" not in prompt


def test_apply_source_context_to_messages_adds_follow_up_context_guidance():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    RAG_TEMPLATE="### Task:\n<context>\n{{CONTEXT}}\n</context>"
                )
            )
        )
    )
    messages = [
        {
            "role": "user",
            "content": "我的工龄满了30年，今年请了10天病假，还能再休多少天？",
        },
        {"role": "assistant", "content": "我先核对制度口径。"},
        {"role": "user", "content": "我请了一个月病假，是否影响我的薪酬"},
    ]
    sources = [
        {
            "document": ["病假工资待遇按公司本部工作年限执行。"],
            "metadata": [{"source": "制度"}],
            "source": {"id": "hr-policy", "name": "制度"},
        }
    ]

    updated_messages = apply_source_context_to_messages(
        request,
        messages,
        sources,
        "我请了一个月病假，是否影响我的薪酬",
    )

    guidance_message = next(
        message
        for message in updated_messages
        if "Follow-Up Context Guidance" in str(message.get("content", ""))
    )

    assert guidance_message["role"] in {"system", "user"}
    assert (
        "narrower term than the user's earlier wording"
        in guidance_message["content"]
    )
    assert "工龄满了30年" in guidance_message["content"]


def test_selected_file_reference_persists_when_active_scope_is_resolved():
    metadata = {
        "active_source_scope": _resolved_active_source_scope(),
        "sources": [_local_file_source()],
    }

    persisted = _build_assistant_reference_persistence_metadata(metadata)

    assert persisted["active_source_scope"]["status"] == "resolved"
    assert len(persisted["canonical_references"]) == 1

    reference = persisted["canonical_references"][0]
    assert reference["source"]["id"] == "file-1"
    assert reference["source"]["name"] == "atlas-note.txt"
    assert reference["source"]["type"] == "file"
    assert reference.get("source_class") not in {"official_web", "generic_web"}


def test_active_source_scope_persists_focus_model_separate_from_citations():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "active_source_scope": _resolved_active_source_scope(
                reason="current_turn_upload",
            ),
            "sources": [_local_file_source()],
        }
    )

    active_scope = persisted["active_source_scope"]

    assert active_scope["focus_state"] == "single"
    assert active_scope["source_ids"] == ["file-1"]
    assert active_scope["authority"] == "current_upload"
    assert active_scope["validity_reason"] == "current_turn_upload"
    assert active_scope["follow_up"]["reuse"] is True
    assert "canonical_references" in persisted
    assert persisted["canonical_references"] is not active_scope


def test_selected_file_followup_reuses_previous_single_source_focus():
    previous_assistant = {
        "role": "assistant",
        "metadata": {
            "active_source_scope": _resolved_active_source_scope(
                file_id="file-2",
                name="delta-approval.txt",
                reason="current_turn_upload",
            )
        },
    }
    fallback_file = {
        "id": "file-2",
        "name": "delta-approval.txt",
        "type": "file",
        "url": "/api/v1/files/file-2/content",
        "focus_origin": "history",
        "focus_tier": "reference",
    }

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "后面呢",
        [],
        stored_messages=[previous_assistant],
        fallback_candidates=[fallback_file],
    )

    assert blocked is False
    assert scope["status"] == "resolved"
    assert scope["source_ids"] == ["file-2"]
    assert resolved_files == [fallback_file]

    persisted = _build_assistant_reference_persistence_metadata(
        {
            "active_source_scope": scope,
            "sources": [
                _local_file_source(
                    file_id="file-2",
                    name="delta-approval.txt",
                    content="3. CFO signoff. 4. Board notice.",
                )
            ],
        }
    )

    assert persisted["active_source_scope"]["source_ids"] == ["file-2"]
    assert len(persisted["canonical_references"]) == 1
    assert persisted["canonical_references"][0]["source"]["id"] == "file-2"


def test_mixed_file_and_web_references_persist_once_each():
    web_sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_web_tool_output(
            tool_name="visit_webpage",
            tool_args={
                "url": "https://travel.state.gov/content/travel/en/passports/how-apply/processing-times.html"
            },
            payload={
                "url": "https://travel.state.gov/content/travel/en/passports/how-apply/processing-times.html",
                "source_class": "official_web",
                "authority": "official",
                "content": "Routine service can take 4 to 6 weeks.",
                "provider": "browser",
                "as_of": "2026-05-15",
            },
        )
    )
    web_reference = web_sidecar["canonical_references"][0]

    persisted = _build_assistant_reference_persistence_metadata(
        {
            "active_source_scope": _resolved_active_source_scope(
                file_id="file-3",
                name="passport-sla.txt",
            ),
            "canonical_references": [web_reference],
            "sources": [
                _local_file_source(
                    file_id="file-3",
                    name="passport-sla.txt",
                    content="Internal passport promise: 5 weeks end-to-end.",
                ),
                web_reference,
            ],
        }
    )

    references = persisted["canonical_references"]
    assert len(references) == 2
    assert sum(ref["source"].get("type") == "file" for ref in references) == 1
    assert sum(ref.get("source_class") == "official_web" for ref in references) == 1


def test_ambiguous_scope_does_not_synthesize_local_references():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "active_source_scope": {
                "status": "ambiguous",
                "source_set_mode": "none",
                "source_ids": ["file-a", "file-b"],
                "sources": [
                    {"id": "file-a", "name": "alpha.txt", "type": "file"},
                    {"id": "file-b", "name": "beta.txt", "type": "file"},
                ],
                "reason": "ambiguous_retrieval_scope",
                "confidence": "low",
                "expires_on": "new_upload_or_explicit_change",
            },
            "sources": [
                _local_file_source(file_id="file-a", name="alpha.txt"),
                _local_file_source(file_id="file-b", name="beta.txt"),
            ],
        }
    )

    assert persisted["active_source_scope"]["status"] == "ambiguous"
    assert persisted["active_source_scope"]["focus_state"] == "ambiguous"
    assert persisted["active_source_scope"]["ambiguity_reason"] == (
        "ambiguous_retrieval_scope"
    )
    assert persisted["active_source_scope"]["follow_up"]["reuse"] is False
    assert "canonical_references" not in persisted


def test_no_evidence_diagnostics_preserve_focus_without_source_cards():
    source = _local_file_source()
    metadata = {
        "active_source_scope": _resolved_active_source_scope(),
        "canonical_references": [source],
        "sources": [source],
        "retrieval_diagnostics": [
            {
                "kind": "retrieval_quality",
                "classification": "no_evidence",
                "reason": "no_retrieval_candidates",
            }
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(metadata=metadata)

    assert sidecar["active_source_scope"]["focus_state"] == "single"
    assert sidecar["retrieval_diagnostics"][0]["classification"] == "no_evidence"
    assert "canonical_references" not in sidecar


def test_message_hydration_preserves_focus_without_promoting_diagnostics():
    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "content": "I could not identify the intended file.",
            "metadata": {
                "active_source_scope": {
                    "status": "ambiguous",
                    "source_set_mode": "none",
                    "source_ids": ["file-a", "file-b"],
                    "sources": [
                        {"id": "file-a", "name": "alpha.txt", "type": "file"},
                        {"id": "file-b", "name": "beta.txt", "type": "file"},
                    ],
                    "reason": "ambiguous_retrieval_scope",
                    "confidence": "low",
                },
                "retrieval_diagnostics": [
                    {
                        "kind": "retrieval_quality",
                        "classification": "diagnostics",
                        "reason": "ambiguous_retrieval_scope",
                    }
                ],
            },
        }
    )

    metadata = normalized["metadata"]

    assert metadata["active_source_scope"]["focus_state"] == "ambiguous"
    assert metadata["retrieval_diagnostics"][0]["reason"] == (
        "ambiguous_retrieval_scope"
    )
    assert "canonical_references" not in metadata
    assert "sources" not in normalized


def test_raw_search_results_do_not_become_canonical_references_by_default():
    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_web_tool_output(
            tool_name="search_web",
            tool_args={"query": "current passport processing time"},
            payload={
                "query": "current passport processing time",
                "results": [
                    {
                        "title": "Result A",
                        "url": "https://example.com/a",
                        "snippet": "A snippet",
                    },
                    {
                        "title": "Result B",
                        "url": "https://example.com/b",
                        "snippet": "B snippet",
                    },
                ],
                "provider": "mock-search",
            },
        )
    )

    assert sidecar == {}


def test_visit_webpage_success_persists_one_web_reference_with_provenance():
    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_web_tool_output(
            tool_name="visit_webpage",
            tool_args={
                "url": "https://travel.state.gov/content/travel/en/passports/how-apply/processing-times.html"
            },
            payload={
                "url": "https://travel.state.gov/content/travel/en/passports/how-apply/processing-times.html",
                "source_class": "official_web",
                "authority": "official",
                "content": "Routine service can take 4 to 6 weeks.",
                "provider": "browser",
                "retrieval_round": 2,
                "as_of": "2026-05-15",
                "freshness": "updated_2026-01-28",
            },
        )
    )

    assert len(sidecar["canonical_references"]) == 1
    reference = sidecar["canonical_references"][0]
    assert reference["source"]["type"] == "official_web"
    assert reference["source"]["url"].startswith("https://travel.state.gov/")
    assert reference["metadata"][0]["tool_name"] == "visit_webpage"
    assert reference["metadata"][0]["provider"] == "browser"
    assert reference["metadata"][0]["retrieval_round"] == 2
    assert reference["metadata"][0]["as_of"] == "2026-05-15"
    assert reference["metadata"][0]["freshness"] == "updated_2026-01-28"


def test_visit_webpage_blocked_persists_diagnostics_only():
    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_web_tool_output(
            tool_name="visit_webpage",
            tool_args={"url": "https://example.com/restricted"},
            payload={
                "url": "https://example.com/restricted",
                "status": "blocked",
                "reason": "provider_blocked_url",
                "detail": "blocked by upstream provider",
            },
        )
    )

    assert "canonical_references" not in sidecar
    assert len(sidecar["retrieval_diagnostics"]) == 1
    diagnostic = sidecar["retrieval_diagnostics"][0]
    assert diagnostic["reason"] == "provider_blocked_url"
    assert diagnostic["classification"] == "diagnostics"


def test_selected_source_scope_blocks_ambiguous_multi_file_deictic_prompt():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt"},
        {"id": "beta", "name": "beta-policy.txt"},
    ]
    prompt_with_injected_file_tags = (
        '<attached_files>\n'
        '<file type="file" name="alpha-policy.txt" id="alpha"/>\n'
        '<file type="file" name="beta-policy.txt" id="beta"/>\n'
        '</attached_files>\n\n'
        "这个文件说了什么？"
    )

    prompts = [
        "这个文件说了什么？",
        "What does this file say?",
        "继续",
        "后面呢",
        "然后呢",
        "再往下看",
        "接着处理一下",
        prompt_with_injected_file_tags,
    ]

    for prompt in prompts:
        assert _selected_source_scope_is_ambiguous(prompt, files), prompt


def test_selected_source_scope_allows_single_anchor_and_explicit_multi_file_prompt():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt"},
        {"id": "beta", "name": "beta-policy.txt"},
    ]

    assert not _selected_source_scope_is_ambiguous("这个文件说了什么？", [files[0]])
    assert not _selected_source_scope_is_ambiguous("alpha-policy.txt 说了什么？", files)
    assert not _selected_source_scope_is_ambiguous("Alpha文件的政策答案是什么？", files)
    assert not _selected_source_scope_is_ambiguous("Beta文件的政策答案是什么？", files)
    assert not _selected_source_scope_is_ambiguous("总结这两个文件", files)
    assert not _selected_source_scope_is_ambiguous("比较这两个文件", files)
    assert not _selected_source_scope_is_ambiguous("总结这些文件的差异", files)
    assert not _selected_source_scope_is_ambiguous("summarize these two files", files)
    assert not _selected_source_scope_is_ambiguous(
        "summarize the differences in these files",
        files,
    )
    assert not _selected_source_scope_is_ambiguous(
        "Alpha和Beta两个文件的政策答案有什么区别？",
        files,
    )


def test_anchor_filter_keeps_only_matching_inline_source():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt"},
        {"id": "beta", "name": "beta-policy.txt"},
    ]
    sources = [
        {
            "source": {"id": "alpha", "name": "alpha-policy.txt"},
            "document": ["alpha body"],
            "metadata": [{"source": "alpha"}],
        },
        {
            "source": {"id": "beta", "name": "beta-policy.txt"},
            "document": ["beta body"],
            "metadata": [{"source": "beta"}],
        },
    ]

    filtered = _filter_inline_sources_for_selected_files(sources, [files[0]])

    assert filtered == [sources[0]]


def test_active_scope_reuses_previous_single_reference_for_deictic_prompt():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "canonical_references": [
                    {
                        "source": {"id": "alpha", "name": "alpha-policy.txt"},
                        "document": ["alpha body"],
                        "metadata": [{"source": "alpha", "name": "alpha-policy.txt"}],
                    }
                ]
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "这个文件继续总结",
        files,
        stored_messages=stored_messages,
        current_files=files,
    )

    assert not blocked
    assert resolved_files == [files[0]]
    assert scope["status"] == "resolved"
    assert scope["source_set_mode"] == "single"
    assert scope["source_ids"] == ["alpha"]
    assert scope["reason"] == "previous_single_canonical_reference"


def test_active_scope_reuses_previous_single_reference_for_bare_continuation_prompt():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "canonical_references": [
                    {
                        "source": {"id": "alpha", "name": "alpha-policy.txt"},
                        "document": ["alpha body"],
                        "metadata": [{"source": "alpha", "name": "alpha-policy.txt"}],
                    }
                ]
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "继续",
        files,
        stored_messages=stored_messages,
        current_files=files,
    )

    assert not blocked
    assert resolved_files == [files[0]]
    assert scope["status"] == "resolved"
    assert scope["source_set_mode"] == "single"
    assert scope["source_ids"] == ["alpha"]
    assert scope["reason"] == "previous_single_canonical_reference"


def test_active_scope_reuses_previous_single_reference_for_natural_followup_prompts():
    files = [
        {"id": "alpha", "name": "langfuse-e2e-alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "langfuse-e2e-beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "active_source_scope": {
                    "status": "resolved",
                    "source_set_mode": "single",
                    "source_ids": ["alpha"],
                    "sources": [
                        {
                            "id": "alpha",
                            "name": "langfuse-e2e-alpha-policy.txt",
                            "type": "file",
                        }
                    ],
                    "reason": "previous_single_canonical_reference",
                    "confidence": "high",
                }
            },
        }
    ]

    prompts = ["后面呢", "然后呢", "再往下看", "接着处理一下", "继续"]

    for prompt in prompts:
        scope, resolved_files, blocked = _resolve_active_source_scope(
            prompt,
            files,
            stored_messages=stored_messages,
            current_files=files,
        )

        assert not blocked, prompt
        assert resolved_files == [files[0]], prompt
        assert scope["status"] == "resolved", prompt
        assert scope["source_set_mode"] == "single", prompt
        assert scope["source_ids"] == ["alpha"], prompt


def test_active_scope_reuses_current_preview_focus_for_deictic_prompt():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "这个文件说了什么？",
        files,
        stored_messages=[],
        current_files=files,
        metadata={"current_preview_source": {"id": "beta", "name": "beta-policy.txt"}},
    )

    assert not blocked
    assert resolved_files == [files[1]]
    assert scope["status"] == "resolved"
    assert scope["source_set_mode"] == "single"
    assert scope["source_ids"] == ["beta"]
    assert scope["reason"] == "current_preview_source"


def test_active_scope_reuses_pinned_source_scope_when_still_selected():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "继续",
        files,
        stored_messages=[],
        current_files=files,
        metadata={
            "pinned_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["alpha"],
                "sources": [{"id": "alpha", "name": "alpha-policy.txt"}],
                "reason": "pinned_source_scope",
                "confidence": "high",
            }
        },
    )

    assert not blocked
    assert resolved_files == [files[0]]
    assert scope["source_ids"] == ["alpha"]
    assert scope["reason"] == "pinned_source_scope"


def test_active_scope_reuses_recent_source_card_interaction():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "后面呢",
        files,
        stored_messages=[],
        current_files=files,
        metadata={
            "recent_source_card_interaction": {
                "source": {"id": "beta", "name": "beta-policy.txt", "type": "file"},
                "metadata": [{"source": "beta", "name": "beta-policy.txt"}],
            }
        },
    )

    assert not blocked
    assert resolved_files == [files[1]]
    assert scope["source_ids"] == ["beta"]
    assert scope["reason"] == "recent_source_card_interaction"


def test_active_scope_reuses_previous_explicit_user_source_mention():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {"role": "user", "content": "请先总结 beta-policy.txt"},
        {"role": "assistant", "content": "Beta summary."},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "继续",
        files,
        stored_messages=stored_messages,
        current_files=files,
    )

    assert not blocked
    assert resolved_files == [files[1]]
    assert scope["source_ids"] == ["beta"]
    assert scope["reason"] == "previous_explicit_user_source_mention"


def test_active_scope_explicit_anchor_wins_over_previous_focus():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "canonical_references": [
                    {
                        "source": {"id": "alpha", "name": "alpha-policy.txt"},
                        "document": ["alpha body"],
                        "metadata": [{"source": "alpha", "name": "alpha-policy.txt"}],
                    }
                ]
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "请继续看 beta-policy.txt",
        files,
        stored_messages=stored_messages,
        current_files=files,
    )

    assert not blocked
    assert resolved_files == [files[1]]
    assert scope["source_ids"] == ["beta"]
    assert scope["reason"] == "explicit_anchor"


def test_active_scope_explicit_anchor_wins_over_current_preview_focus():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "请继续看 beta-policy.txt",
        files,
        stored_messages=[],
        current_files=files,
        metadata={"current_preview_source": {"id": "alpha", "name": "alpha-policy.txt"}},
    )

    assert not blocked
    assert resolved_files == [files[1]]
    assert scope["source_ids"] == ["beta"]
    assert scope["reason"] == "explicit_anchor"


def test_active_scope_does_not_reuse_stale_previous_focus():
    selected_files = [
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
        {"id": "gamma", "name": "gamma-policy.txt", "context": "full"},
    ]
    active_candidates = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        *selected_files,
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "canonical_references": [
                    {
                        "source": {"id": "alpha", "name": "alpha-policy.txt"},
                        "document": ["alpha body"],
                        "metadata": [{"source": "alpha", "name": "alpha-policy.txt"}],
                    }
                ]
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "这个文件说了什么？",
        active_candidates,
        stored_messages=stored_messages,
        current_files=selected_files,
    )

    assert blocked
    assert resolved_files == []
    assert scope["status"] == "expired"
    assert scope["reason"] == "expired_or_conflicting"


def test_active_scope_does_not_reuse_unselected_pinned_focus():
    selected_files = [
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
        {"id": "gamma", "name": "gamma-policy.txt", "context": "full"},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "这个文件说了什么？",
        selected_files,
        stored_messages=[],
        current_files=selected_files,
        metadata={
            "pinned_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["alpha"],
                "sources": [{"id": "alpha", "name": "alpha-policy.txt"}],
                "reason": "pinned_source_scope",
            }
        },
    )

    assert blocked
    assert resolved_files == []
    assert scope["status"] == "expired"
    assert scope["reason"] == "expired_or_conflicting"


def test_active_scope_persists_user_clarification_after_ambiguity():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "retrieval_diagnostics": [
                    {
                        "kind": "retrieval_quality",
                        "classification": "diagnostics",
                        "reason": "ambiguous_retrieval_scope",
                        "candidate_index": -1,
                    }
                ]
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "alpha-policy.txt",
        files,
        stored_messages=stored_messages,
        current_files=files,
    )

    assert not blocked
    assert resolved_files == [files[0]]
    assert scope["status"] == "resolved"
    assert scope["reason"] == "user_clarified_deictic_reference"
    assert scope["source_ids"] == ["alpha"]


def test_active_scope_reuses_persisted_user_clarification_after_ambiguity():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "active_source_scope": {
                    "status": "resolved",
                    "source_set_mode": "single",
                    "source_ids": ["alpha"],
                    "sources": [
                        {"id": "alpha", "name": "alpha-policy.txt", "type": "file"}
                    ],
                    "reason": "user_clarified_deictic_reference",
                    "confidence": "high",
                }
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "继续",
        files,
        stored_messages=stored_messages,
        current_files=files,
    )

    assert not blocked
    assert resolved_files == [files[0]]
    assert scope["status"] == "resolved"
    assert scope["source_ids"] == ["alpha"]
    assert scope["reason"] == "user_clarified_deictic_reference"


def test_active_scope_explicit_anchor_overrides_prior_user_clarification():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "active_source_scope": {
                    "status": "resolved",
                    "source_set_mode": "single",
                    "source_ids": ["alpha"],
                    "sources": [
                        {"id": "alpha", "name": "alpha-policy.txt", "type": "file"}
                    ],
                    "reason": "user_clarified_deictic_reference",
                    "confidence": "high",
                }
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "请改看 beta-policy.txt",
        files,
        stored_messages=stored_messages,
        current_files=files,
    )

    assert not blocked
    assert resolved_files == [files[1]]
    assert scope["status"] == "resolved"
    assert scope["source_ids"] == ["beta"]
    assert scope["reason"] == "explicit_anchor"


def test_active_scope_rejects_ambiguous_user_clarification_after_ambiguity():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "active_source_scope": {
                    "status": "ambiguous",
                    "source_set_mode": "none",
                    "reason": "ambiguous_retrieval_scope",
                    "confidence": "low",
                }
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "alpha beta",
        files,
        stored_messages=stored_messages,
        current_files=files,
    )

    assert blocked
    assert resolved_files == []
    assert scope["status"] == "ambiguous"
    assert scope["reason"] == "ambiguous_retrieval_scope"
    sidecar = Chats.build_reference_metadata_sidecar(
        metadata={"active_source_scope": scope},
        sources=[
            {
                "source": {"id": "alpha", "name": "alpha-policy.txt", "type": "file"},
                "document": ["alpha evidence"],
                "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha"}],
            }
        ],
    )
    assert "canonical_references" not in sidecar


def test_active_scope_rejects_unselected_user_clarification_after_ambiguity():
    selected_files = [
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
        {"id": "gamma", "name": "gamma-policy.txt", "context": "full"},
    ]
    all_files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        *selected_files,
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "active_source_scope": {
                    "status": "ambiguous",
                    "source_set_mode": "none",
                    "reason": "ambiguous_retrieval_scope",
                    "confidence": "low",
                }
            },
        }
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "alpha-policy.txt",
        selected_files,
        stored_messages=stored_messages,
        current_files=selected_files,
        fallback_candidates=all_files,
    )

    assert blocked
    assert resolved_files == []
    assert scope["status"] == "expired"
    assert scope["reason"] == "expired_or_conflicting"
    sidecar = Chats.build_reference_metadata_sidecar(
        metadata={"active_source_scope": scope},
        sources=[
            {
                "source": {"id": "alpha", "name": "alpha-policy.txt", "type": "file"},
                "document": ["alpha evidence"],
                "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha"}],
            }
        ],
    )
    assert "canonical_references" not in sidecar


def test_active_scope_keeps_unfocused_multi_file_deictic_ambiguous():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "这个文件说了什么？",
        files,
        stored_messages=[],
        current_files=files,
    )

    assert blocked
    assert resolved_files == []
    assert scope["status"] == "ambiguous"
    assert scope["reason"] == "ambiguous_retrieval_scope"


def test_active_scope_treats_bare_continuation_as_ambiguous_without_focus():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "继续",
        files,
        stored_messages=[],
        current_files=files,
    )

    assert blocked
    assert resolved_files == []
    assert scope["status"] == "ambiguous"
    assert scope["source_set_mode"] == "none"
    assert scope["reason"] == "ambiguous_retrieval_scope"


def test_active_scope_treats_natural_followup_prompts_as_ambiguous_without_focus():
    files = [
        {"id": "alpha", "name": "langfuse-e2e-alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "langfuse-e2e-beta-policy.txt", "context": "full"},
    ]

    for prompt in ["后面呢", "然后呢", "再往下看", "接着处理一下", "继续"]:
        scope, resolved_files, blocked = _resolve_active_source_scope(
            prompt,
            files,
            stored_messages=[],
            current_files=files,
        )

        assert blocked, prompt
        assert resolved_files == [], prompt
        assert scope["status"] == "ambiguous", prompt
        assert scope["source_set_mode"] == "none", prompt
        assert scope["reason"] == "ambiguous_retrieval_scope", prompt


def test_active_scope_explicit_alpha_beta_anchors_narrow_to_matching_file():
    files = [
        {
            "id": "alpha-file",
            "name": "langfuse-e2e-alpha-20260513-observability.txt",
            "context": "full",
        },
        {
            "id": "beta-file",
            "name": "langfuse-e2e-beta-20260513-observability.txt",
            "context": "full",
        },
    ]

    alpha_scope, alpha_files, alpha_blocked = _resolve_active_source_scope(
        "Alpha文件的政策答案是什么？",
        files,
        stored_messages=[],
        current_files=files,
    )
    beta_scope, beta_files, beta_blocked = _resolve_active_source_scope(
        "Beta文件的政策答案是什么？",
        files,
        stored_messages=[],
        current_files=files,
    )

    assert not alpha_blocked
    assert alpha_files == [files[0]]
    assert alpha_scope["source_set_mode"] == "single"
    assert alpha_scope["source_ids"] == ["alpha-file"]

    assert not beta_blocked
    assert beta_files == [files[1]]
    assert beta_scope["source_set_mode"] == "single"
    assert beta_scope["source_ids"] == ["beta-file"]


def test_active_scope_allows_explicit_multi_file_prompt():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "compare alpha-policy.txt and beta-policy.txt",
        files,
        stored_messages=[],
        current_files=files,
    )

    assert not blocked
    assert resolved_files == files
    assert scope["status"] == "resolved"
    assert scope["source_set_mode"] == "multi"
    assert scope["source_ids"] == ["alpha", "beta"]


def test_active_scope_explicit_plural_multi_prompt_overrides_previous_focus():
    files = [
        {"id": "alpha", "name": "alpha-policy.txt", "context": "full"},
        {"id": "beta", "name": "beta-policy.txt", "context": "full"},
    ]
    stored_messages = [
        {
            "role": "assistant",
            "metadata": {
                "active_source_scope": {
                    "status": "resolved",
                    "source_set_mode": "single",
                    "source_ids": ["alpha"],
                    "sources": [
                        {"id": "alpha", "name": "alpha-policy.txt", "type": "file"}
                    ],
                    "reason": "previous_single_canonical_reference",
                    "confidence": "high",
                }
            },
        }
    ]

    for prompt in ("比较这两个文件", "总结这些文件的差异"):
        scope, resolved_files, blocked = _resolve_active_source_scope(
            prompt,
            files,
            stored_messages=stored_messages,
            current_files=files,
        )

        assert not blocked, prompt
        assert resolved_files == files, prompt
        assert scope["status"] == "resolved", prompt
        assert scope["source_set_mode"] == "multi", prompt
        assert scope["source_ids"] == ["alpha", "beta"], prompt
        assert scope["reason"] == "explicit_anchor", prompt


def test_active_scope_uses_anchor_matches_for_explicit_alpha_beta_multi_prompt():
    files = [
        {
            "id": "alpha-file",
            "name": "langfuse-e2e-alpha-20260513-observability.txt",
            "context": "full",
        },
        {
            "id": "beta-file",
            "name": "langfuse-e2e-beta-20260513-observability.txt",
            "context": "full",
        },
    ]

    scope, resolved_files, blocked = _resolve_active_source_scope(
        "Alpha和Beta两个文件的政策答案有什么区别？",
        files,
        stored_messages=[],
        current_files=files,
    )

    assert not blocked
    assert resolved_files == files
    assert scope["status"] == "resolved"
    assert scope["source_set_mode"] == "multi"
    assert scope["source_ids"] == ["alpha-file", "beta-file"]


def test_active_scope_metadata_does_not_become_canonical_reference():
    sidecar = Chats.build_reference_metadata_sidecar(
        metadata={
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["alpha"],
                "sources": [
                    {"id": "alpha", "name": "alpha-policy.txt", "type": "file"}
                ],
                "reason": "previous_single_canonical_reference",
                "confidence": "high",
                "expires_on": "new_upload_or_explicit_change",
            }
        }
    )

    assert "canonical_references" not in sidecar
    assert "retrieval_diagnostics" not in sidecar


def test_empty_collection_with_null_meta_is_not_treated_as_media():
    empty_collection = {
        "type": "collection",
        "id": "empty-kb",
        "name": "Empty KB",
        "meta": None,
    }

    assert not _is_media_file_item(empty_collection)
    assert not _is_image_file_item(empty_collection)


def test_no_evidence_gate_keeps_diagnostics_out_of_canonical_references():
    candidates = [
        {
            "source": {"id": "empty-kb", "name": "Empty KB", "type": "collection"},
            "document": [],
            "metadata": [],
        }
    ]

    sources, diagnostics = _gate_retrieval_sources(candidates)
    sidecar = Chats.build_reference_metadata_sidecar(
        sources=sources,
        diagnostics=diagnostics,
    )

    assert sources == []
    assert "canonical_references" not in sidecar
    assert "retrieval_diagnostics" in sidecar
    assert {item["reason"] for item in sidecar["retrieval_diagnostics"]} == {
        "missing_document_body",
        "no_injectable_evidence",
    }


def test_retrieval_quality_gate_preserves_selected_file_success():
    candidates = [
        {
            "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
            "document": ["Alpha selected-file evidence."],
            "metadata": [
                {
                    "file_id": "alpha-file",
                    "name": "alpha-policy.txt",
                    "source": "alpha-policy.txt",
                }
            ],
        }
    ]

    sources, diagnostics = _gate_retrieval_sources(
        candidates,
        active_source_scope={
            "status": "resolved",
            "source_set_mode": "single",
            "source_ids": ["alpha-file"],
            "sources": [{"id": "alpha-file", "name": "alpha-policy.txt"}],
        },
    )
    sidecar = Chats.build_reference_metadata_sidecar(
        sources=sources,
        diagnostics=diagnostics,
    )

    assert diagnostics == []
    assert len(sources) == 1
    assert sources[0]["retrieval_outcome"] == "success"
    assert sources[0]["retrieval_classification"] == "injectable"
    assert sidecar["canonical_references"][0]["source"]["id"] == "alpha-file"


def test_retrieval_quality_gate_rejects_scope_mismatch_and_weak_candidates():
    candidates = [
        {
            "source": {"id": "beta-file", "name": "beta-policy.txt", "type": "file"},
            "document": ["Beta evidence should not satisfy alpha focus."],
            "metadata": [{"file_id": "beta-file", "name": "beta-policy.txt"}],
        },
        {
            "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
            "document": ["Low quality alpha evidence."],
            "metadata": [
                {
                    "file_id": "alpha-file",
                    "name": "alpha-policy.txt",
                    "answerable": False,
                }
            ],
        },
    ]

    sources, diagnostics = _gate_retrieval_sources(
        candidates,
        active_source_scope={
            "status": "resolved",
            "source_set_mode": "single",
            "source_ids": ["alpha-file"],
            "sources": [{"id": "alpha-file", "name": "alpha-policy.txt"}],
        },
    )
    sidecar = Chats.build_reference_metadata_sidecar(
        sources=sources,
        diagnostics=diagnostics,
    )

    assert sources == []
    assert "canonical_references" not in sidecar
    assert {item["reason"] for item in sidecar["retrieval_diagnostics"]} >= {
        "source_scope_mismatch",
        "weak_or_indirect_evidence",
        "no_injectable_evidence",
    }
    assert {item["outcome"] for item in sidecar["retrieval_diagnostics"]} >= {
        "unauthorized",
        "weak_evidence",
        "empty",
    }


def test_retrieval_quality_gate_keeps_stale_unauthorized_conflict_as_diagnostics():
    candidates = [
        {
            "source": {"id": "stale-file", "name": "stale.txt", "type": "file"},
            "document": ["Stale evidence."],
            "metadata": [{"file_id": "stale-file", "stale": True}],
        },
        {
            "source": {"id": "denied-file", "name": "denied.txt", "type": "file"},
            "document": ["Denied evidence."],
            "metadata": [{"file_id": "denied-file", "authorized": False}],
        },
        {
            "source": {"id": "conflict-file", "name": "conflict.txt", "type": "file"},
            "document": ["Conflicting evidence."],
            "metadata": [{"file_id": "conflict-file", "conflict": True}],
        },
    ]

    sources, diagnostics = _gate_retrieval_sources(candidates)
    sidecar = Chats.build_reference_metadata_sidecar(
        sources=sources,
        diagnostics=diagnostics,
    )

    assert sources == []
    assert "canonical_references" not in sidecar
    assert {item["outcome"] for item in sidecar["retrieval_diagnostics"]} >= {
        "stale",
        "unauthorized",
        "conflict",
    }


def test_knowflow_neutral_contract_comparison_preserves_scope_and_quality_gate():
    comparison = _compare_knowflow_retrieval_against_neutral_contract(
        {
            "provider": "knowflow",
            "strategy_used": ["hybrid", "rerank"],
            "candidates": [
                {
                    "source": {
                        "id": "alpha-file",
                        "name": "alpha-policy.txt",
                        "type": "file",
                    },
                    "document": ["Alpha selected-file evidence."],
                    "metadata": [
                        {
                            "file_id": "alpha-file",
                            "name": "alpha-policy.txt",
                            "strategy_used": ["metadata_first", "hybrid"],
                            "vector_score": 0.82,
                            "bm25_score": 12.4,
                            "rerank_score": 0.91,
                            "index_state": "fresh",
                            "indexed_at": "2026-05-18T09:00:00Z",
                            "authorized": True,
                        }
                    ],
                }
            ],
        },
        query="alpha policy",
        active_source_scope={
            "status": "resolved",
            "source_set_mode": "single",
            "source_ids": ["alpha-file"],
            "sources": [{"id": "alpha-file", "name": "alpha-policy.txt"}],
        },
        evidence_need="internal_selected_source",
        exact_anchors=["alpha-policy.txt"],
        filters={"file_id": "alpha-file"},
        strategy_preferences=["literal_anchor", "metadata_first", "hybrid", "rerank"],
        count=5,
        retrieval_round=1,
    )

    assert comparison["reference_mechanics"] == "weknora_style"
    assert comparison["reference_dependency"] == "none"
    assert comparison["runtime_authority"] == "diagnostic_only"
    assert comparison["neutral_request"]["source_scope"]["source_ids"] == [
        "alpha-file"
    ]
    assert comparison["neutral_request"]["evidence_need"] == "internal_selected_source"
    assert comparison["neutral_response"]["status"] == "success"
    assert comparison["neutral_response"]["provider"] == "knowflow"
    assert comparison["neutral_response"]["accepted_count"] == 1
    assert comparison["neutral_response"]["canonical_references"][0]["source"]["id"] == (
        "alpha-file"
    )
    assert comparison["neutral_response"]["diagnostics"] == []
    assert {"metadata_first", "hybrid"}.issubset(
        set(comparison["neutral_response"]["capabilities_observed"])
    )
    assert comparison["neutral_response"]["provider_local_scores"][0]["semantics"] == (
        "provider_local_advisory"
    )
    assert comparison["neutral_response"]["freshness_index_state"][0]["state"][
        "index_state"
    ] == "fresh"
    assert comparison["neutral_response"]["authorization_outcomes"][0]["outcome"] == (
        "authorized"
    )
    assert [stage["stage"] for stage in comparison["retrieval_stage_trace"]] == [
        "neutral_request",
        "provider_recall",
        "quality_gate",
        "canonicalization",
    ]


def test_knowflow_neutral_contract_comparison_keeps_provider_prose_out_of_citations():
    comparison = _compare_knowflow_retrieval_against_neutral_contract(
        {
            "provider": "knowflow",
            "answer": "Provider-specific answer prose that must not become a citation.",
            "candidates": [],
        },
        query="what does selected source say",
        active_source_scope=_resolved_active_source_scope(),
        evidence_need="internal_selected_source",
    )

    assert comparison["neutral_response"]["status"] == "diagnostic"
    assert comparison["neutral_response"]["canonical_references"] == []
    assert {
        item["reason"] for item in comparison["neutral_response"]["diagnostics"]
    } == {"provider_answer_prose_ignored"}
    assert "provider_answer_prose_not_citation" in comparison["notes"]


def test_knowflow_neutral_contract_comparison_keeps_rejections_diagnostic_only():
    comparison = _compare_knowflow_retrieval_against_neutral_contract(
        {
            "provider": "knowflow",
            "capabilities": ["dense_semantic"],
            "candidates": [
                {
                    "source": {"id": "beta-file", "name": "beta.txt", "type": "file"},
                    "document": ["Beta content outside alpha scope."],
                    "metadata": [{"file_id": "beta-file", "distance": 0.1}],
                },
                {
                    "source": {
                        "id": "alpha-file",
                        "name": "alpha.txt",
                        "type": "file",
                    },
                    "document": ["Weak alpha content."],
                    "metadata": [{"file_id": "alpha-file", "answerable": False}],
                },
            ],
        },
        query="alpha policy",
        active_source_scope={
            "status": "resolved",
            "source_set_mode": "single",
            "source_ids": ["alpha-file"],
            "sources": [{"id": "alpha-file", "name": "alpha.txt"}],
        },
        evidence_need="internal_selected_source",
    )

    assert comparison["neutral_response"]["canonical_references"] == []
    reasons = {item["reason"] for item in comparison["neutral_response"]["diagnostics"]}
    assert reasons >= {
        "source_scope_mismatch",
        "weak_or_indirect_evidence",
        "no_injectable_evidence",
    }
    assert comparison["quality_gate"]["accepted_count"] == 0
    assert "dense_semantic" in comparison["neutral_response"]["capabilities_observed"]


def test_completion_wrapper_filter_does_not_persist_ambiguous_sources():
    wrapper = {
        "done": True,
        "content": "",
        "output": [{"type": "message", "content": []}],
        "metadata": {
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_quality",
                    "classification": "diagnostics",
                    "reason": "ambiguous_retrieval_scope",
                    "candidate_index": -1,
                }
            ]
        },
    }

    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "metadata": {
                "retrieval_diagnostics": [
                    {
                        "kind": "retrieval_quality",
                        "classification": "diagnostics",
                        "reason": "ambiguous_retrieval_scope",
                        "candidate_index": -1,
                    }
                ]
            },
            "sources": [wrapper],
        }
    )

    assert "sources" not in normalized
    metadata = normalized.get("metadata") or {}
    assert "canonical_references" not in metadata
    assert metadata.get("retrieval_diagnostics", [])[0]["reason"] == (
        "ambiguous_retrieval_scope"
    )


def test_ambiguous_scope_strips_stale_sources_and_canonical_references():
    stale_source = {
        "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
        "document": ["stale evidence"],
        "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha-file"}],
    }

    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "done": True,
            "sources": [stale_source],
            "metadata": {
                "canonical_references": [stale_source],
                "active_source_scope": {
                    "status": "ambiguous",
                    "source_set_mode": "none",
                    "source_ids": ["alpha-file", "beta-file"],
                    "sources": [
                        {
                            "id": "alpha-file",
                            "name": "alpha-policy.txt",
                            "type": "file",
                        },
                        {
                            "id": "beta-file",
                            "name": "beta-policy.txt",
                            "type": "file",
                        },
                    ],
                    "reason": "ambiguous_retrieval_scope",
                    "confidence": "low",
                },
                "retrieval_diagnostics": [
                    {
                        "kind": "retrieval_quality",
                        "classification": "diagnostics",
                        "reason": "ambiguous_retrieval_scope",
                        "candidate_index": -1,
                    }
                ],
            },
        }
    )

    assert "sources" not in normalized
    metadata = normalized.get("metadata") or {}
    assert "canonical_references" not in metadata
    assert metadata["active_source_scope"]["status"] == "ambiguous"
    assert metadata["retrieval_diagnostics"][0]["reason"] == (
        "ambiguous_retrieval_scope"
    )


def test_merge_reference_sidecar_drops_stale_canonical_refs_for_ambiguous_scope():
    stale_source = {
        "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
        "document": ["stale evidence"],
        "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha-file"}],
    }
    metadata = {
        "canonical_references": [stale_source],
        "active_source_scope": {
            "status": "ambiguous",
            "source_set_mode": "none",
            "reason": "ambiguous_retrieval_scope",
            "confidence": "low",
        },
        "retrieval_diagnostics": [
            {
                "kind": "retrieval_quality",
                "classification": "diagnostics",
                "reason": "ambiguous_retrieval_scope",
                "candidate_index": -1,
            }
        ],
    }

    sidecar = _merge_reference_sidecar_into_metadata(
        metadata,
        sources=[stale_source],
        diagnostics=metadata["retrieval_diagnostics"],
    )

    assert "canonical_references" not in sidecar
    assert "canonical_references" not in metadata
    assert metadata["retrieval_diagnostics"][0]["reason"] == (
        "ambiguous_retrieval_scope"
    )


def test_no_evidence_diagnostics_clear_stale_sources_and_canonical_references():
    stale_source = {
        "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
        "document": ["stale evidence"],
        "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha-file"}],
    }

    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "done": True,
            "sources": [stale_source],
            "metadata": {
                "retrieval_diagnostics": [
                    {
                        "kind": "retrieval_quality",
                        "classification": "no_evidence",
                        "reason": "no_retrieval_candidates",
                        "candidate_index": -1,
                    }
                ],
            },
        }
    )

    assert "sources" not in normalized
    metadata = normalized.get("metadata") or {}
    assert "canonical_references" not in metadata
    assert metadata["retrieval_diagnostics"][0]["classification"] == "no_evidence"


def test_ambiguous_scope_strips_suppressed_inline_citation_markers():
    stale_source = {
        "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
        "document": ["stale evidence"],
        "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha-file"}],
    }

    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "done": True,
            "content": (
                "Because ambiguous deictic references must not choose a file [2], "
                "please specify which file you mean."
            ),
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "Because ambiguous deictic references must not choose "
                                "a file [2], please specify which file you mean."
                            ),
                        }
                    ],
                }
            ],
            "sources": [stale_source],
            "metadata": {
                "active_source_scope": {
                    "status": "ambiguous",
                    "source_set_mode": "none",
                    "reason": "ambiguous_retrieval_scope",
                    "confidence": "low",
                },
                "retrieval_diagnostics": [
                    {
                        "kind": "retrieval_quality",
                        "classification": "diagnostics",
                        "reason": "ambiguous_retrieval_scope",
                        "candidate_index": -1,
                    }
                ],
            },
        }
    )

    assert normalized["content"] == (
        "Because ambiguous deictic references must not choose a file, "
        "please specify which file you mean."
    )
    assert normalized["output"][0]["content"][0]["text"] == (
        "Because ambiguous deictic references must not choose a file, "
        "please specify which file you mean."
    )
    assert "sources" not in normalized
    assert "canonical_references" not in (normalized.get("metadata") or {})


def test_assistant_reference_seed_metadata_keeps_ambiguous_scope_without_refs():
    seed_metadata = _build_assistant_reference_seed_metadata(
        {
            "active_source_scope": {
                "status": "ambiguous",
                "source_set_mode": "none",
                "reason": "ambiguous_retrieval_scope",
                "confidence": "low",
            },
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_quality",
                    "classification": "diagnostics",
                    "reason": "ambiguous_retrieval_scope",
                    "candidate_index": -1,
                }
            ],
            "sources": [
                {
                    "source": {
                        "id": "alpha-file",
                        "name": "alpha-policy.txt",
                        "type": "file",
                    },
                    "document": ["stale evidence"],
                }
            ],
        }
    )

    assert seed_metadata["active_source_scope"]["status"] == "ambiguous"
    assert "canonical_references" not in seed_metadata
    assert seed_metadata["retrieval_diagnostics"][0]["reason"] == (
        "ambiguous_retrieval_scope"
    )


def test_merge_message_payload_clears_historical_stale_sources_from_seed_metadata():
    stale_source = {
        "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
        "document": ["stale evidence"],
        "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha-file"}],
    }
    existing_message = {
        "role": "assistant",
        "sources": [stale_source],
        "metadata": {"canonical_references": [stale_source]},
    }
    incoming_message = {
        "role": "assistant",
        "metadata": _build_assistant_reference_seed_metadata(
            {
                "active_source_scope": {
                    "status": "ambiguous",
                    "source_set_mode": "none",
                    "reason": "ambiguous_retrieval_scope",
                    "confidence": "low",
                },
                "retrieval_diagnostics": [
                    {
                        "kind": "retrieval_quality",
                        "classification": "diagnostics",
                        "reason": "ambiguous_retrieval_scope",
                        "candidate_index": -1,
                    }
                ],
            }
        ),
    }

    merged = Chats._merge_message_payload(existing_message, incoming_message)

    assert "sources" not in merged
    assert "canonical_references" not in (merged.get("metadata") or {})
    assert merged["metadata"]["active_source_scope"]["status"] == "ambiguous"


def test_assistant_reference_persistence_metadata_keeps_selected_file_reference():
    persisted_metadata = _build_assistant_reference_persistence_metadata(
        {
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["alpha-file"],
                "sources": [
                    {
                        "id": "alpha-file",
                        "name": "alpha-policy.txt",
                        "type": "file",
                    }
                ],
                "reason": "current_turn_upload",
                "confidence": "high",
            },
            "sources": [
                {
                    "source": {
                        "id": "alpha-file",
                        "name": "alpha-policy.txt",
                        "type": "file",
                    },
                    "document": ["alpha evidence"],
                    "metadata": [
                        {
                            "source": "alpha-policy.txt",
                            "file_id": "alpha-file",
                        }
                    ],
                }
            ],
        }
    )

    assert persisted_metadata["active_source_scope"]["status"] == "resolved"
    assert len(persisted_metadata["canonical_references"]) == 1
    assert persisted_metadata["canonical_references"][0]["source"]["id"] == (
        "alpha-file"
    )


def test_assistant_reference_persistence_metadata_keeps_explicit_multi_scope():
    persisted_metadata = _build_assistant_reference_persistence_metadata(
        {
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "multi",
                "source_ids": ["alpha-file", "beta-file"],
                "sources": [
                    {
                        "id": "alpha-file",
                        "name": "langfuse-e2e-alpha-policy.txt",
                        "type": "file",
                    },
                    {
                        "id": "beta-file",
                        "name": "langfuse-e2e-beta-policy.txt",
                        "type": "file",
                    },
                ],
                "reason": "explicit_anchor",
                "confidence": "high",
            },
            "sources": [
                {
                    "source": {
                        "id": "alpha-file",
                        "name": "langfuse-e2e-alpha-policy.txt",
                        "type": "file",
                    },
                    "document": ["alpha evidence"],
                    "metadata": [
                        {
                            "source": "langfuse-e2e-alpha-policy.txt",
                            "file_id": "alpha-file",
                        }
                    ],
                },
                {
                    "source": {
                        "id": "beta-file",
                        "name": "langfuse-e2e-beta-policy.txt",
                        "type": "file",
                    },
                    "document": ["beta evidence"],
                    "metadata": [
                        {
                            "source": "langfuse-e2e-beta-policy.txt",
                            "file_id": "beta-file",
                        }
                    ],
                },
            ],
        }
    )

    assert persisted_metadata["active_source_scope"]["source_set_mode"] == "multi"
    assert [item["source"]["id"] for item in persisted_metadata["canonical_references"]] == [
        "alpha-file",
        "beta-file",
    ]


def test_fresh_multi_selected_explicit_multi_prompt_keeps_active_source_scope(
    monkeypatch,
):
    files = [
        {"id": "alpha-file", "name": "Alpha文件-verification.txt", "context": "full"},
        {"id": "beta-file", "name": "Beta文件-verification.txt", "context": "full"},
    ]
    inline_sources = [
        {
            "source": {
                "id": "alpha-file",
                "name": "Alpha文件-verification.txt",
                "type": "file",
            },
            "document": ["alpha evidence"],
            "metadata": [
                {
                    "source": "Alpha文件-verification.txt",
                    "file_id": "alpha-file",
                }
            ],
        },
        {
            "source": {
                "id": "beta-file",
                "name": "Beta文件-verification.txt",
                "type": "file",
            },
            "document": ["beta evidence"],
            "metadata": [
                {
                    "source": "Beta文件-verification.txt",
                    "file_id": "beta-file",
                }
            ],
        },
    ]

    async def prepare_files(*_args, **_kwargs):
        return files, inline_sources, [], [], []

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        prepare_files,
    )

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    TOP_K=4,
                    TOP_K_RERANKER=4,
                    RELEVANCE_THRESHOLD=0.0,
                    HYBRID_BM25_WEIGHT=0.0,
                    ENABLE_RAG_HYBRID_SEARCH=False,
                    RAG_FULL_CONTEXT=False,
                )
            )
        )
    )
    body = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "Alpha和Beta两个文件的政策答案有什么区别？"}
        ],
        "metadata": {"files": files},
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            request,
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert flags["active_source_scope"]["status"] == "resolved"
    assert flags["active_source_scope"]["source_set_mode"] == "multi"
    assert flags["active_source_scope"]["source_ids"] == ["alpha-file", "beta-file"]
    assert persisted_metadata["active_source_scope"]["source_set_mode"] == "multi"
    assert [item["source"]["id"] for item in persisted_metadata["canonical_references"]] == [
        "alpha-file",
        "beta-file",
    ]


def _first_pass_retrieval_request_stub():
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    TOP_K=4,
                    TOP_K_RERANKER=4,
                    RELEVANCE_THRESHOLD=0.0,
                    HYBRID_BM25_WEIGHT=0.0,
                    ENABLE_RAG_HYBRID_SEARCH=False,
                    RAG_FULL_CONTEXT=False,
                ),
                EMBEDDING_FUNCTION=lambda text, prefix=None, user=None: [0.1],
                RERANKING_FUNCTION=None,
            )
        )
    )


def _retrieval_engine_result_stub(
    *,
    status: str,
    terminal_reason: str,
    diagnostics: list[dict],
    source_id: str = "alpha-file",
):
    diagnostic_objects = [
        SimpleNamespace(
            code=str(item.get("code") or ""),
            kind=str(item.get("kind") or "diagnostics"),
            severity=str(item.get("severity") or "warning"),
            phase=str(item.get("phase") or "acceptance"),
            source_id=str(item.get("source_id") or source_id),
            candidate_id=str(item.get("candidate_id") or "candidate:alpha"),
            details=dict(item.get("details") or {}),
        )
        for item in diagnostics
    ]
    scope_decision = status if status in {"denied", "blocked"} else "authorized"
    return SimpleNamespace(
        status=status,
        terminal_reason=terminal_reason,
        accepted_outputs=(),
        references=(),
        diagnostics=diagnostic_objects,
        candidates=(),
        evidence_bundles=(),
        request=SimpleNamespace(
            scope=SimpleNamespace(
                decision=scope_decision,
                source_ids=(source_id,),
                reason=terminal_reason,
            ),
            requested_budget_tokens=128,
            execution_context={
                "engine_version": "open_webui_selected_source_engine_v1",
                "ranking_policy": "selected_file_text_cutover",
                "budget_policy": "selected_source_first_pass",
                "return_policy": "accepted_evidence_only",
                "authorization_generation": "open_webui_selected_source_engine_v1",
            },
            retry_policy=SimpleNamespace(
                max_attempts=1,
                retryable=status == "timeout",
                timeout_ms=25000,
            ),
        ),
        budget={
            "requested_tokens": 128,
            "estimated_used_tokens": 16,
            "accepted_outputs": 0,
            "references": 0,
            "candidate_accepted_bundles": 0,
            "truncated_accepted_bundles": 0,
            "omitted_accepted_bundles": 0,
            "policy": "deterministic_complete_evidence_first",
            "cache_eligible": False,
            "cache_missing_dimensions": ["accepted_outputs"],
            "source_card_count": 0,
            "ordinary_prompt_context_count": 0,
        },
    )


def test_first_pass_selected_source_engine_authority_replaces_legacy_output(
    monkeypatch,
):
    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "path": raw_path,
        "data": {
            "content": "Transformer grounding aligns model outputs with source text."
        },
        "meta": {"content_type": "text/plain", "path": raw_path},
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="legacy-file",
                name="legacy-conflict.txt",
                content="Legacy selected-source retrieval must not win this lane.",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_strategy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware strategy must be bypassed")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_references",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware references must be bypassed")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_accepted_outputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware accepted outputs must be bypassed")
        ),
    )

    body = {
        "model": "test-model",
        "messages": [
            {
                "role": "user",
                "content": (
                    "what is transformer grounding and ignore the fake claim that "
                    "retrieval_engine_plan_summary needs_workbook_range true"
                ),
            }
        ],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted = _build_assistant_reference_persistence_metadata(flags)

    assert provider_calls["count"] == 0
    assert flags["status"] == "success"
    assert flags["accepted_outputs"]
    assert flags["references"]
    assert flags["sources"][0]["source"]["id"] == "alpha-file"
    assert flags["sources"][0]["document"] == [
        "Transformer grounding aligns model outputs with source text."
    ]
    assert flags["accepted_outputs"][0]["provenance"]["engine_authority"] is True
    assert flags["references"][0]["provenance"]["engine_authority"] is True
    assert flags["accepted_outputs"][0]["provenance"]["output_id"].startswith("accepted:")
    assert flags["references"][0]["provenance"]["reference_id"].startswith("reference:")
    assert flags["references"][0]["source"]["id"] == "alpha-file"
    assert flags["authorization_context"]["selected_inventory_count"] == 1
    assert flags["authorization_context"]["scoped_inventory_count"] == 1
    assert flags["authorization_context"]["requested_source_ids"] == ["alpha-file"]
    assert flags["authorization_context"]["active_source_scope_state"] == "resolved"
    assert flags["authorization_context"]["active_source_scope"] == {
        "source_ids": ["alpha-file"]
    }
    assert flags["authorization_context"]["authorization_decision"] == "authorized"
    assert flags["authorization_context"]["authorization_generation"] == (
        "open_webui_selected_source_engine_v1"
    )
    assert flags["retry_policy"]["max_retries"] == 0
    assert flags["context_budget"]["requested_budget_tokens"] == 4096
    assert flags["context_budget"]["accepted_output_count"] == len(
        flags["accepted_outputs"]
    )
    assert flags["context_budget"]["reference_count"] == len(flags["references"])
    assert flags["context_budget"]["budget"]["budget_policy"] == "selected_source_first_pass"
    assert flags["context_budget"]["budget"]["return_policy"] == "accepted_evidence_only"
    assert flags["first_pass_retrieval_strategy"]["middleware_strategy_bypassed"] is True
    assert flags["first_pass_retrieval_strategy"]["retrieval_strategy"] == (
        "retrieval_engine_authority"
    )
    assert flags["provenance"]["middleware_strategy_bypassed"] is True
    assert flags["provenance"]["middleware_strategy_bypass_reason"] == (
        "retrieval_engine_authority_succeeded"
    )
    assert flags["provenance"]["selected_source_runtime_mode"] == (
        "retrieval_engine_authority"
    )
    assert flags["provenance"].get("compatibility_fallback") is not True
    assert (
        flags["provenance"]["compact_retrieval_provenance"]["budget"]["budget_policy"]
        == "selected_source_first_pass"
    )
    assert persisted["canonical_references"][0]["metadata"][0]["engine_authority"] is True
    assert (
        persisted["canonical_references"][0]["metadata"][0][
            "retrieval_engine_reference_id"
        ].startswith("reference:")
    )
    assert (
        persisted["retrieval_provenance"]["budget"]["authorization_generation"]
        == "open_webui_selected_source_engine_v1"
    )
    assert repr(flags).find("legacy-conflict") == -1

    observe = flags["retrieval_engine_observe"]
    assert observe["mode"] == "observe_parallel"
    assert observe["lane"] == "selected_file_text"
    assert observe["authority"] == "retrieval_engine"
    assert observe["status"] == "success"
    assert observe["terminal_reason"] == "success"
    assert observe["plan_summary"]["evidence_shape"] == "narrow_chunk"
    assert observe["plan_summary"]["needs_workbook_range"] is False
    assert observe["comparison"]["engine_reference_count"] == len(flags["references"])
    assert "accepted_outputs" not in observe
    assert "references" not in observe
    assert "source_cards" not in observe
    assert body["metadata"]["retrieval_engine_plan_summary"] == observe["plan_summary"]
    assert flags["retrieval_engine_plan_summary"] == observe["plan_summary"]
    assert raw_path not in repr(flags)


def test_first_pass_selected_source_processed_local_file_uses_engine_authority(
    monkeypatch,
):
    content = "The answer token is basalt-lantern-20260608."
    prepared_file = {
        "id": "alpha-file",
        "name": "selected-source-public-chat.txt",
        "context": "full",
        "type": "file",
        "focus_tier": "active",
        "focus_origin": "user_selection",
    }
    inline_source = _local_file_source(
        file_id="alpha-file",
        name="selected-source-public-chat.txt",
        content=content,
    )
    provider_calls = {"count": 0}
    captured_selected_files: dict[str, list[dict]] = {}
    original_package = middleware._retrieval_engine_selected_source_lane_package

    async def fake_prepare_files(*_args, **_kwargs):
        return [prepared_file], [inline_source], [], [], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="legacy-file",
                name="legacy-conflict.txt",
                content="Legacy compatibility fallback must not win this lane.",
            )
        ]

    class _SessionStub:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    def recording_package(**kwargs):
        captured_selected_files["value"] = copy.deepcopy(kwargs["selected_files"])
        return original_package(**kwargs)

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.SessionLocal",
        lambda: _SessionStub(),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.Files.get_file_by_id_and_user_id",
        lambda file_id, user_id, db=None: SimpleNamespace(
            id=file_id,
            filename="selected-source-public-chat.txt",
            data={"content": content, "status": "completed"},
            meta={"content_type": "text/plain"},
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        recording_package,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_strategy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware strategy must be bypassed")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_references",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware references must be bypassed")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_accepted_outputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware accepted outputs must be bypassed")
        ),
    )

    body = {
        "model": "test-model",
        "messages": [
            {
                "role": "user",
                "content": "What is the answer token in the selected file?",
            }
        ],
        "metadata": {
            "files": [prepared_file],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1", role="user"),
        )
    )

    assert provider_calls["count"] == 0
    assert captured_selected_files["value"][0]["id"] == "alpha-file"
    assert captured_selected_files["value"][0]["data"]["content"] == content
    assert flags["status"] == "success"
    assert flags["sources"][0]["source"]["id"] == "alpha-file"
    assert flags["sources"][0]["document"] == [content]
    assert flags["accepted_outputs"][0]["provenance"]["engine_authority"] is True
    assert flags["references"][0]["provenance"]["engine_authority"] is True
    assert flags["provenance"]["selected_source_runtime_mode"] == (
        "retrieval_engine_authority"
    )
    assert flags["provenance"].get("compatibility_fallback") is not True
    assert body["metadata"]["retrieval_engine_plan_summary"]["evidence_shape"] == (
        "narrow_chunk"
    )


def test_first_pass_selected_source_engine_unsupported_shape_falls_back_to_legacy(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "meta": {"content_type": "text/plain"},
    }
    provider_calls = {"count": 0}
    strategy_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="Legacy fallback remains authoritative for unsupported shape.",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    original_strategy = middleware._first_pass_selected_source_strategy

    def recording_strategy(*args, **kwargs):
        strategy_calls["count"] += 1
        return original_strategy(*args, **kwargs)

    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_strategy",
        recording_strategy,
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 1
    assert strategy_calls["count"] >= 1
    assert flags["status"] == "success"
    assert flags["sources"][0]["document"] == [
        "Legacy fallback remains authoritative for unsupported shape."
    ]
    assert flags["provenance"]["selected_source_runtime_mode"] == (
        "compatibility_fallback"
    )
    assert flags["provenance"]["compatibility_fallback"] is True
    assert flags["provenance"]["compatibility_boundary"] == (
        "legacy_selected_source_fallback_until_10_5"
    )
    assert (
        flags["provenance"]["compact_retrieval_provenance"]["runtime_mode"]
        == "compatibility_fallback"
    )
    observe = flags["retrieval_engine_observe"]
    assert observe["status"] == "no_evidence"
    assert observe["terminal_reason"] == "unsupported_source_shape"
    assert observe["authority"] == "legacy_selected_source"
    assert observe["diagnostics"][0]["code"] == (
        "retrieval_engine_unsupported_source_shape"
    )


def test_first_pass_selected_source_compatibility_hybrid_events_are_bounded(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "partial",
        "type": "text",
        "collection_name": "alpha-file",
        "meta": {"content_type": "text/plain"},
    }

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return retrieval_utils.RetrievalSourceList(
            [
                _local_file_source(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                    content="Fallback Atlas evidence.",
                )
            ],
            compatibility_provenance={
                "events": [
                    "hybrid_no_evidence_then_non_hybrid_fallback",
                    "non_hybrid_fallback_success",
                ]
            },
        )

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "what is atlas evidence"}],
                "metadata": {"files": [retrieval_candidate]},
            },
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert flags["status"] == "success"
    assert flags["provenance"]["compatibility_hybrid_events"] == [
        "hybrid_no_evidence_then_non_hybrid_fallback",
        "non_hybrid_fallback_success",
    ]
    assert (
        flags["provenance"]["compact_retrieval_provenance"][
            "compatibility_hybrid_events"
        ]
        == ["hybrid_no_evidence_then_non_hybrid_fallback", "non_hybrid_fallback_success"]
    )


def test_retrieval_engine_cutover_denied_scope_is_fail_closed():
    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {
                    "content": "Transformer grounding aligns model outputs with source text."
                },
            }
        ],
        active_source_scope={
            "status": "unresolved",
            "source_ids": ["alpha-file"],
            "reason": "ambiguous_selected_source",
        },
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    assert package["authority"]["state"] == "fail_closed"
    contract = package["contract"]
    assert contract["status"] == "blocked"
    assert contract["terminal_reason"] == "active_source_scope_unresolved"
    assert contract["accepted_outputs"] == []
    assert contract["references"] == []
    assert contract["sources"] == []
    assert package["observe"]["counts"]["accepted_output_count"] == 0
    assert package["observe"]["counts"]["reference_count"] == 0


def test_retrieval_engine_request_construction_error_is_marked_fallback(monkeypatch):
    def fail_adapter(*_args, **_kwargs):
        raise RuntimeError("adapter exploded at /srv/private/should-not-leak")

    monkeypatch.setattr(
        "open_webui.retrieval.engine_adapter.build_retrieval_engine_request",
        fail_adapter,
    )

    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {
                    "content": "Transformer grounding aligns model outputs with source text."
                },
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    assert package["authority"]["state"] == "fallback"
    assert package["authority"]["reason"] == "request_construction_error"
    assert package["attempt"]["engine_invoked"] is False
    assert package["attempt"]["fallback_reason"] == "request_construction_error"
    observe = package["observe"]
    assert observe["status"] == "error"
    assert observe["terminal_reason"] == "request_construction_error"
    assert observe["counts"]["accepted_output_count"] == 0
    assert observe["counts"]["reference_count"] == 0
    assert "/srv/private/should-not-leak" not in repr(observe)


def test_retrieval_engine_first_pass_contract_diagnostic_categories_zero_source_cards():
    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"
    selected_files = [
        {
            "id": "alpha-file",
            "name": "alpha-policy.txt",
            "type": "text",
            "path": raw_path,
            "meta": {"path": raw_path},
        }
    ]
    plan_summary = {
        "evidence_shape": "narrow_chunk",
        "required_capabilities": ["keyword"],
        "first_pass_evidence_state": "diagnostics",
    }
    cases = [
        ("no_evidence", "rejected_candidates_only", "candidate_rejected"),
        ("no_evidence", "unsupported_capability", "unsupported_connector_capability"),
        ("denied", "source_scope_violation", "source_scope_violation"),
        ("blocked", "active_source_scope_unresolved", "active_source_scope_unresolved"),
        ("no_evidence", "no_accepted_evidence", "no_accepted_evidence"),
        ("timeout", "retrieval_timeout", "retrieval_timeout"),
        ("malformed", "malformed_provider_output", "malformed_provider_output"),
        ("partial", "parser_loss", "parser_loss"),
    ]

    for status, terminal_reason, diagnostic_code in cases:
        result = _retrieval_engine_result_stub(
            status=status,
            terminal_reason=terminal_reason,
            diagnostics=[
                {
                    "code": diagnostic_code,
                    "details": {
                        "raw_path": raw_path,
                        "candidate_body": "candidate body must not become source cards",
                    },
                }
            ],
        )
        contract = _retrieval_engine_result_first_pass_contract(
            result=result,
            adapter_diagnostics=[],
            selected_files=selected_files,
            diagnostics=[],
            plan_summary=plan_summary,
        )

        expected_runtime_mode = (
            "retrieval_engine_fail_closed"
            if status in {"denied", "blocked"}
            else "retrieval_engine_diagnostics"
        )
        expected_bypass_reason = (
            "retrieval_engine_fail_closed"
            if status in {"denied", "blocked"}
            else "retrieval_engine_diagnostics_only"
        )
        expected_authority_state = (
            "fail_closed" if status in {"denied", "blocked"} else "diagnostics"
        )

        assert _retrieval_engine_authority_state(result) == expected_authority_state
        assert contract["status"] == status
        assert contract["terminal_reason"] == terminal_reason
        assert contract["accepted_outputs"] == []
        assert contract["references"] == []
        assert contract["sources"] == []
        assert contract["authorization_context"]["requested_source_ids"] == ["alpha-file"]
        assert contract["retry_policy"]["timeout_seconds"] == 25.0
        assert contract["context_budget"]["accepted_output_count"] == 0
        assert contract["context_budget"]["reference_count"] == 0
        assert contract["provenance"]["selected_source_runtime_mode"] == expected_runtime_mode
        assert contract["provenance"]["middleware_strategy_bypass_reason"] == (
            expected_bypass_reason
        )
        assert contract["provenance"]["plan_summary"]["evidence_shape"] == "narrow_chunk"
        assert contract["provenance"]["context_budget"]["accepted_output_count"] == 0
        diagnostic = contract["diagnostics"][0]
        assert diagnostic["reason"] == diagnostic_code
        assert diagnostic["outcome"] == status
        assert diagnostic["provenance"]["selected_source_runtime_mode"] == (
            expected_runtime_mode
        )
        assert diagnostic["provenance"]["engine_owned"] is True
        assert diagnostic["provenance"]["compatibility_fallback"] is False
        assert raw_path not in json.dumps(
            diagnostic["provenance"],
            ensure_ascii=False,
        )
        assert raw_path not in json.dumps(diagnostic, ensure_ascii=False)
        assert "candidate body must not become source cards" in json.dumps(
            diagnostic,
            ensure_ascii=False,
        )
        assert raw_path not in repr(contract["sources"])
        assert raw_path not in repr(contract["accepted_outputs"])
        assert raw_path not in repr(contract["references"])
        assert "candidate body must not become source cards" not in json.dumps(
            contract["sources"],
            ensure_ascii=False,
        )
        assert "candidate body must not become source cards" not in json.dumps(
            contract["accepted_outputs"],
            ensure_ascii=False,
        )
        assert "candidate body must not become source cards" not in json.dumps(
            contract["references"],
            ensure_ascii=False,
        )


def test_first_pass_selected_source_engine_diagnostics_only_bypass_legacy_output(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "meta": {"content_type": "text/plain"},
    }

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def emit_event(_event):
        return None

    async def raise_legacy_provider(*_args, **_kwargs):
        raise AssertionError("legacy provider should be bypassed")

    diagnostic_contract = _engine_authority_tool_contract(
        status="no_evidence",
        terminal_reason="no_accepted_evidence",
        include_evidence=False,
        diagnostics=[
            {
                "classification": "no_evidence",
                "reason": "no_accepted_evidence",
                "outcome": "no_evidence",
            }
        ],
    )
    diagnostic_contract["diagnostics"] = copy.deepcopy(
        diagnostic_contract["retrieval_diagnostics"]
    )
    diagnostic_contract["sources"] = []

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        raise_legacy_provider,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_strategy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware strategy must be bypassed")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        lambda **_kwargs: {
            "authority": {
                "state": "diagnostics",
                "reason": "no_accepted_evidence",
            },
            "contract": diagnostic_contract,
            "observe": {
                "mode": "observe_parallel",
                "lane": "selected_file_text",
                "authority": "retrieval_engine",
                "status": "no_evidence",
                "terminal_reason": "no_accepted_evidence",
                "plan_summary": {"evidence_shape": "narrow_chunk"},
                "counts": {
                    "candidate_count": 0,
                    "evidence_bundle_count": 0,
                    "accepted_output_count": 0,
                    "reference_count": 0,
                    "diagnostic_count": 1,
                },
                "diagnostics": [
                    {
                        "code": "no_accepted_evidence",
                        "kind": "diagnostics",
                        "severity": "info",
                        "phase": "acceptance",
                    }
                ],
            },
        },
    )

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "what is atlas evidence"}],
                "metadata": {"files": [retrieval_candidate]},
            },
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert flags["status"] == "no_evidence"
    assert flags["terminal_reason"] == "no_accepted_evidence"
    assert flags["sources"] == []
    assert flags["accepted_outputs"] == []
    assert flags["references"] == []
    assert flags["provenance"]["selected_source_runtime_mode"] == (
        "retrieval_engine_diagnostics"
    )
    assert flags["provenance"]["middleware_strategy_bypass_reason"] == (
        "retrieval_engine_diagnostics_only"
    )
    assert flags["first_pass_retrieval_strategy"]["retrieval_strategy"] == (
        "retrieval_engine_diagnostics"
    )
    assert flags["retrieval_engine_observe"]["authority"] == "retrieval_engine"


def test_first_pass_metadata_first_without_bounded_targeted_context_fails_closed(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "file": {"meta": {"name": "alpha-policy.txt"}},
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="semantic chunk should not be accepted for metadata-first first pass",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    body = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "列出最近两年的政策文件并按类型统计。"}
        ],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert provider_calls["count"] == 0
    assert flags["sources"] == []
    assert flags["accepted_outputs"] == []
    assert flags["references"] == []
    assert flags["status"] == "blocked"
    assert flags["terminal_reason"] == "metadata_first_targeted_evidence_required"
    assert flags["authorization_context"]["active_source_scope_state"] == "resolved"
    assert flags["retry_policy"]["max_retries"] == 0
    assert any(
        item.get("reason") == "metadata_first_targeted_evidence_required"
        for item in flags["retrieval_diagnostics"]
    )
    assert flags["diagnostics"] == flags["retrieval_diagnostics"]
    assert flags["provenance"]["strategy_used"]["retrieval_strategy"] == (
        "metadata_first_then_targeted_chunks"
    )
    strategy = flags["first_pass_retrieval_strategy"]
    assert strategy["retrieval_strategy"] == "metadata_first_then_targeted_chunks"
    assert strategy["semantic_chunk_lookup_ok"] is False
    assert "canonical_references" not in persisted_metadata
    assert "reference_cards" not in persisted_metadata


def test_first_pass_metadata_first_selected_collection_inventory_accepts_ordered_references(
    monkeypatch,
):
    class _FakeFileModel:
        def __init__(self, payload: dict):
            self._payload = payload

        def model_dump(self):
            return self._payload

    collection_candidate = {
        "id": "collection-1",
        "name": "Collection A",
        "type": "collection",
        "context": "full",
        "status": "processed",
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [collection_candidate], [], [], [collection_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return []

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.Knowledges.get_files_by_id",
        lambda _collection_id: [
            _FakeFileModel(
                {
                    "id": "doc-a",
                    "filename": "alpha-paper.txt",
                    "updated_at": 50,
                    "meta": {"name": "alpha-paper.txt"},
                    "data": {"content": "ALPHA_MARKER runtime inventory row."},
                }
            ),
            _FakeFileModel(
                {
                    "id": "doc-b",
                    "filename": "beta-paper.txt",
                    "updated_at": 80,
                    "meta": {"name": "beta-paper.txt"},
                    "data": {"content": "BETA_MARKER runtime inventory row."},
                }
            ),
        ],
    )

    body = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "请列出所选集合中的文档/文章标题，并说明你的排序依据。"}
        ],
        "metadata": {
            "files": [collection_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert provider_calls["count"] == 1
    assert flags["status"] == "success"
    assert flags["terminal_reason"] == "success"
    assert len(flags["references"]) >= 1
    assert len(flags["accepted_outputs"]) >= 1
    assert flags["provenance"]["compact_retrieval_provenance"]["basis"]["date_basis"] == (
        "file_updated_at"
    )
    assert flags["provenance"]["compact_retrieval_provenance"]["inventory_counts"] == {
        "selected_inventory_count": 2,
        "scoped_inventory_count": 2,
        "shortlist_count": 2,
    }
    assert len(persisted_metadata["canonical_references"]) >= 1
    assert len(persisted_metadata["reference_cards"]) >= 1
    for reference in persisted_metadata["canonical_references"]:
        for metadata_item in reference.get("metadata") or []:
            assert metadata_item.get("chunk_type") == "inventory_row"


def test_first_pass_metadata_first_selected_collection_missing_inventory_is_diagnostics_only(
    monkeypatch,
):
    collection_candidate = {
        "id": "collection-1",
        "name": "Collection A",
        "type": "collection",
        "context": "full",
        "status": "processed",
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [collection_candidate], [], [], [collection_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return []

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.Knowledges.get_files_by_id",
        lambda _collection_id: [],
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "列出最近两年的政策文件并按类型统计。"}],
        "metadata": {
            "files": [collection_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert provider_calls["count"] == 0
    assert flags["status"] == "blocked"
    assert flags["terminal_reason"] == "metadata_first_targeted_evidence_required"
    assert flags["references"] == []
    assert flags["accepted_outputs"] == []
    reasons = {
        str(item.get("reason") or "")
        for item in flags.get("retrieval_diagnostics", [])
        if isinstance(item, dict)
    }
    assert "inventory_unavailable" in reasons
    assert "canonical_references" not in persisted_metadata
    assert "reference_cards" not in persisted_metadata


def test_first_pass_metadata_first_alias_hint_fails_closed_without_targeted_context(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "file": {"meta": {"name": "alpha-policy.txt"}},
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="semantic chunk should not pass through metadata-first alias hints",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    body = {
        "model": "test-model",
        "messages": [
            {
                "role": "user",
                "content": "最近有哪些和风电相关的政策？请按清单列出。",
            }
        ],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_retrieval"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert provider_calls["count"] == 0
    assert flags["status"] == "blocked"
    assert flags["terminal_reason"] == "metadata_first_targeted_evidence_required"
    assert flags["references"] == []
    assert flags["accepted_outputs"] == []
    assert any(
        item.get("reason") == "metadata_first_targeted_evidence_required"
        for item in flags["retrieval_diagnostics"]
    )
    assert flags["first_pass_retrieval_strategy"]["metadata_first_intent"] is True
    assert "canonical_references" not in persisted_metadata
    assert "reference_cards" not in persisted_metadata


def test_first_pass_metadata_first_blocked_clears_inline_sources_from_prompt_context(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "file": {"meta": {"name": "alpha-policy.txt"}},
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return (
            [retrieval_candidate],
            [
                _local_file_source(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                    content="inline chunk should not be injected for blocked metadata-first turn",
                )
            ],
            [],
            [retrieval_candidate],
            [],
        )

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="provider chunk should never be accepted in blocked metadata-first turn",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "列出最近的重要文章并按类型整理。"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 0
    assert flags["status"] == "blocked"
    assert flags["terminal_reason"] == "metadata_first_targeted_evidence_required"
    assert flags["references"] == []
    assert flags["accepted_outputs"] == []
    assert flags["sources"] == []
    assert flags["no_evidence"] is True
    assert any(
        str(item.get("reason") or "") == "metadata_first_targeted_evidence_required"
        for item in flags["retrieval_diagnostics"]
        if isinstance(item, dict)
    )


def test_selected_source_metadata_first_diagnostics_only_marker_detects_blocked_turn():
    assert _is_selected_source_metadata_first_diagnostics_only(
        {
            "status": "blocked",
            "references": [],
            "accepted_outputs": [],
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
        }
    )
    assert not _is_selected_source_metadata_first_diagnostics_only(
        {
            "status": "success",
            "references": [_local_file_source()],
            "accepted_outputs": [{"type": "selected_source_evidence", "snippet": "ok"}],
            "first_pass_retrieval_strategy": {"metadata_first_intent": True},
        }
    )


def test_first_pass_narrow_fact_keeps_semantic_chunk_execution(monkeypatch):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "file": {"meta": {"name": "alpha-policy.txt"}},
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="Transformer grounding is a method for aligning model outputs with source text.",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert provider_calls["count"] == 1
    assert flags["sources"]
    assert flags["status"] == "success"
    assert flags["references"]
    assert flags["accepted_outputs"]
    assert flags["terminal_reason"] == "success"
    assert flags["diagnostics"] == flags["retrieval_diagnostics"]
    strategy = flags["first_pass_retrieval_strategy"]
    assert strategy["retrieval_strategy"] == "semantic_chunks"
    assert strategy["semantic_chunk_lookup_ok"] is True
    assert flags["provenance"]["strategy_used"]["retrieval_strategy"] == "semantic_chunks"
    assert flags["active_source_scope"]["status"] == "resolved"
    assert persisted_metadata["canonical_references"][0]["source"]["id"] == "alpha-file"


def test_first_pass_explicit_general_profile_lock_blocks_selected_source_research_intent(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "file": {"meta": {"name": "alpha-policy.txt"}},
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return (
            [retrieval_candidate],
            [
                _local_file_source(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                    content="inline evidence should not leak under incompatible profile lock",
                )
            ],
            [],
            [retrieval_candidate],
            [],
        )

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="provider evidence should not run under incompatible profile lock",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "最近有哪些和输电相关的政策文件？"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "selected_source_research",
                "non_promotion_reason": "explicit_incompatible_profile",
                "resolved_execution_profile": "general",
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert provider_calls["count"] == 0
    assert flags["sources"] == []
    assert flags["accepted_outputs"] == []
    assert flags["references"] == []
    assert flags["status"] == "blocked"
    assert flags["terminal_reason"] == "selected_source_intent_profile_locked"
    assert any(
        item.get("reason") == "selected_source_intent_profile_locked"
        for item in flags["retrieval_diagnostics"]
    )
    assert flags["first_pass_profile_lock"]["incompatible"] is True
    assert flags["first_pass_profile_lock"]["resolved_execution_profile"] == "general"
    assert "canonical_references" not in persisted_metadata


def test_first_pass_chat_profile_lock_blocks_selected_source_research_intent(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "file": {"meta": {"name": "alpha-policy.txt"}},
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="provider evidence should not run in chat profile lock",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "继续检索并补充证据"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "selected_source_research",
                "non_promotion_reason": "chat_profile_zero_tool_lane",
                "resolved_execution_profile": "chat",
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 0
    assert flags["sources"] == []
    assert flags["status"] == "blocked"
    assert flags["terminal_reason"] == "selected_source_intent_profile_locked"
    assert flags["first_pass_profile_lock"]["incompatible"] is True
    assert flags["first_pass_profile_lock"]["resolved_execution_profile"] == "chat"


def test_first_pass_selected_source_research_without_profile_lock_keeps_semantic_path(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "file": {"meta": {"name": "alpha-policy.txt"}},
    }
    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="selected source research still uses semantic retrieval when profile lock is not explicit",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "继续检索并补充证据"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "selected_source_research",
                "non_promotion_reason": "",
                "resolved_execution_profile": "research",
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert provider_calls["count"] == 1
    assert flags["status"] == "success"
    assert flags["sources"]
    assert flags["accepted_outputs"]
    assert flags["references"]
    assert flags["first_pass_profile_lock"]["incompatible"] is False
    assert persisted_metadata["canonical_references"][0]["source"]["id"] == "alpha-file"


def test_source_aware_query_constraints_keep_original_query_only_by_default():
    files = [{"id": "alpha-file", "name": "alpha-policy.txt", "context": "partial"}]

    queries = _constrain_retrieval_queries(
        original_query="alpha-policy.txt 的审批要求是什么？",
        generated_queries=[
            "approval requirements",
            "alpha-policy.txt approval requirements",
        ],
        retrieval_candidates=files,
        active_source_scope={
            "status": "resolved",
            "source_set_mode": "single",
            "source_ids": ["alpha-file"],
            "sources": [{"id": "alpha-file", "name": "alpha-policy.txt"}],
            "reason": "explicit_anchor",
        },
    )

    assert queries == ["alpha-policy.txt 的审批要求是什么？"]


def test_source_aware_query_constraints_preserve_explicit_anchors_for_variants():
    files = [{"id": "alpha-file", "name": "alpha-policy.txt", "context": "partial"}]

    queries = _constrain_retrieval_queries(
        original_query="alpha-policy.txt 的审批要求是什么？另外列出负责人。",
        generated_queries=[
            "approval requirements owner",
            "alpha-policy.txt approval requirements owner",
        ],
        retrieval_candidates=files,
        active_source_scope={
            "status": "resolved",
            "source_set_mode": "single",
            "source_ids": ["alpha-file"],
            "sources": [{"id": "alpha-file", "name": "alpha-policy.txt"}],
            "reason": "explicit_anchor",
        },
    )

    assert queries == [
        "alpha-policy.txt 的审批要求是什么？另外列出负责人。",
        "alpha-policy.txt approval requirements owner",
    ]


def test_source_aware_query_constraints_skip_ambiguous_scope_fallbacks():
    files = [
        {"id": "alpha-file", "name": "alpha-policy.txt", "context": "partial"},
        {"id": "beta-file", "name": "beta-policy.txt", "context": "partial"},
    ]

    queries = _constrain_retrieval_queries(
        original_query="这个文件说了什么？",
        generated_queries=["policy summary"],
        retrieval_candidates=files,
        active_source_scope={
            "status": "ambiguous",
            "source_set_mode": "none",
            "reason": "ambiguous_retrieval_scope",
            "confidence": "low",
        },
    )

    assert queries == []
    sidecar = Chats.build_reference_metadata_sidecar(
        metadata={
            "active_source_scope": {
                "status": "ambiguous",
                "source_set_mode": "none",
                "reason": "ambiguous_retrieval_scope",
            }
        },
        diagnostics=[
            {
                "kind": "retrieval_quality",
                "classification": "diagnostics",
                "reason": "ambiguous_retrieval_scope",
                "candidate_index": -1,
            }
        ],
        sources=[
            {
                "source": {"id": "alpha-file", "name": "alpha-policy.txt"},
                "document": ["stale evidence"],
                "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha-file"}],
            }
        ],
    )
    assert "canonical_references" not in sidecar


def test_zero_source_retrieval_status_history_is_hidden_on_reload_normalization():
    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "done": True,
            "content": "NO_FILE_ACCESS",
            "statusHistory": [
                {"action": "queries_generated", "queries": ["alpha policy"]},
                {"action": "sources_retrieved", "count": 0, "hidden": True, "done": True},
                {"action": "chat", "hidden": True, "done": True},
            ],
        }
    )

    status_history = normalized["statusHistory"]
    assert status_history[0]["action"] == "queries_generated"
    assert status_history[0]["hidden"] is True


def test_sourced_message_keeps_retrieval_status_history_visible():
    source = {
        "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
        "document": ["alpha evidence"],
        "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha-file"}],
    }

    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "done": True,
            "sources": [source],
            "statusHistory": [
                {"action": "queries_generated", "queries": ["alpha policy"]},
                {"action": "sources_retrieved", "count": 1, "done": True},
            ],
        }
    )

    assert normalized["statusHistory"][0].get("hidden") is not True
    assert len(_completion_sources_for_persistence(normalized.get("metadata") or {})) == 1
    assert len((normalized.get("metadata") or {}).get("canonical_references") or []) == 1


def _tool_output_item(tool_name: str, payload: object, arguments: object = "{}") -> list[dict]:
    return [
        {
            "type": "function_call",
            "id": "call_1",
            "call_id": "call_1",
            "name": tool_name,
            "arguments": arguments,
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": [
                {
                    "type": "input_text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ],
        },
    ]


def test_second_pass_query_selected_tool_success_persists_canonical_reference():
    payload = {
        "status": "success",
        "tool_name": "query_selected_knowledge_files",
        "query": "alpha policy requirement",
        "retrieval_round": 2,
        "canonical_references": [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "alpha-policy.txt",
                    "type": "file",
                },
                "document": ["alpha evidence"],
                "metadata": [
                    {
                        "source": "alpha-policy.txt",
                        "file_id": "alpha-file",
                        "retrieval_tool_name": "query_selected_knowledge_files",
                        "retrieval_round": 2,
                        "query": "alpha policy requirement",
                        "chunk_id": "chunk-1",
                    }
                ],
            }
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item("query_selected_knowledge_files", payload)
    )

    assert len(sidecar["canonical_references"]) == 1
    reference = sidecar["canonical_references"][0]
    assert reference["source"]["id"] == "alpha-file"
    assert reference["provenance"]["tool_name"] == "query_selected_knowledge_files"
    assert reference["provenance"]["retrieval_round"] == 2
    assert reference["provenance"]["query"] == "alpha policy requirement"
    assert "retrieval_diagnostics" not in sidecar


def test_search_tool_raw_results_do_not_become_canonical_references():
    payload = {
        "status": "success",
        "provider": "internet_search",
        "results": [
            {
                "title": "原始候选一",
                "url": "https://example.com/raw-1",
                "snippet": "候选网页摘要一",
            },
            {
                "title": "原始候选二",
                "url": "https://example.com/raw-2",
                "snippet": "候选网页摘要二",
            },
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item(
            "internet_search",
            payload,
            arguments={"query": "raw search candidates"},
        )
    )

    assert sidecar == {}


def test_search_tool_success_normalizes_explicit_accepted_web_evidence():
    payload = {
        "status": "success",
        "provider": "internet_search",
        "as_of": "2026-05-14",
        "accepted_results": [
            {
                "title": "政策原文",
                "url": "https://www.gov.cn/policy/2026/example.html",
                "content": "官方政策原文摘录",
                "source_class": "official_web",
            },
            {
                "title": "行业解读",
                "url": "https://example.com/analysis",
                "snippet": "第三方行业解读摘要",
                "type": "web",
            },
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item(
            "internet_search",
            payload,
            arguments={"query": "latest policy updates"},
        )
    )

    assert len(sidecar["canonical_references"]) == 2
    official_reference = sidecar["canonical_references"][0]
    generic_reference = sidecar["canonical_references"][1]

    assert official_reference["source"]["type"] == "official_web"
    assert official_reference["source_class"] == "official_web"
    assert official_reference["authority"] == "official"
    assert official_reference["provenance"]["tool_name"] == "internet_search"
    assert official_reference["provenance"]["provider"] == "internet_search"
    assert official_reference["provenance"]["query"] == "latest policy updates"
    assert official_reference["provenance"]["as_of"] == "2026-05-14"
    assert official_reference["provenance"]["domain"] == "gov.cn"
    assert generic_reference["source"]["type"] == "generic_web"
    assert generic_reference["source"]["url"] == "https://example.com/analysis"
    assert generic_reference["provenance"]["source_class"] == "generic_web"
    assert "retrieval_diagnostics" not in sidecar


def test_legacy_raw_web_sources_do_not_normalize_into_canonical_references():
    sidecar = Chats.build_reference_metadata_sidecar(
        sources=[
            {
                "source": {
                    "id": "https://example.com/raw-result",
                    "url": "https://example.com/raw-result",
                    "name": "Raw search candidate",
                    "type": "web",
                },
                "document": ["This candidate should not persist as accepted evidence."],
                "metadata": [
                    {
                        "source": "https://example.com/raw-result",
                        "name": "Raw search candidate",
                        "url": "https://example.com/raw-result",
                    }
                ],
            }
        ]
    )

    assert sidecar == {}


def test_search_tool_empty_results_become_diagnostics_only():
    payload = {
        "status": "empty",
        "provider": "internet_search",
        "results": [],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item(
            "internet_search",
            payload,
            arguments={"query": "missing policy"},
        )
    )

    assert "canonical_references" not in sidecar
    assert sidecar["retrieval_diagnostics"][0]["reason"] == "empty_search_results"
    assert sidecar["retrieval_diagnostics"][0]["tool_name"] == "internet_search"


def test_search_tool_weak_results_become_diagnostics_only():
    payload = {
        "status": "weak",
        "provider": "internet_search",
        "reason": "low_relevance",
        "results": [
            {
                "title": "候选网页",
                "url": "https://example.com/weak",
                "snippet": "相关性不足的候选结果",
            }
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item(
            "internet_search",
            payload,
            arguments={"query": "weak policy match"},
        )
    )

    assert "canonical_references" not in sidecar
    assert sidecar["retrieval_diagnostics"][0]["reason"] == "low_relevance"
    assert sidecar["retrieval_diagnostics"][0]["tool_name"] == "internet_search"


def test_webpage_blocked_result_becomes_diagnostics_only():
    payload = {
        "status": "blocked",
        "code": "provider_blocked_url",
        "detail": "blocked by upstream provider",
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item(
            "visit_webpage",
            payload,
            arguments={"url": "https://example.com/restricted"},
        )
    )

    assert "canonical_references" not in sidecar
    assert sidecar["retrieval_diagnostics"][0]["reason"] == "provider_blocked_url"
    assert sidecar["retrieval_diagnostics"][0]["url"] == "https://example.com/restricted"


def test_webpage_success_normalizes_into_canonical_reference():
    payload = {
        "status": "success",
        "provider": "visit_webpage",
        "retrieval_round": 3,
        "as_of": "2026-05-15",
        "freshness": "current",
        "content": "网页正文摘录，包含可以引用的政策条款。",
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item(
            "visit_webpage",
            payload,
            arguments={"url": "https://www.gov.cn/policy/example.html"},
        )
    )

    assert len(sidecar["canonical_references"]) == 1
    reference = sidecar["canonical_references"][0]
    assert reference["source"]["url"] == "https://www.gov.cn/policy/example.html"
    assert reference["source"]["type"] == "official_web"
    assert reference["provenance"]["tool_name"] == "visit_webpage"
    assert reference["provenance"]["provider"] == "visit_webpage"
    assert reference["provenance"]["retrieval_round"] == 3
    assert reference["provenance"]["as_of"] == "2026-05-15"
    assert reference["provenance"]["freshness"] == "current"
    assert reference["provenance"]["domain"] == "gov.cn"
    assert "retrieval_diagnostics" not in sidecar


def test_successful_page_read_clears_stale_no_evidence_diagnostics():
    persisted_metadata = _build_assistant_reference_persistence_metadata(
        {"sources": []},
        message_metadata={
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_quality",
                    "classification": "no_evidence",
                    "reason": "empty_search_results",
                    "tool_name": "internet_search",
                    "query": "passport processing times",
                }
            ]
        },
        tool_outputs=_tool_output_item(
            "visit_webpage",
            {
                "status": "success",
                "provider": "visit_webpage",
                "content": "The official page confirms the current processing times.",
            },
            arguments={
                "url": "https://travel.state.gov/content/travel/en/passports/how-apply/processing-times.html"
            },
        ),
    )

    assert len(persisted_metadata["canonical_references"]) == 1
    assert persisted_metadata["canonical_references"][0]["source"]["type"] == (
        "official_web"
    )
    assert "retrieval_diagnostics" not in persisted_metadata


def test_web_references_dedupe_by_normalized_url_and_source_class():
    sidecar = Chats.build_reference_metadata_sidecar(
        metadata={
            "canonical_references": [
                {
                    "type": "retrieval_reference",
                    "source": {
                        "id": "https://example.com/policy",
                        "url": "https://example.com/policy",
                        "name": "外部政策解读",
                        "type": "generic_web",
                    },
                    "document": ["已接受的第三方网页证据。"],
                    "metadata": [
                        {
                            "source": "https://example.com/policy",
                            "url": "https://example.com/policy",
                            "name": "外部政策解读",
                            "tool_name": "internet_search",
                            "provider": "internet_search",
                            "query": "external policy context",
                            "source_class": "generic_web",
                        }
                    ],
                }
            ]
        },
        sources=[
            {
                "source": {
                    "id": "https://example.com/policy/",
                    "url": "https://example.com/policy/",
                    "name": "外部政策解读",
                    "type": "web",
                },
                "document": ["已接受的第三方网页证据。"],
                "metadata": [
                    {
                        "source": "https://example.com/policy/",
                        "url": "https://example.com/policy/",
                        "name": "外部政策解读",
                        "tool_name": "internet_search",
                        "provider": "internet_search",
                        "query": "external policy context",
                    }
                ],
                "provenance": {
                    "tool_name": "internet_search",
                    "provider": "internet_search",
                    "query": "external policy context",
                },
            }
        ],
    )

    assert len(sidecar["canonical_references"]) == 1
    reference = sidecar["canonical_references"][0]
    assert reference["source"]["type"] == "generic_web"
    assert reference["provenance"]["tool_name"] == "internet_search"
    assert "retrieval_diagnostics" not in sidecar


def test_mixed_selected_file_and_web_references_preserve_both_source_classes():
    persisted_metadata = _build_assistant_reference_persistence_metadata(
        {
            "sources": [
                {
                    "source": {
                        "id": "alpha-file",
                        "name": "alpha-policy.txt",
                        "type": "file",
                    },
                    "document": ["alpha evidence"],
                    "metadata": [
                        {
                            "source": "alpha-policy.txt",
                            "file_id": "alpha-file",
                        }
                    ],
                },
                {
                    "source": {
                        "id": "https://example.com/policy/",
                        "url": "https://example.com/policy/",
                        "name": "外部政策解读",
                        "type": "web",
                    },
                    "document": ["外部网页证据"],
                    "metadata": [
                        {
                            "source": "https://example.com/policy/",
                            "name": "外部政策解读",
                            "url": "https://example.com/policy/",
                            "tool_name": "internet_search",
                            "provider": "internet_search",
                            "query": "external policy context",
                        }
                    ],
                    "provenance": {
                        "tool_name": "internet_search",
                        "provider": "internet_search",
                        "query": "external policy context",
                    },
                },
            ]
        },
        message_metadata={
            "canonical_references": [
                {
                    "type": "retrieval_reference",
                    "source": {
                        "id": "https://example.com/policy",
                        "url": "https://example.com/policy",
                        "name": "外部政策解读",
                        "type": "generic_web",
                    },
                    "document": ["外部网页证据"],
                    "metadata": [
                        {
                            "source": "https://example.com/policy",
                            "name": "外部政策解读",
                            "url": "https://example.com/policy",
                            "tool_name": "internet_search",
                            "provider": "internet_search",
                            "query": "external policy context",
                            "source_class": "generic_web",
                        }
                    ],
                }
            ]
        },
        tool_outputs=_tool_output_item(
            "internet_search",
            {
                "status": "success",
                "accepted_results": [
                    {
                        "title": "外部政策解读",
                        "url": "https://example.com/policy",
                        "content": "外部网页证据",
                        "type": "web",
                    }
                ],
            },
            arguments={"query": "external policy context"},
        ),
    )

    assert len(persisted_metadata["canonical_references"]) == 2
    assert {
        item["source"].get("type")
        for item in persisted_metadata["canonical_references"]
    } == {"file", "generic_web"}


def test_second_pass_read_selected_file_success_persists_canonical_reference():
    payload = {
        "status": "success",
        "tool_name": "read_selected_file",
        "query": "read:alpha-file",
        "retrieval_round": 3,
        "canonical_references": [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "alpha-policy.txt",
                    "type": "file",
                },
                "document": ["bounded excerpt from selected file"],
                "metadata": [
                    {
                        "source": "alpha-policy.txt",
                        "file_id": "alpha-file",
                        "retrieval_tool_name": "read_selected_file",
                        "retrieval_round": 3,
                        "query": "read:alpha-file",
                    }
                ],
            }
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item("read_selected_file", payload)
    )

    assert len(sidecar["canonical_references"]) == 1
    assert sidecar["canonical_references"][0]["source"]["id"] == "alpha-file"
    assert sidecar["canonical_references"][0]["provenance"]["tool_name"] == (
        "read_selected_file"
    )
    provenance = sidecar["canonical_references"][0]["provenance"]
    assert provenance["old_chat_compatibility_reader"] is True
    assert provenance["retrieval_authority"] == "historical_tool_output"
    assert provenance["creates_new_retrieval_authority"] is False
    assert provenance.get("engine_authority") is not True
    assert "retrieval_diagnostics" not in sidecar


def test_second_pass_empty_result_persists_diagnostics_only():
    payload = {
        "status": "empty",
        "tool_name": "query_selected_knowledge_files",
        "query": "missing policy",
        "retrieval_round": 2,
        "canonical_references": [],
        "retrieval_diagnostics": [
            {
                "kind": "retrieval_quality",
                "classification": "no_evidence",
                "reason": "empty_retrieval_result",
                "tool_name": "query_selected_knowledge_files",
                "query": "missing policy",
                "retrieval_round": 2,
            }
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item("query_selected_knowledge_files", payload)
    )

    assert "canonical_references" not in sidecar
    assert sidecar["retrieval_diagnostics"][0]["reason"] == "empty_retrieval_result"


def test_second_pass_weak_payload_drops_canonical_references():
    payload = {
        "status": "weak",
        "tool_name": "query_selected_knowledge_files",
        "reason": "low_relevance",
        "canonical_references": [
            {
                "source": {
                    "id": "weak-file",
                    "name": "weak.txt",
                    "type": "file",
                },
                "document": ["weak evidence must not render"],
            }
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item("query_selected_knowledge_files", payload)
    )

    assert "canonical_references" not in sidecar
    assert sidecar["retrieval_diagnostics"][0]["reason"] == "low_relevance"


def test_second_pass_selected_source_failure_statuses_normalize_to_diagnostics_only():
    cases = [
        ("empty", "no_evidence", "empty_retrieval_result"),
        ("no_evidence", "no_evidence", "empty_retrieval_result"),
        ("weak_evidence", "weak_evidence", "weak_evidence"),
        ("low_relevance", "low_relevance", "low_relevance"),
        ("timeout", "timeout", "retrieval_timeout"),
        ("malformed", "malformed", "malformed_retrieval_output"),
        ("unauthorized", "permission_denied", "permission_denied"),
        ("permission_denied", "permission_denied", "permission_denied"),
    ]

    for status, expected_outcome, expected_reason in cases:
        payload = {
            "status": status,
            "tool_name": "query_selected_knowledge_files",
            "query": f"{status} query",
            "retrieval_round": 2,
            "canonical_references": [
                {
                    "source": {
                        "id": f"{status}-file",
                        "name": f"{status}.txt",
                        "type": "file",
                    },
                    "document": ["must not render"],
                }
            ],
        }

        sidecar = Chats.build_reference_metadata_sidecar(
            tool_outputs=_tool_output_item("query_selected_knowledge_files", payload)
        )

        assert "canonical_references" not in sidecar
        diagnostic = sidecar["retrieval_diagnostics"][0]
        assert diagnostic["outcome"] == expected_outcome
        assert diagnostic["reason"] == expected_reason
        assert diagnostic["tool_name"] == "query_selected_knowledge_files"


def test_second_pass_permission_denied_diagnostic_omits_sensitive_detail():
    payload = {
        "status": "permission_denied",
        "tool_name": "read_selected_file",
        "detail": "secret-file-id exists but is outside this user's scope",
        "canonical_references": [
            {
                "source": {
                    "id": "secret-file-id",
                    "name": "secret.txt",
                    "type": "file",
                },
                "document": ["must not render"],
            }
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item("read_selected_file", payload)
    )

    assert "canonical_references" not in sidecar
    diagnostic = sidecar["retrieval_diagnostics"][0]
    assert diagnostic["outcome"] == "permission_denied"
    assert diagnostic["reason"] == "permission_denied"
    assert "detail" not in diagnostic
    assert "secret-file-id" not in json.dumps(diagnostic, ensure_ascii=False)


def test_second_pass_malformed_tool_output_synthesizes_diagnostic_only():
    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item("query_selected_knowledge_files", "not json")
    )

    assert "canonical_references" not in sidecar
    diagnostic = sidecar["retrieval_diagnostics"][0]
    assert diagnostic["outcome"] == "malformed"
    assert diagnostic["reason"] == "malformed_retrieval_output"
    assert diagnostic["tool_name"] == "query_selected_knowledge_files"


def test_second_pass_denied_payload_synthesizes_diagnostic_only():
    payload = {
        "status": "error",
        "tool_id": "builtin:retrieval",
        "function_name": "read_selected_file",
        "code": "requested_file_out_of_scope",
        "detail": "file not selected",
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item("read_selected_file", payload)
    )

    assert "canonical_references" not in sidecar
    assert sidecar["retrieval_diagnostics"][0]["reason"] == (
        "requested_file_out_of_scope"
    )
    assert "sources" not in sidecar


def test_second_pass_error_payload_without_status_synthesizes_diagnostic_only():
    payload = {
        "tool_name": "read_selected_file",
        "error": "timeout while reading selected file",
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        tool_outputs=_tool_output_item("read_selected_file", payload)
    )

    assert "canonical_references" not in sidecar
    assert sidecar["retrieval_diagnostics"][0]["reason"] == (
        "timeout while reading selected file"
    )


def test_second_pass_reference_dedupes_with_first_pass_source():
    first_pass = {
        "source": {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
        "document": ["alpha evidence"],
        "metadata": [{"source": "alpha-policy.txt", "file_id": "alpha-file"}],
    }
    second_pass_payload = {
        "status": "success",
        "tool_name": "query_selected_knowledge_files",
        "query": "alpha policy requirement",
        "retrieval_round": 2,
        "canonical_references": [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "alpha-policy.txt",
                    "type": "file",
                },
                "document": ["alpha evidence"],
                "metadata": [
                    {
                        "source": "alpha-policy.txt",
                        "file_id": "alpha-file",
                        "retrieval_tool_name": "query_selected_knowledge_files",
                        "retrieval_round": 2,
                        "query": "alpha policy requirement",
                    }
                ],
            }
        ],
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        sources=[first_pass],
        tool_outputs=_tool_output_item(
            "query_selected_knowledge_files", second_pass_payload
        ),
    )

    assert len(sidecar["canonical_references"]) == 1
    assert sidecar["canonical_references"][0]["provenance"]["tool_name"] == (
        "query_selected_knowledge_files"
    )


def test_second_pass_diagnostics_do_not_normalize_into_sources_or_references():
    diagnostic_payload = {
        "status": "denied",
        "tool_name": "query_selected_knowledge_files",
        "retrieval_diagnostics": [
            {
                "kind": "retrieval_quality",
                "classification": "no_evidence",
                "reason": "requested_source_out_of_scope",
                "tool_name": "query_selected_knowledge_files",
            }
        ],
        "sources": [
            {
                "kind": "retrieval_quality",
                "classification": "no_evidence",
                "reason": "do_not_render",
            }
        ],
    }

    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "output": _tool_output_item(
                "query_selected_knowledge_files", diagnostic_payload
            ),
        }
    )

    metadata = normalized["metadata"]
    assert "sources" not in normalized
    assert "canonical_references" not in metadata
    assert metadata["retrieval_diagnostics"][0]["reason"] == (
        "requested_source_out_of_scope"
    )


def test_web_diagnostics_do_not_normalize_into_sources_or_references():
    diagnostic_payload = {
        "status": "denied",
        "reason": "search_provider_denied_query",
        "results": [
            {
                "title": "should not render",
                "url": "https://example.com/hidden",
                "content": "hidden result",
            }
        ],
    }

    normalized = Chats._normalize_message_reference_sidecar(
        {
            "role": "assistant",
            "output": _tool_output_item(
                "internet_search",
                diagnostic_payload,
                arguments={"query": "blocked query"},
            ),
        }
    )

    metadata = normalized["metadata"]
    assert "sources" not in normalized
    assert "canonical_references" not in metadata
    assert metadata["retrieval_diagnostics"][0]["reason"] == (
        "search_provider_denied_query"
    )


def _selected_source_tool_request_stub(
    *,
    hybrid_enabled: bool = False,
    hybrid_enriched_texts: bool = False,
):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    TOP_K=4,
                    TOP_K_RERANKER=4,
                    RELEVANCE_THRESHOLD=0.0,
                    HYBRID_BM25_WEIGHT=0.0,
                    ENABLE_RAG_HYBRID_SEARCH=hybrid_enabled,
                    ENABLE_RAG_HYBRID_SEARCH_ENRICHED_TEXTS=hybrid_enriched_texts,
                    BYPASS_EMBEDDING_AND_RETRIEVAL=False,
                ),
                EMBEDDING_FUNCTION=lambda text, prefix=None, user=None: [0.1],
                RERANKING_FUNCTION=None,
            )
        )
    )


def _selected_source_file_candidate(file_id: str, name: str) -> dict:
    return {
        "id": file_id,
        "name": name,
        "type": "text",
        "collection_name": file_id,
        "context": "partial",
        "file": {
            "meta": {
                "name": name,
            }
        },
    }


def _diagnostic_reason_set(payload: dict) -> set[str]:
    return {
        str(item.get("reason") or "")
        for item in payload.get("retrieval_diagnostics", [])
        if isinstance(item, dict)
    }


def _compatibility_query_result(
    text: str | None,
    *,
    file_id: str = "alpha-file",
    name: str = "alpha-policy.txt",
) -> dict:
    documents = [text] if text is not None else []
    metadatas = [{"file_id": file_id, "name": name}] if text is not None else []
    distances = [0.91] if text is not None else []
    return {
        "documents": [documents],
        "metadatas": [metadatas],
        "distances": [distances],
    }


def test_get_sources_from_items_hybrid_success_skips_non_hybrid_fallback(monkeypatch):
    calls = {"hybrid": 0, "non_hybrid": 0}

    async def fake_hybrid_search(**kwargs):
        calls["hybrid"] += 1
        assert kwargs["collection_names"] == {"alpha-file"}
        assert kwargs["queries"] == ["atlas evidence"]
        return _compatibility_query_result("Hybrid Atlas evidence.")

    async def fake_non_hybrid_search(**_kwargs):
        calls["non_hybrid"] += 1
        return _compatibility_query_result("Non-hybrid fallback should stay unused.")

    monkeypatch.setattr(retrieval_utils, "is_knowflow_enabled", lambda _config: False)
    monkeypatch.setattr(
        retrieval_utils,
        "query_collection_with_hybrid_search",
        fake_hybrid_search,
    )
    monkeypatch.setattr(retrieval_utils, "query_collection", fake_non_hybrid_search)

    sources = asyncio.run(
        retrieval_utils.get_sources_from_items(
            request=_selected_source_tool_request_stub(hybrid_enabled=True),
            items=[{"collection_name": "alpha-file", "name": "alpha-policy.txt"}],
            queries=["atlas evidence"],
            embedding_function=lambda text, prefix=None, user=None: [0.1],
            k=2,
            reranking_function=None,
            k_reranker=4,
            r=0.0,
            hybrid_bm25_weight=0.25,
            hybrid_search=True,
            full_context=False,
            user=SimpleNamespace(id="user-1", role="user"),
        )
    )

    assert calls == {"hybrid": 1, "non_hybrid": 0}
    assert sources[0]["document"] == ["Hybrid Atlas evidence."]
    assert sources.compatibility_provenance["events"] == ["hybrid_success"]
    assert sources.compatibility_diagnostics == []


def test_get_sources_from_items_hybrid_empty_runs_non_hybrid_fallback(monkeypatch):
    calls = {"hybrid": 0, "non_hybrid": 0}

    async def fake_hybrid_search(**kwargs):
        calls["hybrid"] += 1
        assert kwargs["collection_names"] == {"alpha-file"}
        assert kwargs["queries"] == ["atlas evidence"]
        return _compatibility_query_result(None)

    async def fake_non_hybrid_search(**kwargs):
        calls["non_hybrid"] += 1
        assert kwargs["collection_names"] == {"alpha-file"}
        assert kwargs["queries"] == ["atlas evidence"]
        return _compatibility_query_result("Fallback Atlas evidence.")

    monkeypatch.setattr(retrieval_utils, "is_knowflow_enabled", lambda _config: False)
    monkeypatch.setattr(
        retrieval_utils,
        "query_collection_with_hybrid_search",
        fake_hybrid_search,
    )
    monkeypatch.setattr(retrieval_utils, "query_collection", fake_non_hybrid_search)

    sources = asyncio.run(
        retrieval_utils.get_sources_from_items(
            request=_selected_source_tool_request_stub(hybrid_enabled=True),
            items=[{"collection_name": "alpha-file", "name": "alpha-policy.txt"}],
            queries=["atlas evidence"],
            embedding_function=lambda text, prefix=None, user=None: [0.1],
            k=2,
            reranking_function=None,
            k_reranker=4,
            r=0.0,
            hybrid_bm25_weight=0.25,
            hybrid_search=True,
            full_context=False,
            user=SimpleNamespace(id="user-1", role="user"),
        )
    )

    assert calls == {"hybrid": 1, "non_hybrid": 1}
    assert sources[0]["document"] == ["Fallback Atlas evidence."]
    assert sources.compatibility_provenance["events"] == [
        "hybrid_no_evidence_then_non_hybrid_fallback",
        "non_hybrid_fallback_success",
    ]
    assert sources.compatibility_diagnostics == []


def test_get_sources_from_items_hybrid_error_reports_no_silent_empty_result(
    monkeypatch,
):
    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"

    async def fake_hybrid_search(**_kwargs):
        raise asyncio.TimeoutError(f"hybrid timed out while reading {raw_path}")

    async def fake_non_hybrid_search(**kwargs):
        assert kwargs["collection_names"] == {"alpha-file"}
        assert kwargs["queries"] == ["atlas evidence"]
        return _compatibility_query_result(None)

    monkeypatch.setattr(retrieval_utils, "is_knowflow_enabled", lambda _config: False)
    monkeypatch.setattr(
        retrieval_utils,
        "query_collection_with_hybrid_search",
        fake_hybrid_search,
    )
    monkeypatch.setattr(retrieval_utils, "query_collection", fake_non_hybrid_search)

    sources = asyncio.run(
        retrieval_utils.get_sources_from_items(
            request=_selected_source_tool_request_stub(hybrid_enabled=True),
            items=[{"collection_name": "alpha-file", "name": "alpha-policy.txt"}],
            queries=["atlas evidence"],
            embedding_function=lambda text, prefix=None, user=None: [0.1],
            k=2,
            reranking_function=None,
            k_reranker=4,
            r=0.0,
            hybrid_bm25_weight=0.25,
            hybrid_search=True,
            full_context=False,
            user=SimpleNamespace(id="user-1", role="user"),
        )
    )

    assert sources == []
    assert sources.compatibility_provenance["events"] == [
        "hybrid_error_then_non_hybrid_fallback",
        "non_hybrid_fallback_no_evidence",
    ]
    assert _diagnostic_reason_set(
        {"retrieval_diagnostics": sources.compatibility_diagnostics}
    ) == {
        "hybrid_error_then_non_hybrid_fallback",
        "non_hybrid_fallback_no_evidence",
    }
    assert raw_path not in repr(sources.compatibility_diagnostics)


def _engine_authority_tool_contract(
    *,
    status: str = "success",
    source_id: str = "alpha-file",
    terminal_reason: str = "success",
    include_evidence: bool = True,
    diagnostics: list[dict] | None = None,
) -> dict:
    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"
    engine_authority = status in {"success", "partial"} and include_evidence
    runtime_mode = (
        "retrieval_engine_authority"
        if engine_authority
        else (
            "retrieval_engine_fail_closed"
            if status in {"denied", "blocked"}
            else "retrieval_engine_diagnostics"
        )
    )
    bypass_reason = (
        "retrieval_engine_authority_succeeded"
        if runtime_mode == "retrieval_engine_authority"
        else (
            "retrieval_engine_fail_closed"
            if runtime_mode == "retrieval_engine_fail_closed"
            else "retrieval_engine_diagnostics_only"
        )
    )
    reference = {
        "source": {
            "id": source_id,
            "name": "alpha-policy.txt",
            "type": "file",
            "path": raw_path,
        },
        "document": ["Engine accepted evidence for Atlas policy."],
        "metadata": [
            {
                "file_id": source_id,
                "name": "alpha-policy.txt",
                "retrieval_engine_reference_id": "reference:alpha-file",
                "retrieval_engine_output_id": "accepted:bundle:alpha-file",
                "source_anchor": {"kind": "text_span", "start": 0, "end": 42},
                "path": raw_path,
                "engine_authority": True,
            }
        ],
        "provenance": {
            "engine_authority": True,
            "reference_id": "reference:alpha-file",
            "output_id": "accepted:bundle:alpha-file",
            "server_path": raw_path,
        },
    }
    accepted_output = {
        "output_id": "accepted:bundle:alpha-file",
        "type": "selected_source_evidence",
        "tool_name": "query_selected_knowledge_files",
        "source": {"id": source_id, "name": "alpha-policy.txt", "type": "file"},
        "snippet": "Engine accepted evidence for Atlas policy.",
        "provenance": {
            "engine_authority": True,
            "file_id": source_id,
            "reference_id": "reference:alpha-file",
            "output_id": "accepted:bundle:alpha-file",
            "source_anchor": {"kind": "text_span", "start": 0, "end": 42},
            "evidence_bundle_ids": ["bundle:alpha-file"],
            "file_path": raw_path,
        },
    }
    return {
        "contract_version": "open_webui_selected_source_engine_contract_v1",
        "engine_version": "open_webui_selected_source_engine_v1",
        "boundary_owned": True,
        "boundary_owner": "open_webui.retrieval.engine_contract",
        "source_scope_echo": {
            "status": "resolved",
            "source_ids": [source_id],
            "authorization_generation": "open_webui_selected_source_engine_v1",
        },
        "status": status,
        "terminal_reason": terminal_reason,
        "accepted_outputs": [accepted_output] if include_evidence else [],
        "references": [reference] if include_evidence else [],
        "retrieval_diagnostics": diagnostics or [],
        "authorization_context": {
            "selected_inventory_count": 1,
            "scoped_inventory_count": 1,
            "active_source_scope_state": "resolved",
            "requested_source_ids": [source_id],
            "authorization_generation": "open_webui_selected_source_engine_v1",
        },
        "retry_policy": {
            "max_retries": 0,
            "retries_attempted": 0,
            "retry_allowed": False,
            "timeout_seconds": 25.0,
        },
        "context_budget": {
            "requested_budget_tokens": 128,
            "estimated_used_tokens": 32,
            "accepted_output_count": 1 if include_evidence else 0,
            "reference_count": 1 if include_evidence else 0,
            "candidate_accepted_bundle_count": 1 if include_evidence else 0,
            "truncated_accepted_bundle_count": (
                1 if status == "partial" and terminal_reason == "budget_limited" else 0
            ),
            "omitted_accepted_bundle_count": 0,
            "truncated": bool(
                status == "partial" and terminal_reason == "budget_limited"
            ),
            "budget": {
                "policy": "deterministic_complete_evidence_first",
                "budget_policy": "selected_source_first_pass",
                "return_policy": "accepted_evidence_only",
                "ranking_policy": "selected_file_text_cutover",
                "engine_version": "open_webui_selected_source_engine_v1",
                "authorization_generation": "open_webui_selected_source_engine_v1",
                "cache_eligible": True,
                "cache_missing_dimensions": [],
                "source_card_count": 1 if include_evidence else 0,
                "ordinary_prompt_context_count": 1 if include_evidence else 0,
            },
        },
        "plan_summary": {
            "evidence_shape": "narrow_chunk",
            "selected_source_ids": [source_id],
            "selected_source_count": 1,
            "first_pass_evidence_state": status,
        },
        "provenance": {
            "engine_authority": engine_authority,
            "middleware_strategy_bypassed": True,
            "selected_source_runtime_mode": runtime_mode,
            "retrieval_runtime_mode": "engine_owned",
            "engine_owned": True,
            "middleware_strategy_bypass_reason": bypass_reason,
            "contract_version": "open_webui_selected_source_engine_contract_v1",
            "engine_version": "open_webui_selected_source_engine_v1",
            "boundary_owned": True,
            "boundary_owner": "open_webui.retrieval.engine_contract",
        },
    }


def _trusted_engine_tool_metadata(
    contract: dict,
    *,
    file_id: str = "alpha-file",
    name: str = "alpha-policy.txt",
    turn_id: str = "turn-current-42",
    message_id: str = "msg-current-42",
) -> dict:
    return {
        "retrieval_engine_first_pass_contract": contract,
        "turn_id": turn_id,
        "message_id": message_id,
        "retrieval_engine_attempt": {
            "engine_invoked": True,
            "contract_present": True,
            "contract_valid": True,
            "contract_version": "open_webui_selected_source_engine_contract_v1",
            "engine_version": "open_webui_selected_source_engine_v1",
            "terminal_status": str(contract.get("status") or ""),
            "terminal_reason": str(contract.get("terminal_reason") or ""),
            "runtime_mode": "engine_owned",
            "retrieval_runtime_mode": "engine_owned",
            "selected_source_runtime_mode": str(
                (contract.get("provenance") or {}).get("selected_source_runtime_mode")
                or ""
            ),
            "fallback_reason": "",
            "failure_class": "",
            "invalid_reasons": [],
            "turn_id": turn_id,
            "message_id": message_id,
            "boundary_owned": True,
            "boundary_owner": "open_webui.retrieval.engine_contract",
        },
        "active_source_scope": _resolved_active_source_scope(
            file_id=file_id,
            name=name,
        ),
    }


def _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch):
    def raise_legacy_policy(*_args, **_kwargs):
        raise AssertionError("legacy selected-source tool policy should be bypassed")

    async def raise_legacy_exact_lookup(*_args, **_kwargs):
        raise AssertionError("legacy exact lookup should be bypassed")

    for name, replacement in {
        "_selected_retrieval_normalize_strategy": raise_legacy_policy,
        "_selected_retrieval_run_local_exact_lookup": raise_legacy_exact_lookup,
        "_selected_retrieval_apply_structured_precision_guard": raise_legacy_policy,
        "_selected_retrieval_accepted_outputs": raise_legacy_policy,
        "_filter_selected_retrieval_sources_by_query": raise_legacy_policy,
    }.items():
        monkeypatch.setattr(f"open_webui.utils.tools.{name}", replacement)

    async def raise_legacy_provider(*_args, **_kwargs):
        raise AssertionError("legacy retrieval provider should be bypassed")

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        raise_legacy_provider,
    )


def test_query_selected_knowledge_files_uses_engine_authority_without_legacy_policy(
    monkeypatch,
):
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)
    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"

    response = asyncio.run(
        query_selected_knowledge_files(
            query=(
                "anchor=08063 answer_slot=labor filter=recent "
                "What does Atlas say?"
            ),
            source_ids=["alpha-file"],
            evidence_need="structured_exact",
            __request__=_selected_source_tool_request_stub(hybrid_enabled=True),
            __files__=[],
            __metadata__=_trusted_engine_tool_metadata(
                _engine_authority_tool_contract(),
            ),
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success"
    assert response["canonical_references"] == response["references"]
    assert response["canonical_references"][0]["source"]["id"] == "alpha-file"
    assert response["accepted_outputs"][0]["snippet"] == (
        "Engine accepted evidence for Atlas policy."
    )
    assert response["strategy_used"]["retrieval_strategy"] == "retrieval_engine_authority"
    assert response["strategy_used"]["tool_handler_policy_bypassed"] is True
    assert response["provenance"]["tool_handler_policy_bypassed"] is True
    assert response["provenance"]["tool_handler_bypass_reason"] == (
        "retrieval_engine_authority_succeeded"
    )
    assert response["retrieval_engine_plan_summary"]["evidence_shape"] == "narrow_chunk"
    assert raw_path not in repr(response)


def test_read_selected_file_uses_engine_authority_without_legacy_policy(monkeypatch):
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)
    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"

    response = asyncio.run(
        read_selected_file(
            source_id="alpha-file",
            query="read the selected file exactly",
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=_trusted_engine_tool_metadata(
                _engine_authority_tool_contract(),
            ),
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success"
    assert response["source_id"] == "alpha-file"
    assert response["canonical_references"][0]["source"]["id"] == "alpha-file"
    assert response["accepted_outputs"][0]["source"]["id"] == "alpha-file"
    assert response["strategy_used"]["retrieval_strategy"] == "retrieval_engine_authority"
    assert response["provenance"]["tool_handler_bypass_reason"] == (
        "retrieval_engine_authority_succeeded"
    )
    assert raw_path not in repr(response)


def test_selected_source_tool_engine_partial_maps_budget_and_keeps_diagnostics_separate(
    monkeypatch,
):
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)
    rejected_candidate = "Rejected candidate body must stay out of accepted lanes."
    diagnostics = [
        {
            "classification": "diagnostics",
            "reason": "budget_limited",
            "outcome": "partial",
            "detail": {"candidate_body": rejected_candidate},
        }
    ]

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say with a strict budget?",
            source_ids=["alpha-file"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=_trusted_engine_tool_metadata(
                _engine_authority_tool_contract(
                    status="partial",
                    terminal_reason="budget_limited",
                    diagnostics=diagnostics,
                ),
            ),
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "partial"
    assert response["terminal_reason"] == "budget_limited"
    assert response["accepted_outputs"]
    assert response["references"]
    assert response["context_budget"]["truncated"] is True
    assert response["context_budget"]["budget"]["budget_policy"] == (
        "selected_source_first_pass"
    )
    assert response["authorization_context"]["authorization_generation"] == (
        "open_webui_selected_source_engine_v1"
    )
    assert response["provenance"]["selected_source_runtime_mode"] == (
        "retrieval_engine_authority"
    )
    assert response["provenance"]["compact_retrieval_provenance"]["budget"] == {
        "requested_budget_tokens": 128,
        "estimated_used_tokens": 32,
        "accepted_output_count": 1,
        "reference_count": 1,
        "candidate_accepted_bundle_count": 1,
        "truncated_accepted_bundle_count": 1,
        "omitted_accepted_bundle_count": 0,
        "truncated": True,
        "policy": "deterministic_complete_evidence_first",
        "budget_policy": "selected_source_first_pass",
        "return_policy": "accepted_evidence_only",
        "ranking_policy": "selected_file_text_cutover",
        "engine_version": "open_webui_selected_source_engine_v1",
        "authorization_generation": "open_webui_selected_source_engine_v1",
        "cache_eligible": True,
        "cache_miss_reason": "",
        "cache_missing_dimensions": [],
        "source_card_count": 1,
        "ordinary_prompt_context_count": 1,
    }
    assert rejected_candidate not in json.dumps(
        response["accepted_outputs"],
        ensure_ascii=False,
    )
    assert rejected_candidate not in json.dumps(
        response["references"],
        ensure_ascii=False,
    )


def test_selected_source_tool_engine_denied_scope_fails_closed(monkeypatch):
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)
    diagnostics = [
        {
            "classification": "diagnostics",
            "reason": "source_scope_violation",
            "outcome": "denied",
            "severity": "error",
        }
    ]

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=_trusted_engine_tool_metadata(
                _engine_authority_tool_contract(
                    status="denied",
                    terminal_reason="source_scope_violation",
                    include_evidence=False,
                    diagnostics=diagnostics,
                ),
            ),
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "denied"
    assert response["canonical_references"] == []
    assert response["references"] == []
    assert response["accepted_outputs"] == []
    assert response["retrieval_diagnostics"][0]["reason"] == "source_scope_violation"
    assert response["retrieval_diagnostics"][0]["outcome"] == "denied"
    assert response["retrieval_diagnostics"][0]["provenance"][
        "selected_source_runtime_mode"
    ] == "retrieval_engine_fail_closed"
    assert response["strategy_used"]["tool_handler_policy_bypassed"] is True
    assert response["provenance"]["tool_handler_bypass_reason"] == (
        "retrieval_engine_fail_closed"
    )
    assert response["provenance"]["selected_source_runtime_mode"] == (
        "retrieval_engine_fail_closed"
    )


def test_selected_source_tool_engine_diagnostic_categories_zero_accepted_lanes(
    monkeypatch,
):
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)
    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"
    candidate_body = "candidate body must not leak into accepted lanes"
    cases = [
        (
            "no_evidence",
            "rejected_candidates_only",
            "candidate_rejected",
            "retrieval_engine_diagnostics",
            "retrieval_engine_diagnostics_only",
        ),
        (
            "no_evidence",
            "unsupported_capability",
            "unsupported_connector_capability",
            "retrieval_engine_diagnostics",
            "retrieval_engine_diagnostics_only",
        ),
        (
            "denied",
            "source_scope_violation",
            "source_scope_violation",
            "retrieval_engine_fail_closed",
            "retrieval_engine_fail_closed",
        ),
        (
            "blocked",
            "active_source_scope_unresolved",
            "active_source_scope_unresolved",
            "retrieval_engine_fail_closed",
            "retrieval_engine_fail_closed",
        ),
        (
            "no_evidence",
            "no_accepted_evidence",
            "no_accepted_evidence",
            "retrieval_engine_diagnostics",
            "retrieval_engine_diagnostics_only",
        ),
        (
            "timeout",
            "retrieval_timeout",
            "retrieval_timeout",
            "retrieval_engine_diagnostics",
            "retrieval_engine_diagnostics_only",
        ),
        (
            "malformed",
            "malformed_provider_output",
            "malformed_provider_output",
            "retrieval_engine_diagnostics",
            "retrieval_engine_diagnostics_only",
        ),
        (
            "partial",
            "parser_loss",
            "parser_loss",
            "retrieval_engine_diagnostics",
            "retrieval_engine_diagnostics_only",
        ),
    ]

    for status, terminal_reason, diagnostic_reason, expected_mode, expected_bypass_reason in cases:
        response = asyncio.run(
            query_selected_knowledge_files(
                query="What does Atlas say?",
                source_ids=["alpha-file"],
                evidence_need="narrow_fact",
                __request__=_selected_source_tool_request_stub(),
                __files__=[],
                __metadata__=_trusted_engine_tool_metadata(
                    _engine_authority_tool_contract(
                        status=status,
                        terminal_reason=terminal_reason,
                        include_evidence=False,
                        diagnostics=[
                            {
                                "classification": (
                                    "no_evidence"
                                    if status in {"no_evidence", "denied", "blocked", "timeout"}
                                    else "diagnostics"
                                ),
                                "reason": diagnostic_reason,
                                "outcome": status,
                                "detail": {
                                    "raw_path": raw_path,
                                    "candidate_body": candidate_body,
                                },
                            }
                        ],
                    ),
                ),
                __user_model__=SimpleNamespace(id="user-1"),
            )
        )

        assert response["status"] == status
        assert response["terminal_reason"] == terminal_reason
        assert response["canonical_references"] == []
        assert response["references"] == []
        assert response["accepted_outputs"] == []
        assert response["context_budget"]["accepted_output_count"] == 0
        assert response["context_budget"]["reference_count"] == 0
        assert response["authorization_context"]["requested_source_ids"] == ["alpha-file"]
        assert response["retry_policy"]["timeout_seconds"] == 25.0
        assert response["strategy_used"]["retrieval_strategy"] == expected_mode
        assert response["provenance"]["selected_source_runtime_mode"] == expected_mode
        assert response["provenance"]["tool_handler_bypass_reason"] == (
            expected_bypass_reason
        )
        assert response["provenance"]["engine_owned"] is True
        assert response["retrieval_engine_plan_summary"]["evidence_shape"] == "narrow_chunk"
        diagnostic = response["retrieval_diagnostics"][0]
        assert diagnostic["reason"] == diagnostic_reason
        assert diagnostic["outcome"] == status
        assert diagnostic["provenance"]["selected_source_runtime_mode"] == expected_mode
        assert diagnostic["provenance"]["engine_owned"] is True
        assert diagnostic["provenance"]["compatibility_fallback"] is False
        assert (
            diagnostic["provenance"]["compact_retrieval_provenance"]["runtime_mode"]
            == expected_mode
        )
        assert "budget" in diagnostic["provenance"]["compact_retrieval_provenance"]
        assert raw_path not in json.dumps(
            diagnostic["provenance"],
            ensure_ascii=False,
        )
        assert raw_path not in json.dumps(diagnostic, ensure_ascii=False)
        assert candidate_body in json.dumps(diagnostic, ensure_ascii=False)
        assert candidate_body not in json.dumps(
            response["accepted_outputs"],
            ensure_ascii=False,
        )
        assert candidate_body not in json.dumps(
            response["canonical_references"],
            ensure_ascii=False,
        )
        assert candidate_body not in json.dumps(
            response["references"],
            ensure_ascii=False,
        )


def test_selected_source_tool_missing_engine_contract_uses_marked_compatibility(
    monkeypatch,
):
    raw_path = "/srv/open-webui/uploads/private/alpha-policy.txt"

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return retrieval_utils.RetrievalSourceList(
            [],
            compatibility_provenance={
                "events": ["hybrid_error_then_non_hybrid_fallback"],
            },
            compatibility_diagnostics=[
                {
                    "kind": "retrieval_quality",
                    "classification": "no_evidence",
                    "reason": "non_hybrid_fallback_no_evidence",
                    "outcome": "no_evidence",
                    "detail": {"raw_path": raw_path},
                }
            ],
        )

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="fallback evidence",
            source_ids=["alpha-file"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(hybrid_enabled=True),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "retrieval_engine_attempt": {
                    "engine_invoked": False,
                    "runtime_mode": "legacy_fallback",
                    "fallback_reason": "adapter_unavailable",
                    "failure_class": "adapter_unavailable",
                },
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                ),
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["references"] == []
    assert response["accepted_outputs"] == []
    assert response["provenance"]["selected_source_runtime_mode"] == (
        "compatibility_fallback"
    )
    assert response["provenance"]["retrieval_runtime_mode"] == "legacy_fallback"
    assert response["provenance"]["fallback_reason"] == "adapter_unavailable"
    assert response["provenance"]["compatibility_fallback"] is True
    assert response["provenance"]["engine_owned"] is False
    diagnostic = response["retrieval_diagnostics"][0]
    assert diagnostic["reason"] == "non_hybrid_fallback_no_evidence"
    assert diagnostic["provenance"]["selected_source_runtime_mode"] == (
        "compatibility_fallback"
    )
    assert diagnostic["provenance"]["compatibility_fallback"] is True
    assert diagnostic["provenance"]["engine_owned"] is False
    assert diagnostic["provenance"].get("engine_authority") is not True
    assert raw_path not in json.dumps(diagnostic["provenance"], ensure_ascii=False)


def test_selected_source_tool_invalid_present_engine_contract_fails_closed(
    monkeypatch,
):
    provider_calls = {"count": 0}

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="Legacy compatibility fallback evidence.",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    untrusted_contract = _engine_authority_tool_contract()
    untrusted_contract["provenance"] = {}
    response = asyncio.run(
        query_selected_knowledge_files(
            query="fallback evidence",
            source_ids=["alpha-file"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "retrieval_engine_first_pass_contract": untrusted_contract,
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                ),
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 0
    assert response["status"] == "error"
    assert response["terminal_reason"] == "invalid_engine_contract"
    assert response["code"] == "invalid_engine_contract"
    assert response["canonical_references"] == []
    assert response["references"] == []
    assert response["accepted_outputs"] == []
    assert response["retrieval_diagnostics"][0]["reason"] == "invalid_engine_contract"
    assert response["strategy_used"]["retrieval_strategy"] == (
        "retrieval_engine_fail_closed"
    )
    assert response["provenance"]["selected_source_runtime_mode"] == (
        "retrieval_engine_fail_closed"
    )
    assert response["provenance"]["retrieval_runtime_mode"] == "engine_owned"
    assert response["provenance"]["engine_owned"] is True
    assert response["provenance"]["compatibility_fallback"] is False


def test_selected_source_tool_preserves_hybrid_fallback_provenance(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return retrieval_utils.RetrievalSourceList(
            [
                _local_file_source(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                    content="Fallback Atlas evidence.",
                )
            ],
            compatibility_provenance={
                "events": [
                    "hybrid_no_evidence_then_non_hybrid_fallback",
                    "non_hybrid_fallback_success",
                ]
            },
        )

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="atlas evidence",
            source_ids=["alpha-file"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(hybrid_enabled=True),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success"
    assert response["provenance"]["compatibility_hybrid_events"] == [
        "hybrid_no_evidence_then_non_hybrid_fallback",
        "non_hybrid_fallback_success",
    ]
    assert (
        response["provenance"]["compact_retrieval_provenance"][
            "compatibility_hybrid_events"
        ]
        == ["hybrid_no_evidence_then_non_hybrid_fallback", "non_hybrid_fallback_success"]
    )
    assert response["accepted_outputs"][0]["snippet"] == "Fallback Atlas evidence."


def test_selected_source_metadata_first_without_targeted_context_fails_closed(
    monkeypatch,
):
    provider_calls = {"count": 0}

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="semantic chunk should not be accepted for metadata-first inventory",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="List recent policy documents about storage safety.",
            source_ids=["alpha-file"],
            evidence_need="metadata_first_inventory",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 0
    assert response["status"] == "no_evidence"
    assert response["code"] == "metadata_first_targeted_evidence_required"
    assert response["canonical_references"] == []
    assert response["references"] == []
    assert response["accepted_outputs"] == []
    assert response["diagnostics"] == response["retrieval_diagnostics"]
    assert response["authorization_context"]["active_source_scope_state"] == "resolved"
    assert response["retry_policy"]["retry_allowed"] is False
    assert response["terminal_reason"] == "metadata_first_targeted_evidence_required"
    assert response["provenance"]["strategy_used"]["retrieval_strategy"] == (
        "metadata_first_then_targeted_chunks"
    )
    assert response["strategy_used"]["evidence_need"] == "metadata_first_inventory"
    assert response["strategy_used"]["retrieval_strategy"] == (
        "metadata_first_then_targeted_chunks"
    )
    assert response["strategy_used"]["semantic_chunk_lookup_ok"] is False
    assert response["retrieval_diagnostics"][0]["reason"] == (
        "metadata_first_targeted_evidence_required"
    )
    assert "semantic chunk should not be accepted" not in json.dumps(
        response,
        ensure_ascii=False,
    )


def test_selected_source_narrow_fact_keeps_semantic_chunk_execution(monkeypatch):
    provider_calls = {"count": 0}

    async def fake_get_sources_from_items(*_args, **kwargs):
        provider_calls["count"] += 1
        assert kwargs.get("queries") == ["what is transformer grounding"]
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="Transformer grounding is a method for aligning model outputs with source text.",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="what is transformer grounding",
            source_ids=["alpha-file"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 1
    assert response["status"] == "success"
    assert response["canonical_references"]
    assert response["references"] == response["canonical_references"]
    assert response["accepted_outputs"]
    assert response["diagnostics"] == []
    assert response["terminal_reason"] == "success"
    assert response["provenance"]["strategy_used"]["retrieval_strategy"] == (
        "semantic_chunks"
    )
    assert response["strategy_used"]["evidence_need"] == "narrow_fact"
    assert response["strategy_used"]["retrieval_strategy"] == "semantic_chunks"
    assert response["strategy_used"]["semantic_chunk_lookup_ok"] is True
    assert response["strategy_used"]["structured_fact_intent"] == "none"
    assert response["strategy_used"]["structured_routing_allowed"] is False
    assert response["strategy_used"]["structured_fact_plan"]["answer_slots"] == []


def test_selected_source_out_of_scope_ids_deny_before_retrieval(monkeypatch):
    provider_calls = {"count": 0}

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return []

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="List recent policy documents about storage safety.",
            source_ids=["beta-file"],
            evidence_need="metadata_first_inventory",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 0
    assert response["status"] == "permission_denied"
    assert response["code"] == "requested_source_out_of_scope"


def test_selected_source_metadata_first_with_bounded_targeted_context_is_scoped(
    monkeypatch,
):
    provider_calls = {"count": 0}

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content=(
                    "policy-2026 2026 Safety Policy title abstract topic relation evidence for storage policy."
                ),
            ),
            _local_file_source(
                file_id="beta-file",
                name="beta-policy.txt",
                content=(
                    "policy-2026 2026 Safety Policy title abstract topic relation evidence for storage policy."
                ),
            ),
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=(
                "document=2026 Safety Policy ; storage policy title abstract relation support"
            ),
            source_ids=["alpha-file"],
            required_anchors=["2026 Safety Policy"],
            evidence_need="metadata_first_inventory",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 1
    assert response["status"] == "success", json.dumps(response, ensure_ascii=False)
    assert response["strategy_used"]["retrieval_strategy"] == (
        "metadata_first_then_targeted_chunks"
    )
    assert response["strategy_used"]["semantic_chunk_lookup_ok"] is True
    assert len(response["canonical_references"]) == 1
    assert response["canonical_references"][0]["source"]["id"] == "alpha-file"
    assert response["accepted_outputs"][0]["snippet"]
    compact = response["provenance"]["compact_retrieval_provenance"]
    assert compact["strategy"]["retrieval_strategy"] == (
        "metadata_first_then_targeted_chunks"
    )
    assert compact["inventory_counts"]["shortlist_count"] == 1
    assert compact["accepted_counts"]["reference_count"] == 1
    assert compact["accepted_counts"]["accepted_output_count"] == 1


def test_first_pass_metadata_first_bounded_context_rejects_off_shortlist_and_weak_chunks(
    monkeypatch,
):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "partial",
        "type": "text",
        "collection_name": "alpha-file",
        "file": {"meta": {"name": "alpha-policy.txt"}},
    }

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="beta-file",
                name="beta-policy.txt",
                content="2026 Safety Policy explicitly discusses storage safety controls.",
            ),
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="generic semantic drift paragraph without required anchor support",
            ),
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    body = {
        "model": "test-model",
        "messages": [
            {
                "role": "user",
                "content": "请列出这个选定来源里和储能安全相关的政策材料。",
            }
        ],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
            "selected_source_next_targeted_chunk_request": {
                "source_ids": ["alpha-file"],
                "required_anchors": ["2026 Safety Policy"],
                "query": "document=2026 Safety Policy; topic_terms=storage safety",
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted_metadata = _build_assistant_reference_persistence_metadata(flags)

    assert flags["references"] == []
    assert flags["accepted_outputs"] == []
    assert flags["status"] == "no_evidence"
    reasons = {
        str(item.get("reason") or "")
        for item in flags["retrieval_diagnostics"]
        if isinstance(item, dict)
    }
    assert "off_shortlist_chunk" in reasons
    assert "weak_targeted_chunk_evidence" in reasons
    assert "canonical_references" not in persisted_metadata
    assert "reference_cards" not in persisted_metadata


def test_selected_source_metadata_first_tool_path_rejects_scoped_unsupported_chunks(
    monkeypatch,
):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="Storage Safety Notice includes storage safety evidence.",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=(
                "document=Storage Safety Notice; topic_terms=storage safety; "
                "list latest related policy updates"
            ),
            source_ids=["alpha-file"],
            required_anchors=["Storage Safety Notice"],
            evidence_need="metadata_first_inventory",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["code"] == "unsupported_latest_or_recent_claim"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    reasons = {
        str(item.get("reason") or "")
        for item in response["retrieval_diagnostics"]
        if isinstance(item, dict)
    }
    assert "missing_date_metadata" in reasons
    assert "unsupported_latest_or_recent_claim" in reasons


def test_structured_fact_standard_value_requires_direct_value_anchor(monkeypatch):
    provider_payloads = [
        [
            _local_file_source(
                file_id="alpha-file",
                name="sl190-table.txt",
                content=(
                    "SL190-2007 水土保持标准映射：北方土石山区允许土壤流失量"
                    "为 200t/(km2·a)。"
                ),
            )
        ],
        [
            _local_file_source(
                file_id="alpha-file",
                name="sl190-table.txt",
                content=(
                    "SL190-2007 水土保持标准映射：北方土石山区属于一级分区，"
                    "该段未给出允许土壤流失量数值。"
                ),
            )
        ],
    ]

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return provider_payloads.pop(0)

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    base_kwargs = {
        "query": "SL190-2007 标准里北方土石山区允许土壤流失量是多少？",
        "source_ids": ["alpha-file"],
        "required_anchors": ["北方土石山区", "200t/(km2·a)"],
        "evidence_need": "narrow_fact",
        "__request__": _selected_source_tool_request_stub(),
        "__files__": [_selected_source_file_candidate("alpha-file", "sl190-table.txt")],
        "__metadata__": {
            "active_source_scope": _resolved_active_source_scope(
                file_id="alpha-file",
                name="sl190-table.txt",
            )
        },
        "__user_model__": SimpleNamespace(id="user-1"),
    }

    supported = asyncio.run(query_selected_knowledge_files(**base_kwargs))
    assert supported["status"] == "success"
    assert supported["terminal_reason"] == "success"
    assert supported["canonical_references"]
    assert supported["accepted_outputs"]
    plan = supported["strategy_used"]["structured_fact_plan"]
    assert supported["strategy_used"]["structured_fact_intent"] == "standard_value"
    assert supported["strategy_used"]["structured_routing_allowed"] is True
    assert supported["strategy_used"]["structured_query_variants"]
    assert supported["strategy_used"]["structured_rejected_query_variants"]
    assert "structured_engineering_fact" in plan["intent_labels"]
    assert "standard_value" in plan["intent_labels"]
    assert "SL190-2007" in plan["anchor_groups"]["standard_numbers"]
    assert "200t/(km2·a)" in plan["anchor_groups"]["units"]
    assert "北方土石山区" in plan["hard_anchors"]
    assert "200t/(km2·a)" in plan["hard_anchors"]
    supported_doc = " ".join(supported["canonical_references"][0]["document"])
    assert "北方土石山区" in supported_doc
    assert "200t/(km2·a)" in supported_doc
    exact_candidates = supported["strategy_used"].get("exact_anchor_candidates") or []
    assert any(item.get("anchor") == "SL190-2007" for item in exact_candidates)

    missing_value = asyncio.run(query_selected_knowledge_files(**base_kwargs))
    assert missing_value["status"] == "no_evidence"
    assert missing_value["terminal_reason"] in {
        "structured_precision_guard_no_supported_fields",
        "structured_precision_guard_incomplete_fields",
    }
    assert missing_value["canonical_references"] == []
    assert missing_value["accepted_outputs"] == []
    reasons = _diagnostic_reason_set(missing_value)
    assert "missing_standard_mapping" in reasons or "missing_unit" in reasons
    assert (
        "structured_precision_guard_no_supported_fields" in reasons
        or "structured_precision_guard_incomplete_fields" in reasons
    )


def test_structured_fact_quota_08063_direct_row_supports_requested_fields(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="quota-08063.txt",
                content=(
                    "定额 08063 行：labor=12工日；farmyard manure=3m3；"
                    "other material cost=46元；拖拉机37w=0.75台班。"
                ),
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=(
                "请从定额08063提取 labor、farmyard manure、"
                "other material cost 和 拖拉机37w 的取值。"
            ),
            source_ids=["alpha-file"],
            required_anchors=[
                "08063",
                "labor",
                "farmyard manure",
                "other material cost",
                "拖拉机37w",
            ],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-08063.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-08063.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success", json.dumps(response, ensure_ascii=False)
    assert response["terminal_reason"] == "success"
    assert response["canonical_references"]
    assert response["accepted_outputs"]
    plan = response["strategy_used"]["structured_fact_plan"]
    assert response["strategy_used"]["structured_fact_intent"] == (
        "multi_field_slot_extraction"
    )
    assert response["strategy_used"]["structured_routing_allowed"] is True
    assert "08063" in plan["anchor_groups"]["quota_ids"]
    assert plan["source_scope"]["requested_source_ids"] == ["alpha-file"]
    expected_slots = ["labor", "farmyard manure", "other material cost", "拖拉机37w"]
    assert plan["answer_slots"] == expected_slots
    assert "other material cost 和 拖拉机37w 的取值" not in plan["answer_slots"]
    for slot in expected_slots:
        assert slot in plan["hard_anchors"]
    structured_fields = response["structured_fact_fields"]
    assert len(structured_fields) == 4
    assert {
        str(field.get("field_name")): str(field.get("support_state"))
        for field in structured_fields
        if isinstance(field, dict)
    } == {
        "labor": "supported",
        "farmyard manure": "supported",
        "other material cost": "supported",
        "拖拉机37w": "supported",
    }
    assert all(field.get("source_anchor_kind") == "compatibility_text" for field in structured_fields)
    assert all(
        field.get("source_anchor_note") == "compatibility_only_anchor_text"
        for field in structured_fields
    )
    structured_outputs = [
        item
        for item in response["accepted_outputs"]
        if isinstance(item, dict) and item.get("type") == "structured_fact_field"
    ]
    assert len(structured_outputs) == 4
    assert all(str(item.get("support_state")) == "supported" for item in structured_outputs)
    assert all(item.get("strategy_name") == "multi_field_slot_extraction" for item in structured_outputs)
    assert all(item.get("source_anchor_kind") == "compatibility_text" for item in structured_outputs)
    assert all(
        (field.get("evidence_bundle") or {}).get("binding") == "direct_field_value"
        for field in structured_fields
    )
    exact_candidates = response["strategy_used"].get("exact_anchor_candidates") or []
    assert any(item.get("anchor") == "08063" for item in exact_candidates)
    assert plan["focused_queries"]
    assert all(
        bool(item.get("answer_slot")) for item in plan["focused_queries"] if isinstance(item, dict)
    )
    supported_doc = " ".join(response["canonical_references"][0]["document"])
    accepted_snippets = " ".join(
        str(item.get("snippet") or "")
        for item in response["accepted_outputs"]
        if isinstance(item, dict)
    )
    for pair in (
        "labor=12工日",
        "farmyard manure=3m3",
        "other material cost=46元",
        "拖拉机37w=0.75台班",
    ):
        assert pair in supported_doc
        assert pair in accepted_snippets
    assert not any(
        reason.startswith("unsupported_") for reason in _diagnostic_reason_set(response)
    )


def test_structured_fact_quota_08063_split_table_headers_bind_fields(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "quota-08063-markdown.txt",
                    "url": "/api/v1/files/alpha-file/content",
                    "type": "file",
                },
                "document": [
                    "表 4-1 定额消耗量",
                    "| 定额 | labor | farmyard manure | other material cost | 拖拉机37w |",
                    "| 单位 | 工日 | m3 | 元 | 台班 |",
                    "| 08063 | 12 | 3 | 46 | 0.75 |",
                ],
                "metadata": [
                    {
                        "source": "alpha-file",
                        "name": "quota-08063-markdown.txt",
                        "file_id": "alpha-file",
                        "table_title": "表 4-1 定额消耗量",
                        "page": 21,
                    },
                    {
                        "source": "alpha-file",
                        "name": "quota-08063-markdown.txt",
                        "file_id": "alpha-file",
                        "table_title": "表 4-1 定额消耗量",
                        "page": 21,
                    },
                    {
                        "source": "alpha-file",
                        "name": "quota-08063-markdown.txt",
                        "file_id": "alpha-file",
                        "table_title": "表 4-1 定额消耗量",
                        "page": 21,
                    },
                    {
                        "source": "alpha-file",
                        "name": "quota-08063-markdown.txt",
                        "file_id": "alpha-file",
                        "table_title": "表 4-1 定额消耗量",
                        "page": 21,
                    },
                ],
            }
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=(
                "请从定额08063提取 labor、farmyard manure、"
                "other material cost 和 拖拉机37w 的取值。"
            ),
            source_ids=["alpha-file"],
            required_anchors=[
                "08063",
                "labor",
                "farmyard manure",
                "other material cost",
                "拖拉机37w",
            ],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[
                _selected_source_file_candidate("alpha-file", "quota-08063-markdown.txt")
            ],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-08063-markdown.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success", json.dumps(response, ensure_ascii=False)
    fields = {
        str(field.get("field_name")): field
        for field in response["structured_fact_fields"]
        if isinstance(field, dict)
    }
    assert {name: field.get("value") for name, field in fields.items()} == {
        "labor": "12工日",
        "farmyard manure": "3m3",
        "other material cost": "46元",
        "拖拉机37w": "0.75台班",
    }
    assert all(field.get("support_state") == "supported" for field in fields.values())
    assert all(
        (field.get("evidence_bundle") or {}).get("binding") == "table_row_header_unit"
        for field in fields.values()
    )
    assert response["canonical_references"]
    docs = " ".join(response["canonical_references"][0]["document"])
    assert "labor" in docs
    assert "单位" in docs
    assert "08063" in docs


def test_structured_fact_quota_08063_adjacent_unit_row_binds_values(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "quota-08063-units.txt",
                    "url": "/api/v1/files/alpha-file/content",
                    "type": "file",
                },
                "document": [
                    "| 定额 | labor | farmyard manure | other material cost | 拖拉机37w |",
                    "| 单位 | 工日 | m3 | 元 | 台班 |",
                    "| 08063 | 12 | 3 | 46 | 0.75 |",
                ],
                "metadata": [
                    {"source": "alpha-file", "name": "quota-08063-units.txt", "file_id": "alpha-file"},
                    {"source": "alpha-file", "name": "quota-08063-units.txt", "file_id": "alpha-file"},
                    {"source": "alpha-file", "name": "quota-08063-units.txt", "file_id": "alpha-file"},
                ],
            }
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="请从定额08063提取 labor 和 farmyard manure 的取值。",
            source_ids=["alpha-file"],
            required_anchors=["08063", "labor", "farmyard manure"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-08063-units.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-08063-units.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success", json.dumps(response, ensure_ascii=False)
    values = {
        str(field.get("field_name")): str(field.get("value") or "")
        for field in response["structured_fact_fields"]
        if isinstance(field, dict)
    }
    assert values == {"labor": "12工日", "farmyard manure": "3m3"}


def test_structured_fact_quota_08063_missing_headers_are_unsupported(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "quota-08063-no-header.txt",
                    "url": "/api/v1/files/alpha-file/content",
                    "type": "file",
                },
                "document": [
                    "表 4-1 定额消耗量",
                    "| 单位 | 工日 | m3 | 元 | 台班 |",
                    "| 08063 | 12 | 3 | 46 | 0.75 |",
                ],
                "metadata": [
                    {"source": "alpha-file", "name": "quota-08063-no-header.txt", "file_id": "alpha-file"},
                    {"source": "alpha-file", "name": "quota-08063-no-header.txt", "file_id": "alpha-file"},
                    {"source": "alpha-file", "name": "quota-08063-no-header.txt", "file_id": "alpha-file"},
                ],
            }
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="请从定额08063提取 labor 和 farmyard manure 的取值。",
            source_ids=["alpha-file"],
            required_anchors=["08063", "labor", "farmyard manure"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-08063-no-header.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-08063-no-header.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    reasons = _diagnostic_reason_set(response)
    assert "missing_table_header" in reasons
    assert "structured_precision_guard_no_supported_fields" in reasons
    assert all(
        field.get("support_state") == "unsupported"
        for field in response.get("structured_fact_fields", [])
        if isinstance(field, dict)
    )


def test_structured_fact_quota_08063_row_only_parser_loss_is_unsupported(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="quota-08063-row-only.txt",
                content="08063 12 3 46 0.75",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="quota_id=08063; answer_fields=labor,farmyard manure,other material cost,拖拉机37w",
            source_ids=["alpha-file"],
            required_anchors=[
                "08063",
                "labor",
                "farmyard manure",
                "other material cost",
                "拖拉机37w",
            ],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-08063-row-only.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-08063-row-only.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    reasons = _diagnostic_reason_set(response)
    assert "missing_table_header" in reasons
    assert (
        "structured_precision_guard_no_supported_fields" in reasons
        or "structured_precision_guard_incomplete_fields" in reasons
    )
    assert "12" not in json.dumps(response["accepted_outputs"], ensure_ascii=False)


def test_structured_fact_standard_value_split_neighbor_chunks_bundle_support(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "sl190-split.txt",
                    "url": "/api/v1/files/alpha-file/content",
                    "type": "file",
                },
                    "document": [
                        "SL190-2007 标准映射：本项目位于北方土石山区，属于一级分区。",
                        "200t/(km2·a)",
                    ],
                "metadata": [
                    {
                        "source": "alpha-file",
                        "name": "sl190-split.txt",
                        "file_id": "alpha-file",
                        "heading": "附录A 分区说明",
                        "page": 12,
                    },
                    {
                        "source": "alpha-file",
                        "name": "sl190-split.txt",
                        "file_id": "alpha-file",
                        "heading": "附录A 分区说明",
                        "page": 13,
                    },
                ],
            }
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="SL190-2007 标准中北方土石山区允许土壤流失量是否为 200t/(km2·a)？",
            source_ids=["alpha-file"],
            required_anchors=["SL190-2007", "北方土石山区", "200t/(km2·a)"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "sl190-split.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="sl190-split.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success"
    assert response["terminal_reason"] == "success"
    assert response["canonical_references"]
    supported_docs = " ".join(response["canonical_references"][0]["document"])
    assert "SL190-2007" in supported_docs
    assert "200t/(km2·a)" in supported_docs
    fields = response["structured_fact_fields"]
    assert len(fields) == 1
    assert fields[0]["field_name"] == "standard_value"
    assert fields[0]["support_state"] == "supported"
    assert fields[0]["value"] == "200t/(km2·a)"
    bundle = fields[0].get("evidence_bundle") or {}
    assert bundle.get("standard_anchor_hits", 0) >= 1
    assert bundle.get("classification_hits", 0) >= 1
    assert bundle.get("value_hits", 0) >= 1


def test_structured_fact_standard_value_rejects_unrelated_same_file_value_anchor(
    monkeypatch,
):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "sl190-unrelated-value.txt",
                    "url": "/api/v1/files/alpha-file/content",
                    "type": "file",
                },
                "document": [
                    "SL190-2007 标准映射：本项目位于北方土石山区，属于一级分区。",
                    "本段仅说明治理范围和监测点位，未给出允许土壤流失量。",
                    "施工运输章节示例：车辆荷载控制值为 200t/(km2·a)，与分区标准表无关。",
                ],
                "metadata": [
                    {
                        "source": "alpha-file",
                        "name": "sl190-unrelated-value.txt",
                        "file_id": "alpha-file",
                        "heading": "附录A 分区说明",
                        "page": 12,
                    },
                    {
                        "source": "alpha-file",
                        "name": "sl190-unrelated-value.txt",
                        "file_id": "alpha-file",
                        "heading": "附录A 分区说明",
                        "page": 13,
                    },
                    {
                        "source": "alpha-file",
                        "name": "sl190-unrelated-value.txt",
                        "file_id": "alpha-file",
                        "heading": "施工运输参数",
                        "page": 88,
                    },
                ],
            }
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="SL190-2007 标准中北方土石山区允许土壤流失量是否为 200t/(km2·a)？",
            source_ids=["alpha-file"],
            required_anchors=["SL190-2007", "北方土石山区", "200t/(km2·a)"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[
                _selected_source_file_candidate(
                    "alpha-file",
                    "sl190-unrelated-value.txt",
                )
            ],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="sl190-unrelated-value.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["references"] == []
    assert response["accepted_outputs"] == []
    reasons = _diagnostic_reason_set(response)
    assert "missing_standard_mapping" in reasons or "missing_unit" in reasons
    assert (
        "structured_precision_guard_no_supported_fields" in reasons
        or "structured_precision_guard_incomplete_fields" in reasons
    )
    fields = response.get("structured_fact_fields", [])
    assert fields
    assert fields[0]["support_state"] == "unsupported"
    assert fields[0]["value"] == ""


def test_structured_fact_standard_value_conflicting_values_are_not_verified(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "sl190-conflict.txt",
                    "url": "/api/v1/files/alpha-file/content",
                    "type": "file",
                },
                "document": [
                    "SL190-2007 规定：北方土石山区允许土壤流失量为 200t/(km2·a)。",
                    "SL190-2007 另一处表述：北方土石山区允许土壤流失量为 300t/(km2·a)。",
                ],
                "metadata": [
                    {"source": "alpha-file", "name": "sl190-conflict.txt", "file_id": "alpha-file"},
                    {"source": "alpha-file", "name": "sl190-conflict.txt", "file_id": "alpha-file"},
                ],
            }
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
                query=(
                    "SL190-2007 下北方土石山区允许土壤流失量是否出现 "
                    "200t/(km2·a) 与 300t/(km2·a) 两个值？"
                ),
                source_ids=["alpha-file"],
                required_anchors=["SL190-2007", "北方土石山区", "200t/(km2·a)"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "sl190-conflict.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="sl190-conflict.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    reasons = _diagnostic_reason_set(response)
    assert "conflicting_value" in reasons
    assert (
        "structured_precision_guard_no_supported_fields" in reasons
        or "structured_precision_guard_incomplete_fields" in reasons
    )
    fields = response.get("structured_fact_fields", [])
    assert fields
    assert fields[0]["support_state"] == "conflicting"
    assert fields[0]["limitation_reason"] == "conflicting_value"


def test_structured_fact_standard_value_budget_truncation_keeps_no_evidence(monkeypatch):
    documents = [
        f"SL190-2007 北方土石山区标准映射说明，第{i}段仅包含分类上下文。"
        for i in range(1, 11)
    ]
    documents.append(
        "SL190-2007 北方土石山区允许土壤流失量为 200t/(km2·a)。"
    )
    metadata = [
        {
            "source": "alpha-file",
            "name": "sl190-budget.txt",
            "file_id": "alpha-file",
            "page": index + 1,
        }
        for index in range(len(documents))
    ]

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            {
                "source": {
                    "id": "alpha-file",
                    "name": "sl190-budget.txt",
                    "url": "/api/v1/files/alpha-file/content",
                    "type": "file",
                },
                "document": documents,
                "metadata": metadata,
            }
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
                query="请核对 SL190-2007 中北方土石山区允许土壤流失量是否为 200t/(km2·a)。",
                source_ids=["alpha-file"],
                required_anchors=["SL190-2007", "北方土石山区", "200t/(km2·a)"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "sl190-budget.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="sl190-budget.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    assert response["context_budget"]["truncated"] is True
    reasons = _diagnostic_reason_set(response)
    assert "budget_truncation" in reasons
    fields = response.get("structured_fact_fields", [])
    assert fields
    assert fields[0]["support_state"] == "truncated"
    assert fields[0]["limitation_reason"] == "budget_truncation"


def test_structured_fact_standard_value_requires_standard_anchor_match(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
                _local_file_source(
                    file_id="alpha-file",
                    name="sl190-no-anchor.txt",
                    content="该标准条目说明北方土石山区允许土壤流失量是否为 200t/(km2·a)，但文本未出现标准编号。",
                )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
                query="SL190-2007 北方土石山区允许土壤流失量是否为 200t/(km2·a)？",
                source_ids=["alpha-file"],
                required_anchors=["北方土石山区", "200t/(km2·a)"],
                evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "sl190-no-anchor.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="sl190-no-anchor.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    reasons = _diagnostic_reason_set(response)
    assert "no_anchor_match" in reasons


def test_structured_fact_unit_only_measurement_prompt_stays_semantic(monkeypatch):
    provider_calls = {"count": 0}
    query = "200t/(km2·a) 换算成 kg/m2 约等于多少？"

    async def fake_get_sources_from_items(*_args, **kwargs):
        provider_calls["count"] += 1
        assert kwargs.get("queries") == [query]
        return [
            _local_file_source(
                file_id="alpha-file",
                name="unit-conversion.txt",
                content="200t/(km2·a) 换算成 kg/m2 约等于 0.2kg/m2。",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=query,
            source_ids=["alpha-file"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "unit-conversion.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="unit-conversion.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 1
    assert response["status"] == "success"
    assert response["strategy_used"]["retrieval_strategy"] == "semantic_chunks"
    assert response["strategy_used"]["structured_fact_intent"] == "none"
    assert response["strategy_used"]["structured_routing_allowed"] is False
    plan = response["strategy_used"]["structured_fact_plan"]
    assert "200t/(km2·a)" in plan["anchor_groups"]["units"]
    assert plan["has_exact_anchor_signal"] is False
    assert plan["has_multi_field_signal"] is False
    assert plan["intent_labels"] == []


def test_structured_fact_precision_guard_blocks_unrelated_table_row_values(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="quota-mixed-rows.txt",
                content=(
                    "定额08063 行：labor=12工日；farmyard manure=；"
                    "other material cost=；拖拉机37w=。"
                    "定额08064 行：labor=10工日；farmyard manure=3m3；"
                    "other material cost=46元；拖拉机37w=0.75台班。"
                ),
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=(
                "请从定额08063提取 labor、farmyard manure、"
                "other material cost 和 拖拉机37w 的取值。"
            ),
            source_ids=["alpha-file"],
            required_anchors=[
                "08063",
                "labor",
                "farmyard manure",
                "other material cost",
                "拖拉机37w",
            ],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-mixed-rows.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-mixed-rows.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["terminal_reason"] in {
        "structured_precision_guard_no_supported_fields",
        "structured_precision_guard_incomplete_fields",
    }
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    assert response["structured_fact_fields"]
    states = {
        str(field.get("field_name")): str(field.get("support_state"))
        for field in response["structured_fact_fields"]
        if isinstance(field, dict)
    }
    assert states
    assert any(state in {"unsupported", "conflicting", "truncated"} for state in states.values())
    reasons = _diagnostic_reason_set(response)
    assert (
        "missing_table_header" in reasons
        or "missing_field_mapping" in reasons
        or "conflicting_field_values" in reasons
    )
    assert (
        "structured_precision_guard_no_supported_fields" in reasons
        or "structured_precision_guard_incomplete_fields" in reasons
    )
    assert "exact_anchor_lookup_unavailable" not in reasons
    assert "08064" not in json.dumps(response, ensure_ascii=False)


def test_structured_fact_precision_guard_disabled_mode_keeps_semantic_baseline(
    monkeypatch,
):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="quota-mixed-rows.txt",
                content=(
                    "定额08063 行：labor=12工日；farmyard manure=；"
                    "other material cost=；拖拉机37w=。"
                    "定额08064 行：labor=10工日；farmyard manure=3m3；"
                    "other material cost=46元；拖拉机37w=0.75台班。"
                ),
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=(
                "请从定额08063提取 labor、farmyard manure、"
                "other material cost 和 拖拉机37w 的取值。"
            ),
            source_ids=["alpha-file"],
            required_anchors=[
                "08063",
                "labor",
                "farmyard manure",
                "other material cost",
                "拖拉机37w",
            ],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-mixed-rows.txt")],
            __metadata__={
                "structured_fact_precision_mode": "disabled",
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-mixed-rows.txt",
                ),
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    assert response["terminal_reason"] == "structured_precision_guard_disabled_context_only"
    assert response["structured_fact_fields"]
    states = {
        str(field.get("field_name")): str(field.get("support_state"))
        for field in response["structured_fact_fields"]
        if isinstance(field, dict)
    }
    assert states
    assert any(state in {"unsupported", "conflicting", "truncated"} for state in states.values())
    reasons = _diagnostic_reason_set(response)
    assert "structured_precision_guard_disabled_context_only" in reasons
    assert response["strategy_used"]["structured_fact_intent"] == (
        "multi_field_slot_extraction"
    )


def test_read_selected_file_disabled_precision_mode_blocks_structured_references(
    monkeypatch,
):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="quota-mixed-rows.txt",
                content=(
                    "定额08063 行：labor=12工日；farmyard manure=；"
                    "other material cost=；拖拉机37w=。"
                    "定额08064 行：labor=10工日；farmyard manure=3m3；"
                    "other material cost=46元；拖拉机37w=0.75台班。"
                ),
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        read_selected_file(
            source_id="alpha-file",
            query=(
                "请从定额08063提取 labor、farmyard manure、"
                "other material cost 和 拖拉机37w 的取值。"
            ),
            required_anchors=[
                "08063",
                "labor",
                "farmyard manure",
                "other material cost",
                "拖拉机37w",
            ],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-mixed-rows.txt")],
            __metadata__={
                "structured_fact_precision_mode": "disabled",
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-mixed-rows.txt",
                ),
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    assert response["terminal_reason"] == "structured_precision_guard_disabled_context_only"
    assert response["structured_fact_fields"]
    reasons = _diagnostic_reason_set(response)
    assert "structured_precision_guard_disabled_context_only" in reasons


def test_structured_fact_partial_fields_block_in_enabled_mode(monkeypatch):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="quota-partial-08063.txt",
                content="定额08063 行：labor=12工日；farmyard manure=。",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="请从定额08063提取 labor 和 farmyard manure 的取值。",
            source_ids=["alpha-file"],
            required_anchors=["08063", "labor", "farmyard manure"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-partial-08063.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-partial-08063.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    assert response["terminal_reason"] == "structured_precision_guard_incomplete_fields"
    states = {
        str(field.get("field_name")): str(field.get("support_state"))
        for field in response.get("structured_fact_fields", [])
        if isinstance(field, dict)
    }
    assert states.get("labor") == "supported"
    assert states.get("farmyard manure") in {"unsupported", "truncated"}
    exact_candidates = response["strategy_used"].get("exact_anchor_candidates") or []
    assert exact_candidates
    assert any(item.get("anchor") == "08063" for item in exact_candidates)


def test_read_selected_file_structured_fact_partial_fields_block_in_enabled_mode(
    monkeypatch,
):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="quota-partial-08063.txt",
                content="定额08063 行：labor=12工日；farmyard manure=。",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        read_selected_file(
            source_id="alpha-file",
            query="请从定额08063提取 labor 和 farmyard manure 的取值。",
            required_anchors=["08063", "labor", "farmyard manure"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-partial-08063.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-partial-08063.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    assert response["terminal_reason"] == "structured_precision_guard_incomplete_fields"
    reasons = _diagnostic_reason_set(response)
    assert "structured_precision_guard_incomplete_fields" in reasons


def test_structured_fact_negative_weak_or_anchor_incomplete_evidence_is_not_accepted(
    monkeypatch,
):
    cases = [
        {
            "query": "SL190-2007 北方土石山区允许土壤流失量数值与单位。",
            "required_anchors": ["SL190-2007", "北方土石山区", "200t/(km2·a)"],
            "content": (
                "NEG_STANDARD_MAPPING_ONLY SL190-2007 仅说明北方土石山区分区名称，"
                "未提供允许土壤流失量对应数值。"
            ),
            "marker": "NEG_STANDARD_MAPPING_ONLY",
        },
        {
            "query": "quota_id=08063; evidence_facets=labor,other material cost,拖拉机37w",
            "required_anchors": ["08063", "labor", "other material cost", "拖拉机37w"],
            "content": (
                "NEG_TABLE_HEADER_MISSING 08063 12 46 0.75 仅有裸行数值，"
                "无字段表头可绑定。"
            ),
            "marker": "NEG_TABLE_HEADER_MISSING",
        },
        {
            "query": "quota_id=08063; evidence_facets=farmyard manure unit row",
            "required_anchors": ["08063", "farmyard manure", "m3"],
            "content": (
                "NEG_UNIT_ROW_MISSING 定额08063 包含 farmyard manure=3 ，"
                "但文段缺失单位行。"
            ),
            "marker": "NEG_UNIT_ROW_MISSING",
        },
        {
            "query": "quota_id=08063; evidence_facets=labor,other material cost field mapping",
            "required_anchors": ["08063", "labor", "other material cost", "field mapping"],
            "content": (
                "NEG_FIELD_MAPPING_MISSING 08063 labor 12 与 cost 46 出现，"
                "但没有字段到列位映射说明。"
            ),
            "marker": "NEG_FIELD_MAPPING_MISSING",
        },
    ]

    provider_payloads = [
        [
            _local_file_source(
                file_id="alpha-file",
                name="quota-negative.txt",
                content=case["content"],
            )
        ]
        for case in cases
    ]

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return provider_payloads.pop(0)

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    for case in cases:
        response = asyncio.run(
            query_selected_knowledge_files(
                query=case["query"],
                source_ids=["alpha-file"],
                required_anchors=case["required_anchors"],
                evidence_need="narrow_fact",
                __request__=_selected_source_tool_request_stub(),
                __files__=[
                    _selected_source_file_candidate("alpha-file", "quota-negative.txt")
                ],
                __metadata__={
                    "active_source_scope": _resolved_active_source_scope(
                        file_id="alpha-file",
                        name="quota-negative.txt",
                    )
                },
                __user_model__=SimpleNamespace(id="user-1"),
            )
        )

        assert response["status"] == "no_evidence"
        assert response["canonical_references"] == []
        assert response["accepted_outputs"] == []
        reasons = _diagnostic_reason_set(response)
        assert (
            "off_anchor_evidence" in reasons
            or "off_facet_evidence" in reasons
            or "weak_targeted_chunk_evidence" in reasons
            or "weak_or_indirect_evidence" in reasons
            or "no_injectable_evidence" in reasons
            or "structured_precision_guard_no_supported_fields" in reasons
            or "structured_precision_guard_incomplete_fields" in reasons
        )
        assert (
            "no_injectable_evidence" in reasons
            or "structured_precision_guard_no_supported_fields" in reasons
            or "structured_precision_guard_incomplete_fields" in reasons
        )
        assert response.get("structured_fact_fields", []) == [] or all(
            str(field.get("support_state") or "") != "supported"
            for field in response.get("structured_fact_fields", [])
            if isinstance(field, dict)
        )
        assert case["marker"] not in json.dumps(response, ensure_ascii=False)


def test_structured_fact_long_incidental_context_uses_anchor_preserving_variants(
    monkeypatch,
):
    provider_calls = {"count": 0}
    long_query = (
        "project_id=HB-2026-05; contractor=示例单位A; supervisor=示例单位B; "
        "项目涉及多个地市、多个施工标段、多个管理变量，这些信息仅作背景上下文。"
        "请基于 SL190-2007 说明北方土石山区允许土壤流失量，并核对"
        " 200t/(km2·a) 是否有直接证据。"
    )

    async def fake_get_sources_from_items(*_args, **kwargs):
        provider_calls["count"] += 1
        queries = kwargs.get("queries") or []
        assert long_query in queries
        assert any("SL190-2007" in value for value in queries)
        assert any("北方土石山区" in value for value in queries)
        assert any("200t/(km2·a)" in value for value in queries)
        return [
            _local_file_source(
                file_id="alpha-file",
                name="sl190-focused.txt",
                content=(
                    "SL190-2007 明确：北方土石山区允许土壤流失量为 200t/(km2·a)。"
                ),
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=long_query,
            source_ids=["alpha-file"],
            required_anchors=["SL190-2007", "北方土石山区", "200t/(km2·a)"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "sl190-focused.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="sl190-focused.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 1
    assert response["query"] == long_query
    assert response["status"] == "success"
    assert response["terminal_reason"] == "success"
    assert response["canonical_references"]
    assert response["accepted_outputs"]
    plan = response["strategy_used"]["structured_fact_plan"]
    assert response["strategy_used"]["structured_fact_intent"] == "standard_value"
    assert response["strategy_used"]["structured_routing_allowed"] is True
    assert response["strategy_used"]["structured_rejected_query_variants"]
    assert response["strategy_used"]["structured_query_variants"]
    assert response["strategy_used"].get("exact_anchor_candidates")
    assert "project_id=HB-2026-05" in plan["soft_context_terms"]
    assert "contractor=示例单位A" in plan["soft_context_terms"]
    assert plan["focused_queries"]
    assert all(
        "project_id=" not in str(item.get("query") or "")
        for item in plan["focused_queries"]
        if isinstance(item, dict)
    )
    reasons = _diagnostic_reason_set(response)
    assert "exact_anchor_lookup_unavailable" not in reasons


def test_structured_fact_false_positive_anchor_mention_stays_semantic(monkeypatch):
    provider_calls = {"count": 0}
    query = "08063路公交首班车时间是什么？"

    async def fake_get_sources_from_items(*_args, **kwargs):
        provider_calls["count"] += 1
        assert kwargs.get("queries") == [query]
        return [
            _local_file_source(
                file_id="alpha-file",
                name="general-note.txt",
                content="08063路公交首班车时间是 06:30。",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=query,
            source_ids=["alpha-file"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "general-note.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="general-note.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 1
    assert response["status"] == "success"
    assert response["strategy_used"]["retrieval_strategy"] == "semantic_chunks"
    assert response["strategy_used"]["structured_fact_intent"] == "none"
    assert response["strategy_used"]["structured_routing_allowed"] is False
    plan = response["strategy_used"]["structured_fact_plan"]
    assert "08063" in plan["hard_anchors"]
    assert plan["has_exact_anchor_signal"] is False
    assert plan["intent_labels"] == []
    assert response["strategy_used"].get("exact_anchor_candidates") in (None, [])
    assert "exact_anchor_lookup_unavailable" not in _diagnostic_reason_set(response)


def test_structured_fact_plan_extracts_document_and_table_anchors(monkeypatch):
    query = (
        "请根据文号：国能发〔2024〕12号，在表3.2中核对“北方土石山区”"
        "对应允许土壤流失量 200t/(km2·a)。"
    )

    async def fake_get_sources_from_items(*_args, **kwargs):
        queries = kwargs.get("queries") or []
        assert query in queries
        assert all("国能发〔2024〕12号" in value for value in queries)
        assert all("表3.2" in value for value in queries)
        return [
            _local_file_source(
                file_id="alpha-file",
                name="doc-anchor.txt",
                content=(
                    "国能发〔2024〕12号 表3.2 说明：北方土石山区允许土壤流失量"
                    " 200t/(km2·a)。"
                ),
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=query,
            source_ids=["alpha-file"],
            required_anchors=["国能发〔2024〕12号", "表3.2", "北方土石山区", "200t/(km2·a)"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "doc-anchor.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="doc-anchor.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success"
    plan = response["strategy_used"]["structured_fact_plan"]
    assert "国能发〔2024〕12号" in plan["anchor_groups"]["document_numbers"]
    assert "表3.2" in plan["anchor_groups"]["row_or_table_labels"]
    assert "北方土石山区" in plan["anchor_groups"]["quoted_terms"]
    assert "200t/(km2·a)" in plan["anchor_groups"]["units"]
    exact_candidates = response["strategy_used"].get("exact_anchor_candidates") or []
    assert any(item.get("anchor") == "国能发〔2024〕12号" for item in exact_candidates)


def test_structured_fact_exact_lookup_normalizes_punctuation_and_width(monkeypatch):
    query = "SL190-2007 中“北方土石山区”的允许土壤流失量是否为 200t/(km2·a)？"

    async def fake_get_sources_from_items(*_args, **kwargs):
        queries = kwargs.get("queries") or []
        assert query in queries
        return [
            _local_file_source(
                file_id="alpha-file",
                name="sl190-variant.txt",
                content="SL190-2007 规定：『北方土石山区』允许土壤流失量为 200t/（km²·a）。",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=query,
            source_ids=["alpha-file"],
            required_anchors=["SL190-2007", "北方土石山区", "200t/(km2·a)"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "sl190-variant.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="sl190-variant.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] in {"success", "no_evidence"}
    exact_candidates = response["strategy_used"].get("exact_anchor_candidates") or []
    unit_candidates = [
        item for item in exact_candidates if item.get("anchor") == "200t/(km2·a)"
    ]
    assert unit_candidates
    assert all(item.get("span_basis") in {"normalized_direct_text", "normalized_compact_text"} for item in unit_candidates)
    assert all(isinstance(item.get("normalized_text_span"), list) for item in unit_candidates)
    assert all(item.get("match_span") == item.get("normalized_text_span") for item in unit_candidates)
    if response["status"] == "no_evidence":
        assert (
            "structured_precision_guard_no_supported_fields"
            in _diagnostic_reason_set(response)
            or "structured_precision_guard_incomplete_fields"
            in _diagnostic_reason_set(response)
        )


def test_structured_fact_exact_candidates_survive_filtered_no_evidence_and_scope_bound(
    monkeypatch,
):
    query = "请从定额08063提取 labor 和 farmyard manure 的 m3 取值。"

    async def fake_get_sources_from_items(*_args, **kwargs):
        items = kwargs.get("items") or []
        assert items
        assert all(str(item.get("id")) == "alpha-file" for item in items if isinstance(item, dict))
        return [
            _local_file_source(
                file_id="alpha-file",
                name="quota-08063-partial.txt",
                content="定额08063 行：labor=12工日；farmyard manure=。",
            ),
            _local_file_source(
                file_id="beta-file",
                name="quota-08063-unrelated-LEAK_NAME.txt",
                content="LEAK_CONTENT_MARKER 定额08063 行：labor=12工日；farmyard manure=3m3。",
            ),
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=query,
            source_ids=["alpha-file"],
            required_anchors=["08063", "labor", "farmyard manure", "m3"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "quota-08063-partial.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="quota-08063-partial.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["terminal_reason"] in {
        "weak_or_empty_retrieval_result",
        "structured_precision_guard_incomplete_fields",
    }
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    exact_candidates = response["strategy_used"].get("exact_anchor_candidates") or []
    assert exact_candidates
    assert all(item.get("source_id") == "alpha-file" for item in exact_candidates)
    assert all(
        str(reference.get("source", {}).get("id") or "") == "alpha-file"
        for reference in response.get("canonical_references", [])
        if isinstance(reference, dict)
    )
    reasons = _diagnostic_reason_set(response)
    assert "source_scope_mismatch" in reasons
    assert (
        "off_anchor_evidence" in reasons
        or "no_injectable_evidence" in reasons
        or "structured_precision_guard_incomplete_fields" in reasons
    )
    payload_json = json.dumps(response, ensure_ascii=False)
    assert "beta-file" not in payload_json
    assert "quota-08063-unrelated-LEAK_NAME.txt" not in payload_json
    assert "LEAK_CONTENT_MARKER" not in payload_json


def test_structured_fact_exact_lookup_unavailable_requires_local_searchable_text(
    monkeypatch,
):
    query = "请根据文号：国能发〔2024〕12号核对“北方土石山区”对应允许土壤流失量。"

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            {
                "source": {
                    "id": "",
                    "name": "",
                    "url": "",
                    "type": "file",
                },
                "document": [""],
                "metadata": [{}],
            }
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query=query,
            source_ids=["alpha-file"],
            required_anchors=["国能发〔2024〕12号", "北方土石山区"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "sl190-empty.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="sl190-empty.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "no_evidence"
    assert response["canonical_references"] == []
    assert response["accepted_outputs"] == []
    assert response["strategy_used"]["structured_routing_allowed"] is True
    assert response["strategy_used"]["structured_fact_intent"] != "none"
    reasons = _diagnostic_reason_set(response)
    assert "exact_anchor_lookup_unavailable" in reasons


def test_structured_fact_unsupported_candidate_is_gated_from_prompt_context():
    unsupported_candidate = "08063 labor=12; farmyard manure=3; other material cost=46"
    concrete_content = f"定额08063答案如下：{unsupported_candidate}"

    guarded_content, guarded_output = _apply_selected_source_diagnostics_only_content_guard(
        content=concrete_content,
        output=[
            {
                "type": "message",
                "id": "msg-structured-fact",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": concrete_content}],
            }
        ],
        metadata={
            "status": "no_evidence",
            "terminal_reason": "metadata_first_targeted_evidence_required",
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "retrieval_diagnostics": [
                {
                    "classification": "no_evidence",
                    "reason": "missing_field_mapping",
                    "outcome": "unsupported",
                }
            ],
            "active_source_scope": _resolved_active_source_scope(
                file_id="alpha-file",
                name="quota-08063.txt",
            ),
        },
        completion_metadata={
            "status": "no_evidence",
            "terminal_reason": "metadata_first_targeted_evidence_required",
            "retrieval_diagnostics": [
                {
                    "classification": "no_evidence",
                    "reason": "missing_field_mapping",
                    "outcome": "unsupported",
                }
            ],
            "accepted_outputs": [],
            "canonical_references": [],
        },
    )

    assert guarded_content != concrete_content
    assert unsupported_candidate not in guarded_content
    assert "无法给出具体文档列表或结论" in guarded_content
    assert guarded_output[-1]["content"][0]["text"] == guarded_content

    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "no_evidence",
            "terminal_reason": "metadata_first_targeted_evidence_required",
            "references": [
                _local_file_source(
                    file_id="alpha-file",
                    name="quota-08063.txt",
                    content=unsupported_candidate,
                )
            ],
            "accepted_outputs": [
                {
                    "type": "selected_source_evidence",
                    "source": {"id": "alpha-file", "name": "quota-08063.txt"},
                    "snippet": unsupported_candidate,
                }
            ],
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "targeted_context_bounded": False,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "active_source_scope": _resolved_active_source_scope(
                file_id="alpha-file",
                name="quota-08063.txt",
            ),
            "retrieval_diagnostics": [
                {
                    "classification": "no_evidence",
                    "reason": "missing_field_mapping",
                    "outcome": "unsupported",
                }
            ],
        }
    )

    assert "canonical_references" not in persisted
    assert "reference_cards" not in persisted
    assert persisted["terminal_reason"] == "metadata_first_targeted_evidence_required"


def test_diagnostics_only_blocked_persistence_keeps_reference_lanes_empty():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "blocked",
            "terminal_reason": "unsupported_latest_or_recent_claim",
            "references": [
                _local_file_source(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                    content="stale reference should not persist",
                )
            ],
            "accepted_outputs": [
                {
                    "type": "selected_source_evidence",
                    "source": {"id": "alpha-file", "name": "alpha-policy.txt"},
                    "snippet": "stale snippet",
                }
            ],
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_quality",
                    "classification": "no_evidence",
                    "reason": "unsupported_latest_or_recent_claim",
                    "outcome": "unsupported",
                }
            ],
            "provenance": {
                "compact_retrieval_provenance": {
                    "strategy": {"retrieval_strategy": "metadata_first_then_targeted_chunks"},
                    "material_limitations": ["unsupported_latest_or_recent_claim"],
                }
            },
        }
    )

    assert "canonical_references" not in persisted
    assert "reference_cards" not in persisted
    assert "references" not in persisted
    assert "accepted_outputs" not in persisted
    assert persisted["retrieval_provenance"]["material_limitations"] == [
        "unsupported_latest_or_recent_claim"
    ]


def test_blocked_selected_source_diagnostics_remove_stale_message_references():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "blocked",
            "terminal_reason": "metadata_first_targeted_evidence_required",
            "references": [],
            "accepted_outputs": [],
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_quality",
                    "classification": "no_evidence",
                    "reason": "metadata_first_targeted_evidence_required",
                    "outcome": "blocked",
                    "tool_name": "query_selected_knowledge_files",
                }
            ],
        },
        message_metadata={
            "canonical_references": [
                _local_file_source(
                    file_id="stale-file",
                    name="stale-policy.txt",
                    content="stale evidence should be removed",
                )
            ],
            "reference_cards": [
                {
                    "source": {"id": "stale-file", "name": "stale-policy.txt"},
                    "document": ["stale evidence should be removed"],
                }
            ],
        },
    )

    assert "canonical_references" not in persisted
    assert "reference_cards" not in persisted
    assert "references" not in persisted
    assert "accepted_outputs" not in persisted
    assert persisted["retrieval_diagnostics"][0]["reason"] == (
        "metadata_first_targeted_evidence_required"
    )


def test_engine_diagnostics_only_persistence_suppresses_stale_source_aliases():
    rejected_candidate_body = "Rejected candidate body must stay diagnostic-only."
    stale_reference = _local_file_source(
        file_id="stale-file",
        name="stale-policy.txt",
        content="stale source-card body should not survive diagnostic-only engine turns",
    )

    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "no_evidence",
            "terminal_reason": "rejected_candidates_only",
            "sources": [stale_reference],
            "references": [],
            "accepted_outputs": [],
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_engine",
                    "classification": "no_evidence",
                    "reason": "candidate_rejected",
                    "outcome": "no_evidence",
                    "tool_name": "query_selected_knowledge_files",
                    "detail": {
                        "candidate_body": rejected_candidate_body,
                        "candidate_id": "candidate:rejected",
                    },
                    "provenance": {
                        "worker_kind": "selected_source_retrieval",
                        "selected_source_runtime_mode": "retrieval_engine_diagnostics",
                        "engine_owned": True,
                        "compatibility_fallback": False,
                    },
                }
            ],
            "active_source_scope": _resolved_active_source_scope(
                file_id="alpha-file",
                name="alpha-policy.txt",
            ),
        },
        message_metadata={
            "sources": [stale_reference],
            "references": [stale_reference],
            "canonical_references": [stale_reference],
            "reference_cards": [stale_reference],
        },
    )

    assert persisted["status"] == "no_evidence"
    assert persisted["terminal_reason"] == "rejected_candidates_only"
    assert "canonical_references" not in persisted
    assert "reference_cards" not in persisted
    assert "references" not in persisted
    assert "accepted_outputs" not in persisted
    assert persisted["retrieval_diagnostics"][0]["reason"] == (
        "rejected_candidates_only"
    )
    assert rejected_candidate_body not in json.dumps(persisted, ensure_ascii=False)
    assert "stale source-card body" not in json.dumps(persisted, ensure_ascii=False)


def test_build_reference_sidecar_blocked_no_evidence_strips_stale_metadata_references():
    stale_reference = _local_file_source(
        file_id="stale-file",
        name="stale-policy.txt",
        content="stale evidence should never survive blocked/no-evidence turns",
    )

    sidecar = Chats.build_reference_metadata_sidecar(
        metadata={
            "status": "blocked",
            "canonical_references": [stale_reference],
            "reference_cards": [stale_reference],
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_quality",
                    "classification": "no_evidence",
                    "reason": "unsupported_document_type_claim",
                    "outcome": "unsupported",
                }
            ],
        },
        sources=[stale_reference],
    )

    assert "canonical_references" not in sidecar
    assert "reference_cards" not in sidecar
    assert sidecar["retrieval_diagnostics"][0]["reason"] == (
        "unsupported_document_type_claim"
    )


def test_build_reference_sidecar_success_keeps_current_selected_and_web_references():
    local_reference = _local_file_source(
        file_id="alpha-file",
        name="alpha-policy.txt",
        content="selected source evidence remains available on success",
    )
    web_reference = {
        "source": {
            "id": "https://example.com/policy",
            "name": "https://example.com/policy",
            "type": "web",
            "url": "https://example.com/policy",
        },
        "document": ["official policy bulletin"],
        "metadata": [{"title": "Policy Bulletin"}],
        "provenance": {"tool_name": "visit_webpage"},
    }

    sidecar = Chats.build_reference_metadata_sidecar(
        metadata={
            "status": "success",
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_quality",
                    "classification": "no_evidence",
                    "reason": "stale_previous_turn_diagnostic",
                    "outcome": "blocked",
                }
            ],
        },
        sources=[local_reference, web_reference],
    )

    assert len(sidecar["canonical_references"]) == 2
    assert len(sidecar["reference_cards"]) == 2


def test_selected_source_narrow_fact_semantic_path_still_works_with_compact_provenance(
    monkeypatch,
):
    async def fake_get_sources_from_items(*_args, **_kwargs):
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="Transformer grounding aligns model outputs with source text.",
            )
        ]

    monkeypatch.setattr(
        "open_webui.retrieval.utils.get_sources_from_items",
        fake_get_sources_from_items,
    )

    response = asyncio.run(
        query_selected_knowledge_files(
            query="what is transformer grounding",
            source_ids=["alpha-file"],
            evidence_need="narrow_fact",
            __request__=_selected_source_tool_request_stub(),
            __files__=[_selected_source_file_candidate("alpha-file", "alpha-policy.txt")],
            __metadata__={
                "active_source_scope": _resolved_active_source_scope(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                )
            },
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success"
    assert response["canonical_references"]
    assert response["accepted_outputs"]
    compact = response["provenance"]["compact_retrieval_provenance"]
    assert compact["strategy"]["retrieval_strategy"] == "semantic_chunks"


def test_selected_source_limitation_only_persistence_keeps_diagnostics_without_refs():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "no_evidence",
            "terminal_reason": "unsupported_document_type_claim",
            "references": [],
            "retrieval_diagnostics": [
                {
                    "kind": "retrieval_quality",
                    "classification": "no_evidence",
                    "reason": "unsupported_document_type_claim",
                    "outcome": "unsupported",
                    "tool_name": "query_selected_knowledge_files",
                }
            ],
        },
        message_metadata={
            "canonical_references": [
                _local_file_source(
                    file_id="stale-file",
                    name="stale-policy.txt",
                    content="stale evidence should not survive limitation-only turns",
                )
            ],
            "reference_cards": [
                {
                    "source": {"id": "stale-file", "name": "stale-policy.txt"},
                    "document": ["stale evidence should not survive limitation-only turns"],
                }
            ],
        },
    )

    assert "canonical_references" not in persisted
    assert "reference_cards" not in persisted
    assert persisted["retrieval_diagnostics"][0]["reason"] == (
        "unsupported_document_type_claim"
    )


def test_selected_source_metadata_first_persistence_drops_unrelated_broad_chunk_bundle():
    accepted_reference = _local_file_source(
        file_id="alpha-file",
        name="alpha-policy.txt",
        content="2026 Safety Policy supports storage safety anchor.",
    )
    broad_sources = _local_file_source(
        file_id="alpha-file",
        name="alpha-policy.txt",
        content="2026 Safety Policy supports storage safety anchor.",
    )
    broad_sources["document"].append("unrelated semantic chunk that should not be bundled")
    broad_sources["metadata"].append({"file_id": "alpha-file", "name": "alpha-policy.txt"})

    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "success",
            "references": [accepted_reference],
            "sources": [broad_sources],
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "targeted_context_bounded": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
        },
        message_metadata={
            "canonical_references": [broad_sources],
            "reference_cards": [broad_sources],
        },
    )

    assert len(persisted["canonical_references"]) == 1
    persisted_reference = persisted["canonical_references"][0]
    assert persisted_reference["source"]["id"] == "alpha-file"
    assert persisted_reference["document"] == [
        "2026 Safety Policy supports storage safety anchor."
    ]
    assert len(persisted["reference_cards"]) == 1
    assert persisted["reference_cards"][0]["document"] == [
        "2026 Safety Policy supports storage safety anchor."
    ]


def test_selected_source_inventory_success_merge_drops_generic_response_sources():
    collection_id = "cd2569744b8911f18ca152cca89252d6"
    persisted_sources = [
        _local_file_source(
            file_id=collection_id,
            name="能源工程技术期刊",
            content="能源工程技术2026年第1期（总第31期）正文-CTP.pdf",
        ),
        {
            "source": {
                "id": f"/api/openai/v1/files/{collection_id}/content",
                "name": f"/api/openai/v1/files/{collection_id}/content",
                "type": "generic_web",
                "url": f"/api/openai/v1/files/{collection_id}/content",
            },
            "document": [
                f"Error fetching /api/openai/v1/files/{collection_id}/content: Invalid URL"
            ],
            "metadata": [
                {
                    "source": f"/api/openai/v1/files/{collection_id}/content",
                    "name": f"/api/openai/v1/files/{collection_id}/content",
                    "source_class": "generic_web",
                }
            ],
            "source_class": "generic_web",
            "type": "retrieval_reference",
        },
    ]
    completion_metadata = {
        "status": "success",
        "terminal_reason": "success",
        "active_source_scope": {
            "status": "resolved",
            "source_set_mode": "single",
            "source_ids": [collection_id],
            "sources": [
                {
                    "id": collection_id,
                    "name": "能源工程技术期刊",
                    "type": "knowledge",
                    "authority": "current_upload",
                }
            ],
            "focus_state": "single",
            "authority": "current_upload",
        },
        "first_pass_retrieval_strategy": {
            "metadata_first_intent": True,
            "retrieval_strategy": "metadata_first_then_targeted_chunks",
        },
    }

    merged = _merge_persisted_and_response_sources(
        persisted_sources,
        persisted_sources,
        completion_metadata=completion_metadata,
    )
    filtered = _filter_uncited_no_evidence_source_cards(
        merged,
        content="已按排序依据列出所选集合文档。[1]",
        metadata=completion_metadata,
    )

    assert len(filtered) == 1
    assert filtered[0]["source"]["id"] == collection_id
    assert len(Chats.build_reference_cards(filtered)) == 1


def test_selected_source_source_merge_keeps_narrow_fact_persisted_reference_only():
    persisted_sources = [
        _local_file_source(
            file_id="alpha-file",
            name="alpha-policy.txt",
            content="Transformer grounding aligns model outputs with source text.",
        )
    ]
    response_sources = [
        {
            **persisted_sources[0],
            "document": [
                "Transformer grounding aligns model outputs with source text.",
                "unrelated bundled chunk should not persist for selected-source turn",
            ],
            "metadata": [
                {"file_id": "alpha-file", "name": "alpha-policy.txt"},
                {"file_id": "alpha-file", "name": "alpha-policy.txt"},
            ],
        }
    ]
    response_sources.append(
        {
            "source": {
                "id": "https://example.com/offscope",
                "name": "https://example.com/offscope",
                "url": "https://example.com/offscope",
                "type": "generic_web",
            },
            "document": ["off-scope web payload should not persist in selected-source lane"],
            "metadata": [{"source": "https://example.com/offscope"}],
            "source_class": "generic_web",
            "type": "retrieval_reference",
        }
    )
    completion_metadata = {
        "active_source_scope": _resolved_active_source_scope(
            file_id="alpha-file",
            name="alpha-policy.txt",
        ),
        "first_pass_retrieval_strategy": {
            "retrieval_strategy": "semantic_chunks",
            "metadata_first_intent": False,
        },
    }

    merged = _merge_persisted_and_response_sources(
        persisted_sources,
        response_sources,
        completion_metadata=completion_metadata,
    )

    assert len(merged) == 1
    assert merged[0]["document"] == [
        "Transformer grounding aligns model outputs with source text."
    ]
    assert merged[0]["source"]["id"] == "alpha-file"


def test_non_selected_web_success_merge_keeps_web_reference_cards():
    web_reference = {
        "source": {
            "id": "https://www.example.com/article",
            "name": "https://www.example.com/article",
            "url": "https://www.example.com/article",
            "type": "generic_web",
        },
        "document": ["Web search evidence"],
        "metadata": [{"source": "https://www.example.com/article"}],
        "source_class": "generic_web",
        "type": "retrieval_reference",
    }
    merged = _merge_persisted_and_response_sources(
        [],
        [web_reference],
        completion_metadata={"status": "success", "terminal_reason": "success"},
    )
    filtered = _filter_uncited_no_evidence_source_cards(
        merged,
        content="根据网页证据，结论如下。[1]",
        metadata={"status": "success", "terminal_reason": "success"},
    )

    assert len(filtered) == 1
    assert filtered[0]["source"]["id"] == "https://www.example.com/article"
    assert len(Chats.build_reference_cards(filtered)) == 1


def test_assistant_metadata_includes_compact_profile_routing_diagnostics():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "blocked",
            "references": [],
            "first_pass_profile_lock": {
                "incompatible": True,
                "non_promotion_reason": "explicit_incompatible_profile",
                "classified_evidence_need": "metadata_first_inventory",
                "resolved_execution_profile": "general",
            },
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory",
                "resolved_execution_profile": "general",
                "promotion_reason": "",
                "non_promotion_reason": "explicit_incompatible_profile",
                "provider_payload": {"should": "not_leak"},
            },
        }
    )

    diagnostics = persisted.get("execution_profile_routing_diagnostics") or {}
    assert diagnostics["resolved_execution_profile"] == "general"
    assert diagnostics["classified_evidence_need"] == "metadata_first_inventory"
    assert diagnostics["non_promotion_reason"] == "explicit_incompatible_profile"
    assert diagnostics["explicit_profile_lock"] is True
    assert "provider_payload" not in diagnostics


def test_selected_source_persistence_trims_provider_bundle_to_accepted_chunk():
    bundled_reference = {
        "source": {
            "id": "alpha-file",
            "name": "alpha-policy.txt",
            "type": "file",
        },
        "document": [
            "chunk-1 unrelated introduction",
            "chunk-2 accepted and relevant evidence",
            "chunk-3 unrelated appendix",
        ],
        "metadata": [
            {"file_id": "alpha-file", "name": "alpha-policy.txt", "chunk_id": "c1"},
            {"file_id": "alpha-file", "name": "alpha-policy.txt", "chunk_id": "c2"},
            {"file_id": "alpha-file", "name": "alpha-policy.txt", "chunk_id": "c3"},
        ],
    }

    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "success",
            "references": [bundled_reference],
            "accepted_outputs": [
                {
                    "type": "selected_source_evidence",
                    "source": {"id": "alpha-file", "name": "alpha-policy.txt"},
                    "snippet": "chunk-2 accepted and relevant evidence",
                }
            ],
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "targeted_context_bounded": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "active_source_scope": _resolved_active_source_scope(
                file_id="alpha-file",
                name="alpha-policy.txt",
            ),
        }
    )

    assert len(persisted["canonical_references"]) == 1
    trimmed = persisted["canonical_references"][0]
    assert trimmed["document"] == ["chunk-2 accepted and relevant evidence"]
    assert [item["chunk_id"] for item in trimmed["metadata"]] == ["c2"]


def test_selected_source_narrow_fact_persistence_trims_unrelated_sibling_chunks():
    bundled_reference = {
        "source": {
            "id": "alpha-file",
            "name": "alpha-policy.txt",
            "type": "file",
        },
        "document": [
            "The answer author is Deng and Xu.",
            "Unrelated sibling chunk should not persist.",
            "Another unrelated sibling chunk.",
        ],
        "metadata": [
            {"file_id": "alpha-file", "name": "alpha-policy.txt", "chunk_id": "a1"},
            {"file_id": "alpha-file", "name": "alpha-policy.txt", "chunk_id": "a2"},
            {"file_id": "alpha-file", "name": "alpha-policy.txt", "chunk_id": "a3"},
        ],
    }

    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "success",
            "references": [bundled_reference],
            "accepted_outputs": [
                {
                    "type": "selected_source_evidence",
                    "source": {"id": "alpha-file", "name": "alpha-policy.txt"},
                    "snippet": "The answer author is Deng and Xu.",
                }
            ],
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": False,
                "retrieval_strategy": "semantic_chunks",
            },
            "active_source_scope": _resolved_active_source_scope(
                file_id="alpha-file",
                name="alpha-policy.txt",
            ),
        }
    )

    assert len(persisted["canonical_references"]) == 1
    assert persisted["canonical_references"][0]["document"] == [
        "The answer author is Deng and Xu."
    ]
    assert len(persisted["reference_cards"]) == 1
    assert persisted["reference_cards"][0]["document"] == [
        "The answer author is Deng and Xu."
    ]


def test_missing_status_metadata_first_listing_becomes_diagnostics_only():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "references": [
                _local_file_source(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                    content="broad provider bundle should not persist for metadata-first listing",
                )
            ],
            "accepted_outputs": [],
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "targeted_context_bounded": False,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "execution_profile_routing_diagnostics": {
                "resolved_execution_profile": "research",
                "classified_evidence_need": "none",
            },
            "active_source_scope": _resolved_active_source_scope(
                file_id="alpha-file",
                name="alpha-policy.txt",
            ),
        }
    )

    assert persisted["status"] == "blocked"
    assert persisted["terminal_reason"] == "metadata_first_targeted_evidence_required"
    assert "canonical_references" not in persisted
    assert "reference_cards" not in persisted
    reasons = {
        str(item.get("reason") or "")
        for item in persisted.get("retrieval_diagnostics", [])
        if isinstance(item, dict)
    }
    assert "metadata_first_targeted_evidence_required" in reasons


def test_metadata_first_listing_persists_compact_profile_classification():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
                "evidence_need": "none",
                "targeted_context_bounded": True,
            },
            "execution_profile_routing_diagnostics": {
                "resolved_execution_profile": "general",
                "classified_evidence_need": "none",
                "promotion_reason": "",
                "non_promotion_reason": "",
            },
        }
    )

    diagnostics = persisted.get("execution_profile_routing_diagnostics") or {}
    assert diagnostics["resolved_execution_profile"] == "general"
    assert diagnostics["classified_evidence_need"] == "metadata_first_inventory"


def test_selected_source_limitation_guard_restricts_source_naming_to_active_scope():
    messages = [{"role": "user", "content": "最近有哪些相关政策？"}]
    guarded = _append_selected_source_limitation_guard(
        messages,
        terminal_reason="metadata_first_targeted_evidence_required",
        active_source_scope={
            "status": "resolved",
            "source_set_mode": "single",
            "source_ids": ["collection-1"],
            "sources": [{"id": "collection-1", "name": "Collection A", "type": "collection"}],
        },
    )

    system_message = next(
        (item for item in guarded if isinstance(item, dict) and item.get("role") == "system"),
        {},
    )
    content = str(system_message.get("content") or "")
    assert "Collection A" in content
    assert "Do not reuse previous-turn source-derived lists" in content


def test_selected_source_ambiguous_scope_requires_stable_limitation_without_metadata_first():
    metadata = {
        "status": "blocked",
        "terminal_reason": "ambiguous_retrieval_scope",
        "first_pass_retrieval_strategy": {
            "metadata_first_intent": False,
            "retrieval_strategy": "semantic_chunks",
        },
        "active_source_scope": {
            "status": "ambiguous",
            "source_set_mode": "none",
            "source_ids": ["alpha-file", "beta-file"],
            "sources": [
                {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
                {"id": "beta-file", "name": "beta-policy.txt", "type": "file"},
            ],
            "reason": "ambiguous_retrieval_scope",
            "confidence": "low",
        },
        "retrieval_diagnostics": [
            {
                "classification": "diagnostics",
                "reason": "ambiguous_retrieval_scope",
                "candidate_index": -1,
            }
        ],
    }

    assert _selected_source_requires_stable_limitation_response(metadata) is True


def test_ambiguous_selected_source_content_guard_rewrites_generic_reupload_answer():
    generic_reupload_answer = (
        "当前对话中我没有检测到任何已上传或已选的文件，因此无法从中提取“储备比例”。"
        "请上传您要参考的文件。"
    )
    metadata = {
        "status": "blocked",
        "terminal_reason": "ambiguous_retrieval_scope",
        "first_pass_retrieval_strategy": {
            "metadata_first_intent": False,
            "retrieval_strategy": "semantic_chunks",
        },
        "active_source_scope": {
            "status": "ambiguous",
            "source_set_mode": "none",
            "source_ids": ["alpha-file", "beta-file"],
            "sources": [
                {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
                {"id": "beta-file", "name": "beta-policy.txt", "type": "file"},
            ],
            "reason": "ambiguous_retrieval_scope",
            "confidence": "low",
        },
        "retrieval_diagnostics": [
            {
                "classification": "diagnostics",
                "reason": "ambiguous_retrieval_scope",
                "candidate_index": -1,
            }
        ],
    }

    guarded_content, guarded_output = _apply_selected_source_diagnostics_only_content_guard(
        content=generic_reupload_answer,
        output=[
            {
                "type": "message",
                "id": "msg-1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": generic_reupload_answer}],
            }
        ],
        metadata=metadata,
        completion_metadata={},
    )

    assert "alpha-policy.txt" in guarded_content
    assert "beta-policy.txt" in guarded_content
    assert "无法判断您指的是哪一个" in guarded_content
    assert "请上传" not in guarded_content
    assert guarded_output[-1]["content"][0]["text"] == guarded_content


def test_selected_source_limitation_guard_for_ambiguous_scope_forbids_no_upload_claim():
    messages = [{"role": "user", "content": "这个文件的储备比例是多少？"}]
    guarded = _append_selected_source_limitation_guard(
        messages,
        terminal_reason="ambiguous_retrieval_scope",
        active_source_scope={
            "status": "ambiguous",
            "source_set_mode": "none",
            "source_ids": ["alpha-file", "beta-file"],
            "sources": [
                {"id": "alpha-file", "name": "alpha-policy.txt", "type": "file"},
                {"id": "beta-file", "name": "beta-policy.txt", "type": "file"},
            ],
            "reason": "ambiguous_retrieval_scope",
            "confidence": "low",
        },
    )

    system_message = next(
        (item for item in guarded if isinstance(item, dict) and item.get("role") == "system"),
        {},
    )
    content = str(system_message.get("content") or "")
    assert "Do not say no file was uploaded" in content
    assert "name the target file" in content


def test_runtime_selected_collection_shape_inventory_fallback_accepts_references(
    monkeypatch,
):
    class _FakeFileModel:
        def __init__(self, payload: dict):
            self._payload = payload

        def model_dump(self):
            return self._payload

    runtime_collection = {
        "type": "collection",
        "id": "632843fb-69ed-4575-bf33-e28bf8c5e995",
        "name": "e2e-capability-descriptor-smoke-20260513",
        "context": "full",
        "focus_tier": "active",
        "focus_origin": "current_turn",
        "user_id": "461a35be-3613-4fb1-8201-95e5dc21e468",
        "meta": {"document_count": 2, "chunk_count": 2, "status": "1"},
        "source": "knowledge",
        "status": "processed",
        "url": "632843fb-69ed-4575-bf33-e28bf8c5e995",
        "files_count": 2,
    }

    async def fake_prepare_files(*_args, **_kwargs):
        return [runtime_collection], [], [], [runtime_collection], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return []

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.Knowledges.get_files_by_id",
        lambda _collection_id: [
            _FakeFileModel(
                {
                    "id": "7a5a6d75-16d9-4ab9-8ab8-9730ca38bc6d",
                    "filename": "langfuse-e2e-alpha.txt",
                    "updated_at": 1778654111,
                    "meta": {"name": "langfuse-e2e-alpha.txt"},
                    "data": {"content": "ALPHA_MARKER runtime row"},
                }
            ),
            _FakeFileModel(
                {
                    "id": "298c2fd6-c376-48d8-a3f7-4225b16793d6",
                    "filename": "langfuse-e2e-beta.txt",
                    "updated_at": 1778654110,
                    "meta": {"name": "langfuse-e2e-beta.txt"},
                    "data": {"content": "BETA_MARKER runtime row"},
                }
            ),
        ],
    )

    body = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "请列出所选集合中的文档/文章标题，并说明你的排序依据。"}
        ],
        "metadata": {
            "files": [runtime_collection],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted = _build_assistant_reference_persistence_metadata(flags)

    assert flags["status"] == "success"
    assert flags["terminal_reason"] == "success"
    assert len(flags["references"]) >= 1
    assert len(flags["accepted_outputs"]) >= 1
    assert len(persisted["canonical_references"]) >= 1
    assert len(persisted["reference_cards"]) >= 1
    assert "off_facet_evidence" not in {
        str(item.get("reason") or "")
        for item in flags.get("retrieval_diagnostics", [])
        if isinstance(item, dict)
    }


def test_runtime_selected_collection_shape_knowflow_inventory_fallback_accepts_refs_without_scope_widening(
    monkeypatch,
):
    runtime_active_collection = {
        "type": "collection",
        "id": "cd2569744b8911f18ca152cca89252d6",
        "name": "能源工程技术期刊",
        "context": "full",
        "focus_tier": "active",
        "focus_origin": "current_turn",
        "meta": {"document_count": 31, "chunk_count": 306, "status": "1"},
        "source": "knowledge",
        "status": "processed",
        "url": "cd2569744b8911f18ca152cca89252d6",
        "files_count": 31,
    }
    runtime_reference_collection = {
        "type": "collection",
        "id": "reference-collection-id",
        "name": "Reference Collection",
        "context": "snippet",
        "focus_tier": "reference",
        "focus_origin": "history",
        "meta": {"document_count": 5, "chunk_count": 22, "status": "1"},
        "source": "knowledge",
        "status": "processed",
        "url": "reference-collection-id",
        "files_count": 5,
    }

    async def fake_prepare_files(*_args, **_kwargs):
        return (
            [runtime_active_collection, runtime_reference_collection],
            [],
            [],
            [runtime_active_collection],
            [runtime_reference_collection],
        )

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return []

    queried_ids: list[str] = []

    async def fake_list_knowledge_documents(
        _config,
        _user,
        knowledge_id,
        *,
        query=None,
        order_by=None,
        direction=None,
        page=1,
        page_size=30,
        db=None,
    ):
        queried_ids.append(str(knowledge_id))
        if str(knowledge_id) != runtime_active_collection["id"]:
            return {"items": [], "total": 0}
        return {
            "items": [
                {
                    "id": "1e2457b94b8c11f1837f52cca89252d6",
                    "filename": "能源工程技术2026年第1期（总第31期）正文-CTP.pdf",
                    "updated_at": 1778320212,
                    "meta": {"name": "能源工程技术2026年第1期（总第31期）正文-CTP.pdf"},
                    "data": None,
                    "collection": {
                        "id": runtime_active_collection["id"],
                        "name": runtime_active_collection["name"],
                    },
                },
                {
                    "id": "1dcf5f774b8c11f1992452cca89252d6",
                    "filename": "能源工程技术2025年第4期（总第30期）正文-CTP.pdf",
                    "updated_at": 1778320059,
                    "meta": {"name": "能源工程技术2025年第4期（总第30期）正文-CTP.pdf"},
                    "data": None,
                    "collection": {
                        "id": runtime_active_collection["id"],
                        "name": runtime_active_collection["name"],
                    },
                },
            ],
            "total": 2,
        }

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.Knowledges.get_files_by_id",
        lambda _collection_id: [],
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.list_knowledge_documents",
        fake_list_knowledge_documents,
    )

    body = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "请列出所选集合中的文档/文章标题，并说明你的排序依据。"}
        ],
        "metadata": {
            "files": [runtime_active_collection, runtime_reference_collection],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted = _build_assistant_reference_persistence_metadata(flags)

    assert queried_ids == [runtime_active_collection["id"]]
    assert flags["status"] == "success"
    assert flags["terminal_reason"] == "success"
    assert len(flags["references"]) >= 1
    assert len(flags["accepted_outputs"]) >= 1
    assert all(
        (item.get("source") or {}).get("id") == runtime_active_collection["id"]
        for item in flags["references"]
    )
    assert len(persisted["canonical_references"]) >= 1
    assert len(persisted["reference_cards"]) >= 1
    assert all(
        (item.get("source") or {}).get("id") == runtime_active_collection["id"]
        for item in persisted["canonical_references"]
    )
    metadata_item = persisted["canonical_references"][0]["metadata"][0]
    assert metadata_item["chunk_type"] == "inventory_row"
    assert metadata_item["ordering_basis"] == "file_updated_at_desc"
    assert metadata_item["date_basis"] == "file_updated_at"
    assert "inventory_unavailable" not in {
        str(item.get("reason") or "")
        for item in flags.get("retrieval_diagnostics", [])
        if isinstance(item, dict)
    }


def test_runtime_selected_collection_shape_knowflow_inventory_absent_is_diagnostics_only(
    monkeypatch,
):
    runtime_collection = {
        "type": "collection",
        "id": "cd2569744b8911f18ca152cca89252d6",
        "name": "能源工程技术期刊",
        "context": "full",
        "focus_tier": "active",
        "focus_origin": "current_turn",
        "meta": {"document_count": 31, "chunk_count": 306, "status": "1"},
        "source": "knowledge",
        "status": "processed",
        "url": "cd2569744b8911f18ca152cca89252d6",
        "files_count": 31,
    }

    async def fake_prepare_files(*_args, **_kwargs):
        return [runtime_collection], [], [], [runtime_collection], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        return []

    async def fake_list_knowledge_documents(*_args, **_kwargs):
        return {"items": [], "total": 0}

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.Knowledges.get_files_by_id",
        lambda _collection_id: [],
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.list_knowledge_documents",
        fake_list_knowledge_documents,
    )

    body = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "请列出所选集合中的文档/文章标题，并说明你的排序依据。"}
        ],
        "metadata": {
            "files": [runtime_collection],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )
    persisted = _build_assistant_reference_persistence_metadata(flags)

    assert flags["status"] == "blocked"
    assert flags["terminal_reason"] == "metadata_first_targeted_evidence_required"
    assert flags["references"] == []
    assert flags["accepted_outputs"] == []
    assert persisted.get("canonical_references") in (None, [])
    assert persisted.get("reference_cards") in (None, [])
    assert "inventory_unavailable" in {
        str(item.get("reason") or "")
        for item in flags.get("retrieval_diagnostics", [])
        if isinstance(item, dict)
    }


def test_metadata_first_diagnostics_only_turn_forces_limitation_content():
    concrete_content = (
        "截至2026年5月，最近与风电相关的主要政策如下：1) A 2) B 3) C"
    )
    guarded_content, guarded_output = _apply_selected_source_diagnostics_only_content_guard(
        content=concrete_content,
        output=[
            {
                "type": "message",
                "id": "msg-1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": concrete_content}],
            }
        ],
        metadata={
            "status": "no_evidence",
            "terminal_reason": "no_retrieval_candidates",
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "retrieval_diagnostics": [
                {
                    "classification": "no_evidence",
                    "reason": "unsupported_document_type_claim",
                    "outcome": "unsupported",
                }
            ],
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["collection-1"],
                "sources": [
                    {
                        "id": "collection-1",
                        "name": "Collection A",
                        "type": "collection",
                        "authority": "selected_source",
                    }
                ],
                "focus_state": "single",
                "authority": "selected_source",
            },
        },
        completion_metadata={
            "status": "no_evidence",
            "terminal_reason": "no_retrieval_candidates",
            "retrieval_diagnostics": [
                {
                    "classification": "no_evidence",
                    "reason": "unsupported_document_type_claim",
                    "outcome": "unsupported",
                }
            ],
        },
    )

    assert guarded_content != concrete_content
    assert "无法给出具体文档列表或结论" in guarded_content
    assert "Collection A" in guarded_content
    assert guarded_output[-1]["content"][0]["text"] == guarded_content


def test_metadata_first_diagnostics_only_with_missing_inventory_keeps_no_refs_cards():
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "no_evidence",
            "terminal_reason": "no_retrieval_candidates",
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
                "targeted_context_bounded": True,
            },
            "retrieval_diagnostics": [
                {
                    "classification": "no_evidence",
                    "reason": "inventory_unavailable",
                    "outcome": "blocked",
                }
            ],
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["collection-1"],
                "sources": [{"id": "collection-1", "name": "Collection A", "type": "collection"}],
                "focus_state": "single",
                "authority": "selected_source",
            },
            "canonical_references": [
                _local_file_source(
                    file_id="stale-file",
                    name="stale.txt",
                    content="stale reference should be removed",
                )
            ],
        }
    )

    assert persisted["status"] == "no_evidence"
    assert persisted["terminal_reason"] == "no_retrieval_candidates"
    assert "canonical_references" not in persisted
    assert "reference_cards" not in persisted


def test_diagnostics_only_content_guard_does_not_change_narrow_fact_success():
    original = "作者是邓某和徐某。"
    guarded_content, guarded_output = _apply_selected_source_diagnostics_only_content_guard(
        content=original,
        output=[
            {
                "type": "message",
                "id": "msg-1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": original}],
            }
        ],
        metadata={
            "status": "success",
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": False,
                "retrieval_strategy": "semantic_chunks",
            },
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["file-1"],
                "sources": [{"id": "file-1", "name": "alpha.txt", "type": "file"}],
                "focus_state": "single",
                "authority": "selected_source",
            },
            "accepted_outputs": [{"type": "selected_source_evidence", "snippet": original}],
        },
        completion_metadata={
            "status": "success",
            "canonical_references": [
                _local_file_source(file_id="file-1", name="alpha.txt", content=original)
            ],
        },
    )

    assert guarded_content == original
    assert guarded_output[-1]["content"][0]["text"] == original


def test_selected_source_metadata_first_success_with_inventory_refs_persists_cards():
    inventory_reference = {
        "source": {
            "id": "632843fb-69ed-4575-bf33-e28bf8c5e995",
            "name": "e2e-capability-descriptor-smoke-20260513",
            "type": "collection",
            "source": "knowledge",
        },
        "document": ["ALPHA_MARKER runtime inventory row."],
        "metadata": [
            {
                "source": "632843fb-69ed-4575-bf33-e28bf8c5e995",
                "name": "langfuse-e2e-alpha.txt",
                "title": "langfuse-e2e-alpha.txt",
                "chunk_type": "inventory_row",
                "ordering_basis": "file_updated_at_desc",
                "date_basis": "file_updated_at",
            }
        ],
        "retrieval_outcome": "success",
        "retrieval_classification": "injectable",
        "source_class": "collection",
        "type": "retrieval_reference",
    }

    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "success",
            "terminal_reason": "success",
            "references": [inventory_reference],
            "accepted_outputs": [
                {
                    "type": "selected_source_evidence",
                    "snippet": "ALPHA_MARKER runtime inventory row.",
                    "source": {"id": "632843fb-69ed-4575-bf33-e28bf8c5e995"},
                }
            ],
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "targeted_context_bounded": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["632843fb-69ed-4575-bf33-e28bf8c5e995"],
                "sources": [
                    {
                        "id": "632843fb-69ed-4575-bf33-e28bf8c5e995",
                        "name": "e2e-capability-descriptor-smoke-20260513",
                        "type": "knowledge",
                        "authority": "current_upload",
                    }
                ],
                "focus_state": "single",
                "authority": "current_upload",
            },
            "retrieval_diagnostics": [
                {
                    "classification": "no_evidence",
                    "reason": "missing_document_body",
                    "outcome": "empty",
                }
            ],
        }
    )

    assert persisted["status"] == "success"
    assert persisted["terminal_reason"] == "success"
    assert len(persisted.get("canonical_references") or []) == 1
    assert len(persisted.get("reference_cards") or []) == 1
    metadata_item = persisted["canonical_references"][0]["metadata"][0]
    assert metadata_item["chunk_type"] == "inventory_row"
    assert metadata_item["ordering_basis"] == "file_updated_at_desc"


def test_metadata_first_success_without_refs_is_downgraded_before_final_persistence():
    normalized = _enforce_selected_source_metadata_first_final_consistency(
        completion_metadata={
            "status": "success",
            "terminal_reason": "success",
            "retrieval_diagnostics": [
                {"classification": "no_evidence", "reason": "missing_document_body"}
            ],
        },
        metadata={
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["collection-1"],
                "sources": [
                    {
                        "id": "collection-1",
                        "name": "Collection A",
                        "type": "collection",
                        "authority": "current_upload",
                    }
                ],
                "focus_state": "single",
                "authority": "current_upload",
            },
        },
    )

    assert normalized["status"] == "no_evidence"
    assert normalized["terminal_reason"] == "no_accepted_references"
    assert any(
        str(item.get("reason") or "") == "no_accepted_references"
        for item in normalized.get("retrieval_diagnostics", [])
        if isinstance(item, dict)
    )


def test_unsupported_selected_source_metadata_first_turn_drops_generic_web_refs_cards():
    web_reference = {
        "source": {
            "id": "https://www.example.com/policy",
            "name": "https://www.example.com/policy",
            "url": "https://www.example.com/policy",
            "type": "generic_web",
            "authority": "generic",
        },
        "document": ["Wind policy page."],
        "metadata": [
            {
                "source": "https://www.example.com/policy",
                "name": "https://www.example.com/policy",
                "url": "https://www.example.com/policy",
                "tool_name": "visit_webpage",
                "source_class": "generic_web",
                "authority": "generic",
            }
        ],
        "source_class": "generic_web",
        "authority": "generic",
        "type": "retrieval_reference",
    }
    persisted = _build_assistant_reference_persistence_metadata(
        {
            "status": "no_evidence",
            "terminal_reason": "unsupported_document_type_claim",
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["collection-1"],
                "sources": [
                    {
                        "id": "collection-1",
                        "name": "Collection A",
                        "type": "collection",
                        "authority": "current_upload",
                    }
                ],
                "focus_state": "single",
                "authority": "current_upload",
            },
            "retrieval_diagnostics": [
                {
                    "classification": "diagnostics",
                    "reason": "missing_type_metadata",
                    "outcome": "unsupported",
                },
                {
                    "classification": "diagnostics",
                    "reason": "missing_topic_metadata",
                    "outcome": "unsupported",
                },
            ],
        },
        message_metadata={
            "canonical_references": [web_reference],
            "reference_cards": [web_reference],
        },
    )

    assert persisted["status"] == "no_evidence"
    assert persisted["terminal_reason"] == "unsupported_document_type_claim"
    assert "canonical_references" not in persisted
    assert "reference_cards" not in persisted


def test_runtime_shape_inventory_success_without_refs_is_downgraded_and_guarded():
    concrete_content = (
        "基于所选集合的可用内容，包含如下文档，并按更新时间排序："
        "1) A ... 2) B ..."
    )
    completion_metadata = _build_assistant_reference_persistence_metadata(
        {
            "status": "success",
            "terminal_reason": "success",
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["632843fb-69ed-4575-bf33-e28bf8c5e995"],
                "sources": [
                    {
                        "id": "632843fb-69ed-4575-bf33-e28bf8c5e995",
                        "name": "e2e-capability-descriptor-smoke-20260513",
                        "type": "knowledge",
                        "authority": "current_upload",
                    }
                ],
                "focus_state": "single",
                "authority": "current_upload",
            },
            "execution_profile_routing_diagnostics": {
                "resolved_execution_profile": "general",
                "classified_evidence_need": "metadata_first_inventory",
                "promotion_reason": "",
                "non_promotion_reason": "none",
                "explicit_profile_lock": False,
            },
            "retrieval_diagnostics": [
                {
                    "classification": "no_evidence",
                    "reason": "missing_document_body",
                    "outcome": "empty",
                }
            ],
        }
    )
    normalized = _enforce_selected_source_metadata_first_final_consistency(
        completion_metadata=completion_metadata,
        metadata={},
    )
    guarded_content, guarded_output = _apply_selected_source_diagnostics_only_content_guard(
        content=concrete_content,
        output=[
            {
                "type": "message",
                "id": "msg-1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": concrete_content}],
            }
        ],
        metadata={},
        completion_metadata=normalized,
    )

    assert normalized["status"] == "no_evidence"
    assert normalized["terminal_reason"] == "no_accepted_references"
    assert "canonical_references" not in normalized
    assert "reference_cards" not in normalized
    assert "无法给出具体文档列表或结论" in guarded_content
    assert guarded_output[-1]["content"][0]["text"] == guarded_content


def test_selected_source_unsupported_lane_drops_generic_web_refs_in_final_filter():
    web_reference = {
        "source": {
            "id": "https://www.example.com/policy",
            "name": "https://www.example.com/policy",
            "url": "https://www.example.com/policy",
            "type": "generic_web",
            "authority": "generic",
        },
        "document": ["web policy listing result"],
        "metadata": [{"source": "https://www.example.com/policy"}],
        "source_class": "generic_web",
        "authority": "generic",
        "type": "retrieval_reference",
    }

    filtered = _filter_uncited_no_evidence_source_cards(
        [web_reference],
        content="以下是最近政策列表：...",
        metadata={
            "status": "no_evidence",
            "terminal_reason": "unsupported_document_type_claim",
            "active_source_scope": {
                "status": "resolved",
                "source_set_mode": "single",
                "source_ids": ["collection-1"],
                "sources": [
                    {
                        "id": "collection-1",
                        "name": "Collection A",
                        "type": "collection",
                        "authority": "current_upload",
                    }
                ],
                "focus_state": "single",
                "authority": "current_upload",
            },
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "metadata_first_inventory"
            },
            "retrieval_diagnostics": [
                {
                    "classification": "diagnostics",
                    "reason": "missing_type_metadata",
                    "outcome": "unsupported",
                },
                {
                    "classification": "diagnostics",
                    "reason": "missing_topic_metadata",
                    "outcome": "unsupported",
                },
            ],
        },
    )

    assert filtered == []


def test_unrelated_successful_web_references_still_persist():
    web_reference = {
        "source": {
            "id": "https://www.example.com/article",
            "name": "https://www.example.com/article",
            "url": "https://www.example.com/article",
            "type": "generic_web",
            "authority": "generic",
        },
        "document": ["Web search evidence"],
        "metadata": [{"source": "https://www.example.com/article"}],
        "source_class": "generic_web",
        "authority": "generic",
        "type": "retrieval_reference",
    }

    filtered = _filter_uncited_no_evidence_source_cards(
        [web_reference],
        content="根据网页证据，结论如下。[1]",
        metadata={"status": "success", "terminal_reason": "success"},
    )

    assert len(filtered) == 1
    assert filtered[0]["source"]["id"] == "https://www.example.com/article"


def test_final_limitation_guard_rewrites_unsupported_concrete_content_after_lane_enforcement():
    concrete_content = "最近一年风电政策有 7 条，按时间倒序如下：..."
    scope = {
        "status": "resolved",
        "source_set_mode": "single",
        "source_ids": ["collection-1"],
        "sources": [
            {
                "id": "collection-1",
                "name": "Collection A",
                "type": "collection",
                "authority": "current_upload",
            }
        ],
        "focus_state": "single",
        "authority": "current_upload",
    }
    completion_metadata = _enforce_selected_source_metadata_first_final_consistency(
        completion_metadata={
            "status": "no_evidence",
            "terminal_reason": "unsupported_document_type_claim",
            "canonical_references": [
                _local_file_source(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                    content="should be removed in unsupported lane",
                )
            ],
            "reference_cards": [
                _local_file_source(
                    file_id="alpha-file",
                    name="alpha-policy.txt",
                    content="should be removed in unsupported lane",
                )
            ],
            "retrieval_diagnostics": [
                {
                    "classification": "diagnostics",
                    "reason": "missing_type_metadata",
                    "outcome": "unsupported",
                }
            ],
        },
        metadata={
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "active_source_scope": scope,
        },
    )
    guarded_content, guarded_output = _apply_selected_source_diagnostics_only_content_guard(
        content=concrete_content,
        output=[
            {
                "type": "message",
                "id": "msg-1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": concrete_content}],
            }
        ],
        metadata={
            "accepted_outputs": [],
            "first_pass_retrieval_strategy": {
                "metadata_first_intent": True,
                "retrieval_strategy": "metadata_first_then_targeted_chunks",
            },
            "active_source_scope": scope,
        },
        completion_metadata=completion_metadata,
    )

    assert "无法给出具体文档列表或结论" in guarded_content
    assert guarded_content != concrete_content
    assert guarded_output[-1]["content"][0]["text"] == guarded_content


# ---------------------------------------------------------------------------
# Engine contract always bypasses legacy when engine produced a valid result
# ---------------------------------------------------------------------------


def _engine_contract_bypass_legacy_fixture(
    *,
    engine_status: str,
    engine_terminal_reason: str = "",
    include_evidence: bool = True,
    diagnostics: list[dict] | None = None,
    authority_state: str = "",
):
    """Build a middleware monkeypatch set that intercepts legacy calls."""
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "meta": {"content_type": "text/plain"},
    }

    contract = _engine_authority_tool_contract(
        status=engine_status,
        terminal_reason=engine_terminal_reason or engine_status,
        include_evidence=include_evidence,
        diagnostics=diagnostics,
    )
    # The middleware reads engine_contract.get("sources", []) for source cards.
    # Mirror what _retrieval_engine_result_first_pass_contract produces.
    if "sources" not in contract:
        contract["sources"] = list(contract.get("references") or [])

    if not authority_state:
        if engine_status in {"success", "partial"} and include_evidence:
            authority_state = "authoritative"
        elif engine_status in {"denied", "blocked"}:
            authority_state = "fail_closed"
        else:
            authority_state = "diagnostics"

    return retrieval_candidate, contract, authority_state


def test_engine_success_contract_bypasses_legacy_and_maps_references(monkeypatch):
    retrieval_candidate, contract, authority_state = _engine_contract_bypass_legacy_fixture(
        engine_status="success",
    )

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def emit_event(_event):
        return None

    legacy_calls = {"count": 0}

    async def raise_legacy_provider(*_args, **_kwargs):
        legacy_calls["count"] += 1
        raise AssertionError("legacy provider must be bypassed when engine has valid contract")

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        raise_legacy_provider,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_strategy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware strategy must be bypassed")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        lambda **_kwargs: {
            "authority": {"state": authority_state, "reason": "success"},
            "contract": contract,
            "observe": {
                "mode": "observe_parallel",
                "lane": "selected_file_text",
                "authority": "retrieval_engine",
                "status": "success",
                "terminal_reason": "success",
                "plan_summary": {"evidence_shape": "narrow_chunk"},
                "counts": {
                    "candidate_count": 1,
                    "evidence_bundle_count": 1,
                    "accepted_output_count": 1,
                    "reference_count": 1,
                    "diagnostic_count": 0,
                },
                "diagnostics": [],
            },
        },
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert legacy_calls["count"] == 0
    assert flags["status"] == "success"
    assert flags["accepted_outputs"]
    assert flags["references"]
    assert flags["sources"][0]["source"]["id"] == "alpha-file"


def test_engine_no_evidence_contract_bypasses_legacy_zero_source_cards(monkeypatch):
    retrieval_candidate, contract, authority_state = _engine_contract_bypass_legacy_fixture(
        engine_status="no_evidence",
        engine_terminal_reason="no_accepted_evidence",
        include_evidence=False,
        diagnostics=[
            {
                "classification": "no_evidence",
                "reason": "no_accepted_evidence",
                "outcome": "no_evidence",
            }
        ],
    )

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def emit_event(_event):
        return None

    legacy_calls = {"count": 0}

    async def raise_legacy_provider(*_args, **_kwargs):
        legacy_calls["count"] += 1
        raise AssertionError("legacy provider must be bypassed on engine no_evidence")

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        raise_legacy_provider,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_strategy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware strategy must be bypassed on engine no_evidence")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        lambda **_kwargs: {
            "authority": {"state": authority_state, "reason": "no_accepted_evidence"},
            "contract": contract,
            "observe": {
                "mode": "observe_parallel",
                "lane": "selected_file_text",
                "authority": "retrieval_engine",
                "status": "no_evidence",
                "terminal_reason": "no_accepted_evidence",
                "plan_summary": {"evidence_shape": "narrow_chunk"},
                "counts": {
                    "candidate_count": 0,
                    "evidence_bundle_count": 0,
                    "accepted_output_count": 0,
                    "reference_count": 0,
                    "diagnostic_count": 1,
                },
                "diagnostics": [
                    {
                        "classification": "no_evidence",
                        "reason": "no_accepted_evidence",
                        "outcome": "no_evidence",
                    }
                ],
            },
        },
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert legacy_calls["count"] == 0
    assert flags["status"] == "no_evidence"
    assert not flags["sources"]
    assert not flags.get("accepted_outputs")
    assert flags["retrieval_diagnostics"]


def test_engine_denied_contract_bypasses_legacy_zero_source_cards(monkeypatch):
    for denied_status in ("denied", "blocked"):
        retrieval_candidate, contract, authority_state = _engine_contract_bypass_legacy_fixture(
            engine_status=denied_status,
            engine_terminal_reason="source_scope_violation",
            include_evidence=False,
            diagnostics=[
                {
                    "classification": denied_status,
                    "reason": "source_scope_violation",
                    "outcome": denied_status,
                }
            ],
        )

        async def fake_prepare_files(*_args, **_kwargs):
            return [retrieval_candidate], [], [], [retrieval_candidate], []

        async def emit_event(_event):
            return None

        legacy_calls = {"count": 0}

        async def raise_legacy_provider(*_args, **_kwargs):
            legacy_calls["count"] += 1
            raise AssertionError(f"legacy provider must be bypassed on engine {denied_status}")

        monkeypatch.setattr(
            "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
            fake_prepare_files,
        )
        monkeypatch.setattr(
            "open_webui.utils.middleware.get_sources_from_items",
            raise_legacy_provider,
        )
        monkeypatch.setattr(
            "open_webui.utils.middleware.ensure_retrieval_runtime",
            lambda _app: None,
        )
        monkeypatch.setattr(
            "open_webui.utils.middleware._first_pass_selected_source_strategy",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"middleware strategy must be bypassed on engine {denied_status}")
            ),
        )
        monkeypatch.setattr(
            "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
            lambda **_kwargs: {
                "authority": {"state": authority_state, "reason": "source_scope_violation"},
                "contract": contract,
                "observe": {
                    "mode": "observe_parallel",
                    "lane": "selected_file_text",
                    "authority": "retrieval_engine",
                    "status": denied_status,
                    "terminal_reason": "source_scope_violation",
                    "plan_summary": {"evidence_shape": "narrow_chunk"},
                    "counts": {
                        "candidate_count": 0,
                        "evidence_bundle_count": 0,
                        "accepted_output_count": 0,
                        "reference_count": 0,
                        "diagnostic_count": 1,
                    },
                    "diagnostics": [
                        {
                            "classification": denied_status,
                            "reason": "source_scope_violation",
                            "outcome": denied_status,
                        }
                    ],
                },
            },
        )

        body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "what is transformer grounding"}],
            "metadata": {
                "files": [retrieval_candidate],
                "execution_profile_routing_diagnostics": {
                    "classified_evidence_need": "narrow_fact"
                },
            },
        }

        _, flags = asyncio.run(
            chat_completion_files_handler(
                _first_pass_retrieval_request_stub(),
                body,
                {"__event_emitter__": emit_event},
                SimpleNamespace(id="user-1"),
            )
        )

        assert legacy_calls["count"] == 0, f"legacy ran for {denied_status}"
        assert flags["status"] == denied_status, f"wrong status for {denied_status}"
        assert not flags["sources"], f"sources exist for {denied_status}"
        assert not flags.get("accepted_outputs"), f"accepted_outputs exist for {denied_status}"


def test_invalid_present_engine_contract_fails_closed_without_legacy(monkeypatch):
    retrieval_candidate, invalid_contract, authority_state = (
        _engine_contract_bypass_legacy_fixture(
            engine_status="denied",
            engine_terminal_reason="source_scope_violation",
            include_evidence=True,
            authority_state="fail_closed",
        )
    )

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def emit_event(_event):
        return None

    legacy_calls = {"count": 0}

    async def raise_legacy_provider(*_args, **_kwargs):
        legacy_calls["count"] += 1
        raise AssertionError("legacy provider must not run for invalid engine contract")

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        raise_legacy_provider,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_strategy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("first-pass strategy must be bypassed for invalid contract")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        lambda **_kwargs: {
            "authority": {"state": authority_state, "reason": "source_scope_violation"},
            "contract": invalid_contract,
            "observe": {
                "mode": "observe_parallel",
                "lane": "selected_file_text",
                "authority": "retrieval_engine",
                "status": "denied",
                "terminal_reason": "source_scope_violation",
                "plan_summary": {"evidence_shape": "narrow_chunk"},
                "counts": {
                    "candidate_count": 1,
                    "evidence_bundle_count": 1,
                    "accepted_output_count": 1,
                    "reference_count": 1,
                    "diagnostic_count": 0,
                },
                "diagnostics": [],
            },
        },
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert legacy_calls["count"] == 0
    assert flags["status"] == "error"
    assert flags["terminal_reason"] == "invalid_engine_contract"
    assert flags["accepted_outputs"] == []
    assert flags["references"] == []
    assert flags["sources"] == []
    assert _diagnostic_reason_set(flags) == {"invalid_engine_contract"}
    attempt = flags["retrieval_engine_attempt"]
    assert attempt["engine_invoked"] is True
    assert attempt["runtime_mode"] == "engine_owned"
    assert attempt["failure_class"] == "invalid_engine_contract"


def test_engine_timeout_and_error_contracts_bypass_legacy_zero_source_cards(
    monkeypatch,
):
    for status, terminal_reason in (
        ("timeout", "retrieval_timeout"),
        ("error", "engine_runtime_error"),
    ):
        retrieval_candidate, contract, authority_state = (
            _engine_contract_bypass_legacy_fixture(
                engine_status=status,
                engine_terminal_reason=terminal_reason,
                include_evidence=False,
                diagnostics=[
                    {
                        "classification": "no_evidence",
                        "reason": terminal_reason,
                        "outcome": status,
                    }
                ],
            )
        )

        async def fake_prepare_files(*_args, **_kwargs):
            return [retrieval_candidate], [], [], [retrieval_candidate], []

        async def emit_event(_event):
            return None

        legacy_calls = {"count": 0}

        async def raise_legacy_provider(*_args, **_kwargs):
            legacy_calls["count"] += 1
            raise AssertionError(f"legacy provider must be bypassed on engine {status}")

        monkeypatch.setattr(
            "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
            fake_prepare_files,
        )
        monkeypatch.setattr(
            "open_webui.utils.middleware.get_sources_from_items",
            raise_legacy_provider,
        )
        monkeypatch.setattr(
            "open_webui.utils.middleware.ensure_retrieval_runtime",
            lambda _app: None,
        )
        monkeypatch.setattr(
            "open_webui.utils.middleware._first_pass_selected_source_strategy",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"strategy must be bypassed on engine {status}")
            ),
        )
        monkeypatch.setattr(
            "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
            lambda **_kwargs: {
                "authority": {"state": authority_state, "reason": terminal_reason},
                "contract": contract,
                "observe": {
                    "mode": "observe_parallel",
                    "lane": "selected_file_text",
                    "authority": "retrieval_engine",
                    "status": status,
                    "terminal_reason": terminal_reason,
                    "plan_summary": {"evidence_shape": "narrow_chunk"},
                    "counts": {
                        "candidate_count": 0,
                        "evidence_bundle_count": 0,
                        "accepted_output_count": 0,
                        "reference_count": 0,
                        "diagnostic_count": 1,
                    },
                    "diagnostics": [],
                },
            },
        )

        body = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "what is transformer grounding"}],
            "metadata": {
                "files": [retrieval_candidate],
                "execution_profile_routing_diagnostics": {
                    "classified_evidence_need": "narrow_fact"
                },
            },
        }

        _, flags = asyncio.run(
            chat_completion_files_handler(
                _first_pass_retrieval_request_stub(),
                body,
                {"__event_emitter__": emit_event},
                SimpleNamespace(id="user-1"),
            )
        )

        assert legacy_calls["count"] == 0
        assert flags["status"] == status
        assert flags["terminal_reason"] == terminal_reason
        assert flags["sources"] == []
        assert flags["accepted_outputs"] == []
        assert flags["retrieval_engine_attempt"]["engine_invoked"] is True
        assert flags["retrieval_engine_attempt"]["terminal_status"] == status


def test_engine_diagnostics_only_contract_bypasses_legacy_source_publication(monkeypatch):
    retrieval_candidate, contract, authority_state = _engine_contract_bypass_legacy_fixture(
        engine_status="no_evidence",
        engine_terminal_reason="rejected_candidates_only",
        include_evidence=False,
        diagnostics=[
            {
                "classification": "diagnostics",
                "reason": "rejected_candidates_only",
                "outcome": "no_evidence",
            }
        ],
    )

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def emit_event(_event):
        return None

    legacy_calls = {"count": 0}

    async def raise_legacy_provider(*_args, **_kwargs):
        legacy_calls["count"] += 1
        raise AssertionError("legacy provider must be bypassed on diagnostics-only")

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        raise_legacy_provider,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._first_pass_selected_source_strategy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("middleware strategy must be bypassed on diagnostics-only")
        ),
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        lambda **_kwargs: {
            "authority": {"state": authority_state, "reason": "rejected_candidates_only"},
            "contract": contract,
            "observe": {
                "mode": "observe_parallel",
                "lane": "selected_file_text",
                "authority": "retrieval_engine",
                "status": "no_evidence",
                "terminal_reason": "rejected_candidates_only",
                "plan_summary": {"evidence_shape": "narrow_chunk"},
                "counts": {
                    "candidate_count": 2,
                    "evidence_bundle_count": 0,
                    "accepted_output_count": 0,
                    "reference_count": 0,
                    "diagnostic_count": 1,
                },
                "diagnostics": [
                    {
                        "classification": "diagnostics",
                        "reason": "rejected_candidates_only",
                        "outcome": "no_evidence",
                    }
                ],
            },
        },
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert legacy_calls["count"] == 0
    assert flags["status"] == "no_evidence"
    assert not flags["sources"]
    assert not flags.get("accepted_outputs")


def test_missing_engine_contract_allows_compatibility_fallback(monkeypatch):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "meta": {"content_type": "text/plain"},
    }

    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="Legacy compatibility fallback evidence.",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )

    # Engine returns fallback (adapter error) — no contract key
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        lambda **_kwargs: {
            "authority": {"state": "fallback", "reason": "adapter_unavailable"},
            "observe": {
                "mode": "observe_parallel",
                "lane": "selected_file_text",
                "authority": "legacy_selected_source",
                "status": "error",
                "terminal_reason": "engine_observe_error",
                "plan_summary": {},
                "counts": {
                    "candidate_count": 0,
                    "evidence_bundle_count": 0,
                    "accepted_output_count": 0,
                    "reference_count": 0,
                    "diagnostic_count": 0,
                },
                "diagnostics": [],
            },
        },
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 1
    assert flags["sources"]
    assert flags["sources"][0]["source"]["id"] == "alpha-file"
    assert flags["sources"][0]["source"]["origin"] == "legacy_fallback"
    strategy = flags.get("first_pass_retrieval_strategy") or {}
    assert strategy.get("compatibility_fallback") is True
    assert strategy.get("retrieval_runtime_mode") == "legacy_fallback"
    assert strategy.get("fallback_reason") == "adapter_unavailable"
    attempt = flags["retrieval_engine_attempt"]
    assert attempt["engine_invoked"] is False
    assert attempt["runtime_mode"] == "legacy_fallback"
    assert attempt["fallback_reason"] == "adapter_unavailable"
    assert attempt["failure_class"] == "adapter_unavailable"


def test_unsupported_source_shape_allows_marked_compatibility_fallback(monkeypatch):
    retrieval_candidate = {
        "id": "alpha-file",
        "name": "alpha-policy.txt",
        "context": "full",
        "type": "text",
        "collection_name": "alpha-file",
        "meta": {"content_type": "text/plain"},
    }

    provider_calls = {"count": 0}

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def fake_get_sources_from_items(*_args, **_kwargs):
        provider_calls["count"] += 1
        return [
            _local_file_source(
                file_id="alpha-file",
                name="alpha-policy.txt",
                content="Legacy fallback evidence for unsupported source shape.",
            )
        ]

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        fake_get_sources_from_items,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        lambda **_kwargs: {
            "authority": {"state": "fallback", "reason": "unsupported_source_shape"},
            "observe": {
                "mode": "observe_parallel",
                "lane": "selected_file_text",
                "authority": "legacy_selected_source",
                "status": "no_evidence",
                "terminal_reason": "unsupported_source_shape",
                "plan_summary": {},
                "counts": {
                    "candidate_count": 0,
                    "evidence_bundle_count": 0,
                    "accepted_output_count": 0,
                    "reference_count": 0,
                    "diagnostic_count": 1,
                },
                "diagnostics": [],
            },
        },
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert provider_calls["count"] == 1
    assert flags["sources"][0]["source"]["origin"] == "legacy_fallback"
    strategy = flags.get("first_pass_retrieval_strategy") or {}
    assert strategy.get("retrieval_runtime_mode") == "legacy_fallback"
    assert strategy.get("fallback_reason") == "unsupported_source_shape"
    assert flags["retrieval_engine_attempt"]["fallback_reason"] == (
        "unsupported_source_shape"
    )


def test_engine_contract_always_stored_in_metadata_when_invoked(monkeypatch):
    retrieval_candidate, contract, authority_state = _engine_contract_bypass_legacy_fixture(
        engine_status="no_evidence",
        engine_terminal_reason="no_accepted_evidence",
        include_evidence=False,
        diagnostics=[
            {
                "classification": "no_evidence",
                "reason": "no_accepted_evidence",
                "outcome": "no_evidence",
            }
        ],
    )

    async def fake_prepare_files(*_args, **_kwargs):
        return [retrieval_candidate], [], [], [retrieval_candidate], []

    async def emit_event(_event):
        return None

    monkeypatch.setattr(
        "open_webui.utils.middleware._prepare_chat_files_for_retrieval",
        fake_prepare_files,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.get_sources_from_items",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware.ensure_retrieval_runtime",
        lambda _app: None,
    )
    monkeypatch.setattr(
        "open_webui.utils.middleware._retrieval_engine_selected_source_lane_package",
        lambda **_kwargs: {
            "authority": {"state": authority_state, "reason": "no_accepted_evidence"},
            "contract": contract,
            "observe": {
                "mode": "observe_parallel",
                "lane": "selected_file_text",
                "authority": "retrieval_engine",
                "status": "no_evidence",
                "terminal_reason": "no_accepted_evidence",
                "plan_summary": {"evidence_shape": "narrow_chunk"},
                "counts": {
                    "candidate_count": 0,
                    "evidence_bundle_count": 0,
                    "accepted_output_count": 0,
                    "reference_count": 0,
                    "diagnostic_count": 1,
                },
                "diagnostics": [],
            },
        },
    )

    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "what is transformer grounding"}],
        "metadata": {
            "files": [retrieval_candidate],
            "execution_profile_routing_diagnostics": {
                "classified_evidence_need": "narrow_fact"
            },
        },
    }

    _, flags = asyncio.run(
        chat_completion_files_handler(
            _first_pass_retrieval_request_stub(),
            body,
            {"__event_emitter__": emit_event},
            SimpleNamespace(id="user-1"),
        )
    )

    assert flags.get("retrieval_engine_first_pass_contract") or flags.get(
        "retrieval_engine_contract"
    ), "engine contract must be stored in metadata when engine was invoked"


# ---------------------------------------------------------------------------
# Fixture pack wiring
# ---------------------------------------------------------------------------


def test_fixture_pack_is_available():
    """Verify the controlled fixture pack from retrieval-engine is importable."""
    assert _HAS_FIXTURE_PACK, (
        "retrieval-engine/tests/fixtures/local_file_text/ must be importable. "
        "Ensure retrieval-engine is on sys.path (e.g. pip install -e retrieval-engine)."
    )


def test_fixture_pack_contract_matches_local_contract():
    """Verify the fixture pack produces the same contract shape as the local helper."""
    if not _HAS_FIXTURE_PACK:
        return  # skip if fixture pack not available
    local_contract = _engine_authority_tool_contract(
        status="success",
        terminal_reason="success",
        include_evidence=True,
    )
    pack_contract = _engine_authority_contract_fixture(
        status="success",
        terminal_reason="success",
        include_evidence=True,
    )
    core_contract_keys = {
        "status",
        "terminal_reason",
        "accepted_outputs",
        "references",
        "retrieval_diagnostics",
        "authorization_context",
        "retry_policy",
        "context_budget",
        "provenance",
    }
    assert core_contract_keys.issubset(local_contract.keys())
    assert core_contract_keys.issubset(pack_contract.keys())
    assert {
        "contract_version",
        "engine_version",
        "boundary_owned",
        "boundary_owner",
        "source_scope_echo",
    }.issubset(local_contract.keys())
    assert local_contract["status"] == pack_contract["status"]
    assert len(local_contract["references"]) == len(pack_contract["references"])
    assert len(local_contract["accepted_outputs"]) == len(pack_contract["accepted_outputs"])


# ---------------------------------------------------------------------------
# Boundary contract: tool-side scope binding
# ---------------------------------------------------------------------------


def test_tool_side_boundary_contract_missing_scope_binding_fails_closed(monkeypatch):
    """Metadata contract present but caller and attempt both lack scope binding → fail closed."""
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)

    contract = _engine_authority_tool_contract()
    metadata = _trusted_engine_tool_metadata(contract)
    # Strip all scope binding: caller IDs AND attempt IDs
    metadata["retrieval_engine_attempt"].pop("turn_id", None)
    metadata["retrieval_engine_attempt"].pop("message_id", None)
    metadata.pop("turn_id", None)
    metadata.pop("message_id", None)

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=metadata,
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "error"
    assert response["terminal_reason"] == "invalid_engine_contract"
    assert response["provenance"]["failure_class"] == "invalid_engine_contract"
    invalid_reasons = response["provenance"].get("invalid_reasons", [])
    assert "missing_current_turn_scope_binding" in invalid_reasons
    assert "missing_boundary_attempt_scope_binding" in invalid_reasons


def test_tool_side_boundary_contract_stale_replay_fails_closed(monkeypatch):
    """A contract from a different turn/message must fail closed as stale replay."""
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)

    contract = _engine_authority_tool_contract()
    metadata = _trusted_engine_tool_metadata(contract)
    # Bind attempt to a DIFFERENT turn/message
    metadata["retrieval_engine_attempt"]["turn_id"] = "turn-other-999"
    metadata["retrieval_engine_attempt"]["message_id"] = "msg-other-999"
    # Current turn/message context
    metadata["turn_id"] = "turn-current-42"
    metadata["message_id"] = "msg-current-42"

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=metadata,
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "error"
    assert response["terminal_reason"] == "invalid_engine_contract"
    assert response["provenance"]["failure_class"] == "invalid_engine_contract"
    invalid_reasons = response["provenance"].get("invalid_reasons", [])
    assert "stale_boundary_attempt_turn_mismatch" in invalid_reasons


def test_tool_side_boundary_contract_forged_marker_fails_closed(monkeypatch):
    """Forged trust markers (wrong boundary_owner) must fail closed."""
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)

    contract = _engine_authority_tool_contract()
    metadata = _trusted_engine_tool_metadata(contract)
    # Forge the boundary_owner to a wrong value
    metadata["retrieval_engine_attempt"]["boundary_owner"] = "forged.owner.module"
    metadata["retrieval_engine_attempt"]["turn_id"] = "turn-current-42"
    metadata["retrieval_engine_attempt"]["message_id"] = "msg-current-42"
    metadata["turn_id"] = "turn-current-42"
    metadata["message_id"] = "msg-current-42"

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=metadata,
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "error"
    assert response["terminal_reason"] == "invalid_engine_contract"
    assert response["provenance"]["failure_class"] == "invalid_engine_contract"
    assert "forged_boundary_attempt_metadata" in response["provenance"].get(
        "invalid_reasons", []
    )


def test_tool_side_boundary_contract_same_turn_valid_suppresses_legacy(monkeypatch):
    """A valid same-turn contract must suppress legacy fallback and return engine evidence."""
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)

    contract = _engine_authority_tool_contract()
    metadata = _trusted_engine_tool_metadata(contract)
    # Bind attempt to the SAME turn/message as the current context
    metadata["retrieval_engine_attempt"]["turn_id"] = "turn-current-42"
    metadata["retrieval_engine_attempt"]["message_id"] = "msg-current-42"
    metadata["turn_id"] = "turn-current-42"
    metadata["message_id"] = "msg-current-42"

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=metadata,
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success"
    assert response["canonical_references"][0]["source"]["id"] == "alpha-file"
    assert response["accepted_outputs"][0]["snippet"] == (
        "Engine accepted evidence for Atlas policy."
    )
    assert response["strategy_used"]["retrieval_strategy"] == "retrieval_engine_authority"
    assert response["provenance"]["tool_handler_policy_bypassed"] is True
    assert response["provenance"]["tool_handler_bypass_reason"] == (
        "retrieval_engine_authority_succeeded"
    )


def test_tool_side_boundary_contract_partial_binding_turn_only_fails_closed(monkeypatch):
    """Attempt has matching turn_id but missing message_id → fail closed."""
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)

    contract = _engine_authority_tool_contract()
    metadata = _trusted_engine_tool_metadata(contract)
    # Attempt has turn_id but no message_id — partial binding
    metadata["retrieval_engine_attempt"]["turn_id"] = "turn-current-42"
    metadata["retrieval_engine_attempt"].pop("message_id", None)
    metadata["turn_id"] = "turn-current-42"
    metadata["message_id"] = "msg-current-42"

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=metadata,
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "error"
    assert response["terminal_reason"] == "invalid_engine_contract"
    assert response["provenance"]["failure_class"] == "invalid_engine_contract"
    assert "missing_boundary_attempt_scope_binding" in response["provenance"].get(
        "invalid_reasons", []
    )


def test_tool_side_boundary_contract_partial_binding_message_only_fails_closed(monkeypatch):
    """Attempt has matching message_id but missing turn_id → fail closed."""
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)

    contract = _engine_authority_tool_contract()
    metadata = _trusted_engine_tool_metadata(contract)
    # Attempt has message_id but no turn_id — partial binding
    metadata["retrieval_engine_attempt"].pop("turn_id", None)
    metadata["retrieval_engine_attempt"]["message_id"] = "msg-current-42"
    metadata["turn_id"] = "turn-current-42"
    metadata["message_id"] = "msg-current-42"

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=metadata,
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "error"
    assert response["terminal_reason"] == "invalid_engine_contract"
    assert response["provenance"]["failure_class"] == "invalid_engine_contract"
    assert "missing_boundary_attempt_scope_binding" in response["provenance"].get(
        "invalid_reasons", []
    )


def test_tool_side_boundary_contract_missing_caller_scope_fails_closed(monkeypatch):
    """Top-level metadata lacks turn_id/message_id while contract/attempt is present → fail closed."""
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)

    contract = _engine_authority_tool_contract()
    metadata = _trusted_engine_tool_metadata(contract)
    # Attempt has scope binding, but caller (top-level metadata) does not
    metadata["retrieval_engine_attempt"]["turn_id"] = "turn-current-42"
    metadata["retrieval_engine_attempt"]["message_id"] = "msg-current-42"
    metadata.pop("turn_id", None)
    metadata.pop("message_id", None)

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=metadata,
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "error"
    assert response["terminal_reason"] == "invalid_engine_contract"
    assert response["provenance"]["failure_class"] == "invalid_engine_contract"
    assert "missing_current_turn_scope_binding" in response["provenance"].get(
        "invalid_reasons", []
    )


def test_tool_side_boundary_contract_valid_same_turn_suppresses_legacy(monkeypatch):
    """Valid same-turn/same-message contract suppresses legacy fallback."""
    _patch_selected_source_legacy_policy_helpers_to_raise(monkeypatch)

    contract = _engine_authority_tool_contract()
    metadata = _trusted_engine_tool_metadata(contract)
    # Both caller and attempt carry matching scope IDs
    metadata["retrieval_engine_attempt"]["turn_id"] = "turn-current-42"
    metadata["retrieval_engine_attempt"]["message_id"] = "msg-current-42"
    metadata["turn_id"] = "turn-current-42"
    metadata["message_id"] = "msg-current-42"

    response = asyncio.run(
        query_selected_knowledge_files(
            query="What does Atlas say?",
            source_ids=["alpha-file"],
            __request__=_selected_source_tool_request_stub(),
            __files__=[],
            __metadata__=metadata,
            __user_model__=SimpleNamespace(id="user-1"),
        )
    )

    assert response["status"] == "success"
    assert response["canonical_references"][0]["source"]["id"] == "alpha-file"
    assert response["accepted_outputs"][0]["snippet"] == (
        "Engine accepted evidence for Atlas policy."
    )
    assert response["strategy_used"]["retrieval_strategy"] == "retrieval_engine_authority"
    assert response["provenance"]["tool_handler_policy_bypassed"] is True
    assert response["provenance"]["tool_handler_bypass_reason"] == (
        "retrieval_engine_authority_succeeded"
    )
    assert "invalid_reasons" not in response["provenance"]


# ---------------------------------------------------------------------------
# Latency budget: typed timeout contract
# ---------------------------------------------------------------------------


def test_engine_invocation_timeout_returns_typed_timeout_contract(monkeypatch):
    """Engine that exceeds latency budget returns typed timeout contract with zero references."""
    import time

    # Use a very short timeout to keep the test fast
    monkeypatch.setattr(
        "open_webui.utils.middleware.ENGINE_INVOCATION_TIMEOUT_SECONDS",
        0.1,
    )

    class _SlowEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _request):
            time.sleep(2)
            return RetrievalResult(
                status="success",
                request=RetrievalRequest(
                    query="engine query",
                    scope=AuthorizedScope(decision="authorized", source_ids=("alpha-file",)),
                    sources=(),
                ),
                accepted_outputs=(
                    AcceptedOutput(
                        output_id="accepted:alpha",
                        text="should not be returned",
                        evidence_bundle_ids=("bundle:alpha",),
                    ),
                ),
                references=(
                    Reference(
                        reference_id="reference:alpha",
                        source_id="alpha-file",
                        label="alpha-policy.txt",
                        source_anchor={"kind": "text_span", "source_id": "alpha-file"},
                    ),
                ),
            )

    monkeypatch.setattr("retrieval_engine.RetrievalEngine", _SlowEngine)

    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {
                    "content": "Transformer grounding aligns model outputs with source text."
                },
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    contract = package["contract"]
    assert contract["status"] == "timeout"
    assert contract["terminal_reason"] == "retrieval_timeout"
    assert contract["accepted_outputs"] == []
    assert contract["references"] == []
    assert contract["sources"] == []
    assert contract["provenance"]["engine_owned"] is True
    assert contract["provenance"]["compatibility_fallback"] is False
    assert contract["provenance"]["failure_class"] == "retrieval_timeout"
    assert contract["retry_policy"]["timeout_seconds"] == 0.1


def test_engine_invocation_timeout_suppresses_legacy_fallback(monkeypatch):
    """Timeout contract must not be eligible for legacy fallback."""
    import time

    monkeypatch.setattr(
        "open_webui.utils.middleware.ENGINE_INVOCATION_TIMEOUT_SECONDS",
        0.1,
    )

    class _SlowEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _request):
            time.sleep(2)
            return RetrievalResult(
                status="success",
                request=RetrievalRequest(
                    query="engine query",
                    scope=AuthorizedScope(decision="authorized", source_ids=("alpha-file",)),
                    sources=(),
                ),
            )

    monkeypatch.setattr("retrieval_engine.RetrievalEngine", _SlowEngine)

    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {"content": "Transformer grounding aligns model outputs with source text."},
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    assert package["authority"]["state"] == "fail_closed"
    assert package["authority"]["reason"] == "retrieval_timeout"
    assert package["attempt"]["engine_invoked"] is True
    assert package["attempt"]["failure_class"] == "retrieval_timeout"
    assert package["attempt"]["terminal_status"] == "timeout"
    assert package["attempt"]["terminal_reason"] == "retrieval_timeout"
    assert package["attempt"]["runtime_mode"] == "retrieval_engine_fail_closed"


def test_engine_invocation_timeout_persisted_attempt_metadata(monkeypatch):
    """Timeout attempt metadata is persisted with runtime_mode and terminal fields."""
    import time

    monkeypatch.setattr(
        "open_webui.utils.middleware.ENGINE_INVOCATION_TIMEOUT_SECONDS",
        0.1,
    )

    class _SlowEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _request):
            time.sleep(2)
            return RetrievalResult(
                status="success",
                request=RetrievalRequest(
                    query="engine query",
                    scope=AuthorizedScope(decision="authorized", source_ids=("alpha-file",)),
                    sources=(),
                ),
            )

    monkeypatch.setattr("retrieval_engine.RetrievalEngine", _SlowEngine)

    metadata = {}
    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {"content": "Transformer grounding aligns model outputs with source text."},
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[],
        legacy_references=[],
        legacy_accepted_outputs=[],
    )

    from open_webui.retrieval.engine_contract import persist_engine_attempt
    persist_engine_attempt(metadata, {
        "engine_owned": True,
        "contract": package["contract"],
        "attempt_metadata": package["attempt"],
    })

    attempt = metadata["retrieval_engine_attempt"]
    assert attempt["engine_invoked"] is True
    assert attempt["terminal_status"] == "timeout"
    assert attempt["terminal_reason"] == "retrieval_timeout"
    assert attempt["runtime_mode"] == "retrieval_engine_fail_closed"
    assert attempt["failure_class"] == "retrieval_timeout"
    assert attempt["boundary_owned"] is True


def test_engine_invocation_timeout_observe_has_zero_counts(monkeypatch):
    """Timeout observe metadata has zero accepted output and reference counts."""
    import time

    monkeypatch.setattr(
        "open_webui.utils.middleware.ENGINE_INVOCATION_TIMEOUT_SECONDS",
        0.1,
    )

    class _SlowEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _request):
            time.sleep(2)
            return RetrievalResult(
                status="success",
                request=RetrievalRequest(
                    query="engine query",
                    scope=AuthorizedScope(decision="authorized", source_ids=("alpha-file",)),
                    sources=(),
                ),
            )

    monkeypatch.setattr("retrieval_engine.RetrievalEngine", _SlowEngine)

    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {"content": "Transformer grounding aligns model outputs with source text."},
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    observe = package["observe"]
    assert observe["status"] == "timeout"
    assert observe["terminal_reason"] == "retrieval_timeout"
    assert observe["authority"] == "retrieval_engine"
    assert observe["counts"]["accepted_output_count"] == 0
    assert observe["counts"]["reference_count"] == 0
    assert observe["diagnostics"][0]["code"] == "retrieval_timeout"


def test_engine_invocation_timeout_returns_promptly_in_wall_clock(monkeypatch):
    """Timeout path must return well before the slow engine finishes (no blocking shutdown)."""
    import time

    monkeypatch.setattr(
        "open_webui.utils.middleware.ENGINE_INVOCATION_TIMEOUT_SECONDS",
        0.2,
    )

    class _VerySlowEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _request):
            time.sleep(10)
            return RetrievalResult(
                status="success",
                request=RetrievalRequest(
                    query="engine query",
                    scope=AuthorizedScope(decision="authorized", source_ids=("alpha-file",)),
                    sources=(),
                ),
            )

    monkeypatch.setattr("retrieval_engine.RetrievalEngine", _VerySlowEngine)

    start = time.monotonic()
    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {"content": "Transformer grounding aligns model outputs with source text."},
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )
    elapsed = time.monotonic() - start

    assert package["contract"]["status"] == "timeout"
    assert elapsed < 3.0, (
        f"Timeout path took {elapsed:.1f}s — must return in <3s, "
        "not block on executor shutdown"
    )


def test_pre_invocation_fallback_reasons_still_bypass_engine(monkeypatch):
    """adapter_unavailable, request_construction_error, unsupported_source_shape remain pre-invocation fallbacks."""
    # adapter_unavailable: fail to import
    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {"content": "Transformer grounding aligns model outputs with source text."},
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    # If engine import works (it does in test env), this tests request_construction_error
    # by failing the adapter
    def fail_adapter(*_args, **_kwargs):
        raise RuntimeError("adapter unavailable")

    monkeypatch.setattr(
        "open_webui.retrieval.engine_adapter.build_retrieval_engine_request",
        fail_adapter,
    )

    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {"content": "Transformer grounding aligns model outputs with source text."},
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[_local_file_source(file_id="alpha-file")],
        legacy_references=[{"source": {"id": "alpha-file"}}],
        legacy_accepted_outputs=[{"output_id": "legacy:alpha-file"}],
    )

    assert package["authority"]["state"] == "fallback"
    assert package["authority"]["reason"] == "request_construction_error"
    assert package["attempt"]["engine_invoked"] is False
    assert package["attempt"]["fallback_reason"] == "request_construction_error"


def test_engine_fallback_reasons_remain_closed_enum():
    assert ENGINE_FALLBACK_REASONS == {
        "adapter_unavailable",
        "request_construction_error",
        "unsupported_source_shape",
        "operator_disabled",
    }


def test_engine_answer_policy_covers_all_terminal_statuses():
    assert set(ENGINE_ANSWER_POLICIES) == set(ENGINE_TERMINAL_STATUSES)


def test_selected_source_engine_operator_disabled_is_marked_fallback(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_ENGINE_SELECTED_SOURCE_DISABLED", "1")
    assert selected_source_engine_operator_disabled() is True

    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is transformer grounding",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "data": {
                    "content": "Transformer grounding aligns model outputs with source text."
                },
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[],
        legacy_references=[],
        legacy_accepted_outputs=[],
    )

    assert package["authority"] == {
        "state": "fallback",
        "reason": "operator_disabled",
    }
    assert package["attempt"]["engine_invoked"] is False
    assert package["attempt"]["fallback_reason"] == "operator_disabled"
    assert "contract" not in package

    classification = classify_engine_attempt(
        package=package,
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
    )
    assert classification["engine_owned"] is False
    assert classification["fallback_eligible"] is True
    assert classification["fallback_reason"] == "operator_disabled"
    assert classification["retrieval_runtime_mode"] == "legacy_fallback"
    assert classification["contract_present"] is False


def test_selected_source_engine_operator_switch_defaults_enabled(monkeypatch):
    monkeypatch.delenv("RETRIEVAL_ENGINE_SELECTED_SOURCE_DISABLED", raising=False)
    assert selected_source_engine_operator_disabled() is False
    monkeypatch.setenv("RETRIEVAL_ENGINE_SELECTED_SOURCE_DISABLED", "0")
    assert selected_source_engine_operator_disabled() is False


def test_selected_source_all_files_textless_is_engine_owned_no_evidence():
    active_source_scope = {
        "status": "resolved",
        "source_ids": ["alpha-file", "beta-file"],
    }
    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is the allowed tolerance",
        selected_files=[
            {"id": "alpha-file", "name": "alpha-policy.txt", "data": {"content": ""}},
            {"id": "beta-file", "name": "beta-policy.txt", "data": {"content": "   "}},
        ],
        active_source_scope=active_source_scope,
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[],
        legacy_references=[],
        legacy_accepted_outputs=[],
    )

    assert package["authority"]["state"] != "fallback"
    assert package["attempt"]["engine_invoked"] is True
    contract = package["contract"]
    assert contract["status"] == "no_evidence"
    assert contract["references"] == []
    assert contract["accepted_outputs"] == []
    assert contract["sources"] == []

    classification = classify_engine_attempt(
        package=package,
        active_source_scope=active_source_scope,
    )
    assert classification["engine_owned"] is True
    assert classification["valid"] is True
    assert classification["fallback_eligible"] is False
    assert classification["terminal_status"] == "no_evidence"
    assert classification["answer_policy"] == "report_no_evidence"
    assert classification["attempt_metadata"]["engine_invoked"] is True
    assert classification["attempt_metadata"]["contract_present"] is True


def test_selected_source_collection_shape_without_host_text_still_falls_back():
    package = _retrieval_engine_selected_source_lane_package(
        prompt="what is the allowed tolerance",
        selected_files=[
            {
                "id": "alpha-file",
                "name": "alpha-policy.txt",
                "type": "text",
                "collection_name": "alpha-file",
                "meta": {"content_type": "text/plain"},
            }
        ],
        active_source_scope={"status": "resolved", "source_ids": ["alpha-file"]},
        legacy_status="success",
        legacy_terminal_reason="success",
        legacy_sources=[],
        legacy_references=[],
        legacy_accepted_outputs=[],
    )

    assert package["authority"]["state"] == "fallback"
    assert package["authority"]["reason"] == "unsupported_source_shape"
    assert package["attempt"]["engine_invoked"] is False
    assert package["attempt"]["fallback_reason"] == "unsupported_source_shape"


def test_selected_source_retrieval_telemetry_counts_modes():
    reset_selected_source_retrieval_telemetry()
    scope = {"status": "resolved", "source_ids": ["alpha-file"]}

    success = classify_engine_attempt(
        contract=_engine_authority_tool_contract(
            status="success", terminal_reason="success", include_evidence=True
        ),
        active_source_scope=scope,
    )
    snapshot = record_selected_source_retrieval_telemetry(
        classification=success,
        active_sources=[{"source": {"id": "alpha-file"}}],
    )
    assert snapshot["runtime_mode"] == "engine_owned"
    assert snapshot["terminal_status"] == "success"
    assert snapshot["accepted_reference_count"] == 1
    assert snapshot["legacy_origin_source_card_count"] == 0
    assert snapshot["boundary_violation"] is False
    assert snapshot["answer_policy"] == "answer_from_accepted_evidence"
    assert snapshot["source_shape"] == "local_file_text"

    no_evidence = classify_engine_attempt(
        contract=_engine_authority_tool_contract(
            status="no_evidence",
            terminal_reason="rejected_candidates_only",
            include_evidence=False,
        ),
        active_source_scope=scope,
    )
    snapshot = record_selected_source_retrieval_telemetry(
        classification=no_evidence, active_sources=[]
    )
    assert snapshot["terminal_status"] == "no_evidence"
    assert snapshot["accepted_reference_count"] == 0
    assert snapshot["answer_policy"] == "report_no_evidence"
    assert snapshot["boundary_violation"] is False

    timeout = classify_engine_attempt(
        contract=_engine_authority_tool_contract(
            status="timeout",
            terminal_reason="post_invocation_timeout",
            include_evidence=False,
        ),
        active_source_scope=scope,
    )
    snapshot = record_selected_source_retrieval_telemetry(
        classification=timeout, active_sources=[]
    )
    assert snapshot["terminal_status"] == "timeout"
    assert snapshot["answer_policy"] == "report_retrieval_error"

    tampered = _engine_authority_tool_contract(
        status="success", terminal_reason="success", include_evidence=True
    )
    tampered["contract_version"] = "tampered"
    fail_closed = classify_engine_attempt(
        contract=tampered, active_source_scope=scope
    )
    snapshot = record_selected_source_retrieval_telemetry(
        classification=fail_closed, active_sources=[]
    )
    assert snapshot["contract_valid"] is False
    assert snapshot["terminal_status"] == "error"
    assert snapshot["selected_source_runtime_mode"] == "retrieval_engine_fail_closed"
    assert snapshot["boundary_violation"] is False

    fallback = classify_engine_attempt(
        package={
            "authority": {"state": "fallback", "reason": "operator_disabled"},
            "attempt": {
                "engine_invoked": False,
                "fallback_reason": "operator_disabled",
            },
        }
    )
    legacy_card = {
        "origin": "legacy_fallback",
        "source": {"id": "alpha-file", "origin": "legacy_fallback"},
    }
    snapshot = record_selected_source_retrieval_telemetry(
        classification=fallback, active_sources=[legacy_card]
    )
    assert snapshot["runtime_mode"] == "legacy_fallback"
    assert snapshot["fallback_reason"] == "operator_disabled"
    assert snapshot["legacy_origin_source_card_count"] == 1
    assert snapshot["boundary_violation"] is False

    counters = selected_source_retrieval_telemetry_counters()
    assert sum(counters.values()) == 5
    assert all("|" in key for key in counters)


def test_selected_source_retrieval_telemetry_flags_contract_with_legacy_cards():
    reset_selected_source_retrieval_telemetry()
    scope = {"status": "resolved", "source_ids": ["alpha-file"]}
    legacy_card = {"source": {"id": "stale-file", "origin": "legacy_fallback"}}

    valid_contract = classify_engine_attempt(
        contract=_engine_authority_tool_contract(
            status="no_evidence",
            terminal_reason="rejected_candidates_only",
            include_evidence=False,
        ),
        active_source_scope=scope,
    )
    snapshot = record_selected_source_retrieval_telemetry(
        classification=valid_contract, active_sources=[legacy_card]
    )
    assert snapshot["boundary_violation"] is True
    assert "violation=1" in snapshot["counter_key"]

    tampered = _engine_authority_tool_contract(
        status="success", terminal_reason="success", include_evidence=True
    )
    tampered["engine_version"] = "tampered"
    invalid_contract = classify_engine_attempt(
        contract=tampered, active_source_scope=scope
    )
    snapshot = record_selected_source_retrieval_telemetry(
        classification=invalid_contract, active_sources=[legacy_card]
    )
    assert snapshot["boundary_violation"] is True

    counters = selected_source_retrieval_telemetry_counters()
    assert sum(value for key, value in counters.items() if "violation=1" in key) == 2
