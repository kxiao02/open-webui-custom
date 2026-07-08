"""
DeepAgent Pipe — Open WebUI Function (Pipe) for LangGraph Agent Integration.

Upload this file via Open WebUI Admin → Functions → Add Function (type: pipe).
It connects Open WebUI to the agent-bridge (OpenAI-compatible API), exposing:
  - Streaming responses with DeepSeek reasoning traces
  - Tool call visibility (shows which tools the agent uses)
  - Structured research output with citations

Requires the agent-bridge to be running (see deepagent-project).
"""

import json
from typing import Iterator, Union

import httpx
from pydantic import BaseModel, Field


class Pipe:
    """Open WebUI Pipe that connects to the agent-bridge."""

    class Valves(BaseModel):
        """Admin-configurable settings (Open WebUI Admin → Functions → Valves)."""

        bridge_url: str = Field(
            default="http://agent-bridge:8000",
            description="URL of the agent-bridge (OpenAI-compatible API)",
        )
        graph_name: str = Field(
            default="network_search_agent",
            description="LangGraph graph ID to use",
        )
        show_reasoning: bool = Field(
            default=True,
            description="Show DeepSeek reasoning traces in collapsible blocks",
        )
        show_tool_calls: bool = Field(
            default=True,
            description="Show tool call details inline",
        )
        request_timeout: int = Field(
            default=300,
            description="Max seconds to wait for agent response",
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        """Register models that this pipe exposes."""
        return [
            {
                "id": "deepagent-research",
                "name": "DeepAgent Research (LangGraph)",
            }
        ]

    def pipe(
        self,
        body: dict,
        __user__: dict | None = None,
        __event_emitter__=None,
    ) -> Union[str, Iterator[str]]:
        """
        Main handler — called by Open WebUI for each chat turn.
        Supports both streaming and non-streaming.
        """
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        # Convert to OpenAI format for the bridge
        openai_messages = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                # Handle multimodal content blocks
                text_parts = [
                    p.get("text", "") for p in content if p.get("type") == "text"
                ]
                content = "\n".join(text_parts)
            openai_messages.append({"role": msg.get("role", "user"), "content": content})

        payload = {
            "model": "deepagent",
            "messages": openai_messages,
            "stream": stream,
        }

        if stream:
            return self._stream(payload, __event_emitter__)
        else:
            return self._sync(payload)

    def _sync(self, payload: dict) -> str:
        """Non-streaming: call bridge and return full text."""
        try:
            with httpx.Client(timeout=httpx.Timeout(self.valves.request_timeout, connect=10)) as client:
                resp = client.post(
                    f"{self.valves.bridge_url}/v1/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except httpx.ConnectError:
            return "Error: Could not connect to the agent service. Please check that it is running."
        except httpx.TimeoutException:
            return "Error: The agent service did not respond in time. Please try again."
        except httpx.HTTPStatusError as exc:
            return f"Error: Agent service returned status {exc.response.status_code}."
        except Exception:
            return "Error: An unexpected error occurred while contacting the agent service."

    def _stream(self, payload: dict, event_emitter=None) -> Iterator[str]:
        """Streaming: yield chunks from bridge SSE stream."""
        payload["stream"] = True

        try:
            with httpx.Client(timeout=httpx.Timeout(self.valves.request_timeout, connect=10)) as client:
                with client.stream(
                    "POST",
                    f"{self.valves.bridge_url}/v1/chat/completions",
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[len("data: "):]
                        if data_str.strip() == "[DONE]":
                            return

                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        except httpx.ConnectError:
            yield "Error: Could not connect to the agent service. Please check that it is running."
        except httpx.TimeoutException:
            yield "Error: The agent service did not respond in time. Please try again."
        except httpx.HTTPStatusError as exc:
            yield f"Error: Agent service returned status {exc.response.status_code}."
        except Exception:
            yield "Error: An unexpected error occurred while contacting the agent service."
