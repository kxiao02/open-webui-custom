"""
DeepAgent Direct Pipe — Connects Open WebUI directly to LangGraph (no bridge).

Upload via Open WebUI Admin → Functions → Add Function (type: pipe).
This talks to the LangGraph server's native API, giving you:
  - Full reasoning trace visibility
  - Tool call details with arguments
  - Thread-based conversation state

Requires the LangGraph agent to be running (the 'agent' service in docker-compose).
"""

import json
import logging
from typing import Any, Iterator, Union

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
        __task__=None,
        __metadata__=None,
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

        show_reasoning = self.valves.show_reasoning
        show_tool_calls = self.valves.show_tool_calls
        task_value = __task__
        if task_value is None and isinstance(__metadata__, dict):
            task_value = __metadata__.get("task")
        if task_value is not None and str(task_value).strip():
            show_reasoning = False
            show_tool_calls = False

        if stream:
            return self._stream_langgraph(
                lg_messages,
                show_reasoning=show_reasoning,
                show_tool_calls=show_tool_calls,
            )
        else:
            return self._sync_langgraph(
                lg_messages,
                show_reasoning=show_reasoning,
                show_tool_calls=show_tool_calls,
            )

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

    def _sync_langgraph(
        self,
        messages: list[dict],
        *,
        show_reasoning: bool | None = None,
        show_tool_calls: bool | None = None,
    ) -> str:
        """Non-streaming: run agent to completion and return formatted text."""
        try:
            if show_reasoning is None:
                show_reasoning = self.valves.show_reasoning
            if show_tool_calls is None:
                show_tool_calls = self.valves.show_tool_calls

            collected_text = ""
            reasoning_parts = []
            tool_calls_seen = []

            for text, reasoning, tool_infos in self._iter_events(messages):
                if text:
                    collected_text = text
                if reasoning:
                    reasoning_parts.append(reasoning)
                for ti in tool_infos:
                    if ti not in tool_calls_seen:
                        tool_calls_seen.append(ti)

            parts = []
            if show_reasoning and reasoning_parts:
                reasoning_block = "\n\n".join(reasoning_parts)
                parts.append(
                    f"<details>\n<summary>🧠 Reasoning</summary>\n\n"
                    f"{reasoning_block}\n</details>\n"
                )
            if show_tool_calls and tool_calls_seen:
                for name, args in tool_calls_seen:
                    parts.append(f"> 🔧 **{name}**(`{args}`)\n")
            parts.append(collected_text)
            return "\n".join(parts)
        except httpx.ConnectError:
            return "Error: Could not connect to the LangGraph agent. Please check that it is running."
        except httpx.TimeoutException:
            return "Error: The LangGraph agent did not respond in time. Please try again."
        except httpx.HTTPStatusError as exc:
            return f"Error: LangGraph agent returned status {exc.response.status_code}."
        except Exception as exc:
            log.error("Unexpected error in sync handler: %s", exc)
            return "Error: An unexpected error occurred while contacting the agent."

    def _stream_langgraph(
        self,
        messages: list[dict],
        *,
        show_reasoning: bool | None = None,
        show_tool_calls: bool | None = None,
    ) -> Iterator[str]:
        """Streaming: yield formatted chunks as they arrive."""
        try:
            if show_reasoning is None:
                show_reasoning = self.valves.show_reasoning
            if show_tool_calls is None:
                show_tool_calls = self.valves.show_tool_calls

            sent_reasoning = set()
            sent_tools = set()
            last_text_len = 0

            for text, reasoning, tool_infos in self._iter_events(messages):
                if show_reasoning and reasoning and reasoning not in sent_reasoning:
                    sent_reasoning.add(reasoning)
                    yield f"\n<details>\n<summary>🧠 Reasoning</summary>\n\n{reasoning}\n</details>\n\n"

                if show_tool_calls and tool_infos:
                    for ti in tool_infos:
                        if ti not in sent_tools:
                            sent_tools.add(ti)
                            name, args = ti
                            yield f"\n> 🔧 **{name}**(`{args}`)\n\n"

                if text and len(text) > last_text_len:
                    yield text[last_text_len:]
                    last_text_len = len(text)
        except httpx.ConnectError:
            yield "Error: Could not connect to the LangGraph agent. Please check that it is running."
        except httpx.TimeoutException:
            yield "Error: The LangGraph agent did not respond in time. Please try again."
        except httpx.HTTPStatusError as exc:
            yield f"Error: LangGraph agent returned status {exc.response.status_code}."
        except Exception as exc:
            log.error("Unexpected error in streaming handler: %s", exc)
            yield "Error: An unexpected error occurred while contacting the agent."

    def _iter_events(
        self, messages: list[dict]
    ) -> Iterator[tuple[str, str | None, list[tuple]]]:
        """
        Iterate over LangGraph SSE events, yielding (text, reasoning, tool_infos).
        tool_infos is a list of (name, args) tuples (may be empty).
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

                    text, reasoning, tool_infos = "", None, []

                    # Messages stream mode: [event_type, payload]
                    if isinstance(event, list) and len(event) == 2:
                        _, payload_data = event
                        if isinstance(payload_data, dict):
                            msg_type = payload_data.get("type", "")
                            if msg_type == "ai":
                                content = payload_data.get("content", "")
                                text = self._extract_text_from_content(content)
                                reasoning = self._extract_reasoning_from_payload(payload_data)
                                tool_infos.extend(self._extract_tool_infos(payload_data))

                    # Values stream mode: full state snapshot
                    elif isinstance(event, dict):
                        msgs = event.get("messages", [])
                        if msgs:
                            last = msgs[-1]
                            if isinstance(last, dict) and last.get("type") == "ai":
                                content = last.get("content", "")
                                text = self._extract_text_from_content(content)
                                reasoning = self._extract_reasoning_from_payload(last)
                                tool_infos.extend(self._extract_tool_infos(last))

                    yield text, reasoning, tool_infos

    def _extract_text_from_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "").strip()
                if part_type in {"text", "output_text", "input_text"}:
                    text = part.get("text", "")
                    if text:
                        parts.append(str(text))
            return "\n".join(parts)
        if isinstance(content, dict):
            text = content.get("text", "")
            return str(text) if text else ""
        return ""

    def _extract_reasoning_from_payload(self, payload: dict) -> str | None:
        if not isinstance(payload, dict):
            return None
        additional = payload.get("additional_kwargs", {})
        candidates = [
            additional.get("reasoning_content"),
            additional.get("reasoning"),
            additional.get("thinking"),
            payload.get("reasoning_content"),
            payload.get("reasoning"),
            payload.get("thinking"),
        ]
        for candidate in candidates:
            text = self._extract_reasoning_text(candidate)
            if text:
                return text
        return None

    def _extract_reasoning_text(self, value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "model_dump"):
            try:
                return self._extract_reasoning_text(value.model_dump())
            except Exception:
                return ""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            if text[0] in "[{":
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    nested = self._extract_reasoning_text(parsed)
                    if nested:
                        return nested
            return text
        if isinstance(value, list):
            parts = [self._extract_reasoning_text(item) for item in value]
            return "\n".join(part for part in parts if part)
        if isinstance(value, dict):
            block_type = str(value.get("type") or "").strip().lower()
            text_value = value.get("text")
            if isinstance(text_value, str) and text_value.strip():
                if block_type in {
                    "reasoning_text",
                    "reasoning_summary_text",
                    "summary_text",
                    "text",
                    "input_text",
                    "output_text",
                } or not any(
                    key in value
                    for key in ("content", "summary", "reasoning_content", "reasoning", "steps")
                ):
                    return text_value.strip()
            parts: list[str] = []
            for key in ("content", "summary", "reasoning_content", "reasoning", "steps"):
                if key in value:
                    nested = self._extract_reasoning_text(value.get(key))
                    if nested:
                        parts.append(nested)
            return "\n".join(parts)
        return ""

    def _extract_tool_infos(self, payload: dict) -> list[tuple[str, str]]:
        tool_infos: list[tuple[str, str]] = []
        if not isinstance(payload, dict):
            return tool_infos
        for tc in payload.get("tool_calls", []) or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or tc.get("function", {}).get("name") or ""
            args = (
                tc.get("args")
                if "args" in tc
                else tc.get("function", {}).get("arguments")
            )
            if not name:
                continue
            if isinstance(args, dict):
                args_str = json.dumps(args, ensure_ascii=False)
            else:
                args_str = str(args) if args is not None else ""
            tool_infos.append((str(name), args_str))
        return tool_infos
