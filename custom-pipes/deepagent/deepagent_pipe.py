"""
DeepAgent Pipe — Open WebUI Function (Pipe) for LangGraph Agent Integration.

Upload this file via Open WebUI Admin → Functions → Add Function (type: pipe).
It connects Open WebUI directly to the LangGraph agent, exposing:
  - Streaming responses with DeepSeek reasoning traces
  - Tool call visibility (shows which tools the agent uses)
  - Structured research output with citations

This bypasses the bridge for users who want tighter integration.
"""

import json
import time
from typing import Iterator, Union

import httpx
from pydantic import BaseModel, Field


class Pipe:
    """Open WebUI Pipe that connects to a LangGraph agent."""

    class Valves(BaseModel):
        """Admin-configurable settings (Open WebUI Admin → Functions → Valves)."""

        langgraph_url: str = Field(
            default="http://agent-bridge:8000",
            description="URL of the agent-bridge or direct LangGraph server",
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
        with httpx.Client(timeout=self.valves.request_timeout) as client:
            resp = client.post(
                f"{self.valves.langgraph_url}/v1/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    def _stream(self, payload: dict, event_emitter=None) -> Iterator[str]:
        """Streaming: yield chunks from bridge SSE stream."""
        payload["stream"] = True

        with httpx.Client(timeout=self.valves.request_timeout) as client:
            with client.stream(
                "POST",
                f"{self.valves.langgraph_url}/v1/chat/completions",
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
