"""
DeepAgent Direct Pipe — Connects Open WebUI directly to LangGraph (no bridge).

Upload via Open WebUI Admin → Functions → Add Function (type: pipe).
This talks to the LangGraph server's native API, giving you:
  - Full reasoning trace visibility
  - Tool call details with arguments
  - Thread-based conversation state
  - Proper error handling

Requires the LangGraph agent to be running (the 'agent' service in docker-compose).
"""

import json
import logging
from typing import Iterator, Union

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("deepagent-direct-pipe")


class Pipe:
    """Open WebUI Pipe for direct LangGraph integration."""

    class Valves(BaseModel):
        langgraph_url: str = Field(
            default="http://agent:32000",
            description="Direct URL of the LangGraph agent server",
        )
        graph_name: str = Field(
            default="network_search_agent",
            description="LangGraph graph ID",
        )
        show_reasoning: bool = Field(
            default=True,
            description="Show DeepSeek reasoning in collapsible blocks",
        )
        show_tool_calls: bool = Field(
            default=True,
            description="Show tool calls with arguments inline",
        )
        timeout: int = Field(
            default=300,
            description="Request timeout in seconds",
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        return [
            {
                "id": "deepagent-direct",
                "name": "DeepAgent Direct (LangGraph Native)",
            }
        ]

    def pipe(
        self,
        body: dict,
        __user__: dict | None = None,
        __event_emitter__=None,
    ) -> Union[str, Iterator[str]]:
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        # Convert Open WebUI messages to LangGraph input
        lg_messages = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    p.get("text", "") for p in content if p.get("type") == "text"
                ]
                content = "\n".join(text_parts)
            lg_messages.append({"role": msg.get("role", "user"), "content": content})

        if stream:
            return self._stream_langgraph(lg_messages)
        else:
            return self._sync_langgraph(lg_messages)

    def _find_assistant(self, client: httpx.Client) -> str | None:
        try:
            resp = client.post(
                f"{self.valves.langgraph_url}/assistants/search",
                json={"graph_id": self.valves.graph_name, "limit": 1},
                timeout=10,
            )
            resp.raise_for_status()
            assistants = resp.json()
            if assistants:
                return assistants[0]["assistant_id"]
        except Exception as exc:
            log.warning("Could not find assistant: %s", exc)
        return None

    def _create_thread(self, client: httpx.Client) -> str:
        resp = client.post(
            f"{self.valves.langgraph_url}/threads",
            json={},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["thread_id"]

    def _sync_langgraph(self, messages: list[dict]) -> str:
        """Non-streaming: run agent to completion and return formatted text."""
        collected_text = ""
        reasoning_parts = []
        tool_calls_seen = []

        for text, reasoning, tool_info in self._iter_events(messages):
            if text:
                collected_text = text
            if reasoning:
                reasoning_parts.append(reasoning)
            if tool_info:
                tool_calls_seen.append(tool_info)

        parts = []
        if self.valves.show_reasoning and reasoning_parts:
            reasoning_block = "\n\n".join(reasoning_parts)
            parts.append(
                f"<details>\n<summary>🧠 Reasoning</summary>\n\n"
                f"{reasoning_block}\n</details>\n"
            )
        if self.valves.show_tool_calls and tool_calls_seen:
            for name, args in tool_calls_seen:
                parts.append(f"> 🔧 **{name}**(`{args}`)\n")
        parts.append(collected_text)
        return "\n".join(parts)

    def _stream_langgraph(self, messages: list[dict]) -> Iterator[str]:
        """Streaming: yield formatted chunks as they arrive."""
        sent_reasoning = set()
        sent_tools = set()
        sent_text = set()

        for text, reasoning, tool_info in self._iter_events(messages):
            if self.valves.show_reasoning and reasoning and reasoning not in sent_reasoning:
                sent_reasoning.add(reasoning)
                yield f"\n<details>\n<summary>🧠 Reasoning</summary>\n\n{reasoning}\n</details>\n\n"

            if self.valves.show_tool_calls and tool_info and tool_info not in sent_tools:
                sent_tools.add(tool_info)
                name, args = tool_info
                yield f"\n> 🔧 **{name}**(`{args}`)\n\n"

            if text and text not in sent_text:
                sent_text.add(text)
                yield text

    def _iter_events(
        self, messages: list[dict]
    ) -> Iterator[tuple[str, str | None, tuple | None]]:
        """
        Iterate over LangGraph SSE events, yielding (text, reasoning, tool_info).
        """
        with httpx.Client(timeout=httpx.Timeout(self.valves.timeout, connect=10)) as client:
            assistant_id = self._find_assistant(client)
            thread_id = self._create_thread(client)

            payload = {
                "input": {"messages": messages},
                "stream_mode": ["messages", "values"],
            }
            if assistant_id:
                payload["assistant_id"] = assistant_id

            url = f"{self.valves.langgraph_url}/threads/{thread_id}/runs/stream"

            with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[len("data: "):]
                    if data_str.strip() == "[DONE]":
                        return

                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    text, reasoning, tool_info = "", None, None

                    # Messages stream mode: [event_type, payload]
                    if isinstance(event, list) and len(event) == 2:
                        _, payload_data = event
                        if isinstance(payload_data, dict):
                            msg_type = payload_data.get("type", "")
                            if msg_type == "ai":
                                content = payload_data.get("content", "")
                                if isinstance(content, str):
                                    text = content
                                reasoning = (
                                    payload_data
                                    .get("additional_kwargs", {})
                                    .get("reasoning_content")
                                )
                                for tc in (
                                    payload_data
                                    .get("additional_kwargs", {})
                                    .get("tool_calls", [])
                                ):
                                    fn = tc.get("function", {})
                                    name = fn.get("name", "")
                                    args = fn.get("arguments", "")
                                    if name:
                                        tool_info = (name, args)

                    # Values stream mode: full state snapshot
                    elif isinstance(event, dict):
                        msgs = event.get("messages", [])
                        if msgs:
                            last = msgs[-1]
                            if isinstance(last, dict) and last.get("type") == "ai":
                                content = last.get("content", "")
                                if isinstance(content, str):
                                    text = content
                                reasoning = (
                                    last.get("additional_kwargs", {})
                                    .get("reasoning_content")
                                )

                    yield text, reasoning, tool_info
