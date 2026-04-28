from types import SimpleNamespace

from open_webui.utils.middleware import (
    _attach_generated_files_to_output,
    _build_chat_completion_payload,
    apply_source_context_to_messages,
    handle_responses_streaming_event,
)
from open_webui.utils.task import (
    build_session_user_memory_prompt,
    build_relevant_prior_user_facts_block,
    extract_session_user_facts,
    query_generation_template,
)


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
