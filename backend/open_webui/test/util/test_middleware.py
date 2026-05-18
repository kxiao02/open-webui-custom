import asyncio
import json
from types import SimpleNamespace

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
    _constrain_retrieval_queries,
    _compare_knowflow_retrieval_against_neutral_contract,
    _build_assistant_reference_seed_metadata,
    _build_assistant_reference_persistence_metadata,
    _build_chat_completion_payload,
    _resolve_active_source_scope,
    apply_source_context_to_messages,
    handle_responses_streaming_event,
)
from open_webui.utils.task import (
    build_session_user_memory_prompt,
    build_relevant_prior_user_facts_block,
    extract_session_user_facts,
    query_generation_template,
)


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
