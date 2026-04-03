from open_webui.utils.middleware import (
    _attach_generated_files_to_output,
    _build_chat_completion_payload,
    handle_responses_streaming_event,
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
