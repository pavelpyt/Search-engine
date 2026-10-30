"""Direct client for the Anthropic Messages API.

No SDK wrappers, no LangChain: we build the request body ourselves, send it
with httpx and run the tool-use loop by hand, so every message, system prompt
and tool call is visible (and streamed to the UI as events).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

Emit = Callable[[dict], Awaitable[None]]
ClientTool = Callable[[dict], Awaitable[str]]


@dataclass
class AgentResult:
    text: str
    messages: list[dict]
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    turns: int = 0


class MessagesClient:
    """Thin wrapper around POST /v1/messages."""

    def __init__(self, api_key: str, timeout: float = 180.0):
        self.api_key = api_key
        self.http = httpx.AsyncClient(timeout=timeout)

    async def create(self, *, model: str, system: str, messages: list[dict],
                     tools: list[dict] | None = None, max_tokens: int = 4096) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        resp = await self.http.post(
            API_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")
        return resp.json()


def _summarise_search_result(block: dict) -> dict:
    content = block.get("content")
    if isinstance(content, dict):  # error object
        return {"error": content.get("error_code", "unknown")}
    return {
        "results": [
            {"title": r.get("title"), "url": r.get("url"), "page_age": r.get("page_age")}
            for r in (content or [])
        ]
    }


async def run_agent(
    *,
    client,
    agent: str,
    model: str,
    system: str,
    user_content: str,
    tools: list[dict],
    client_tools: dict[str, ClientTool],
    emit: Emit,
    max_turns: int = 10,
    max_tokens: int = 8000,
) -> AgentResult:
    """Run one agent: send messages, execute client tools, loop until end_turn.

    Server tools (web_search) are executed by Anthropic; their blocks come back
    in the assistant content and must be sent back unchanged. Client tools
    (fetch_page, evaluate_source) are executed here and returned as tool_result.
    """
    messages: list[dict] = [{"role": "user", "content": user_content}]
    usage = {"input_tokens": 0, "output_tokens": 0}
    await emit({"type": "agent_request", "agent": agent, "model": model,
                "system": system, "tools": tools, "messages": messages})

    last_text = ""
    for turn in range(1, max_turns + 1):
        resp = await client.create(model=model, system=system, messages=messages,
                                   tools=tools or None, max_tokens=max_tokens)
        u = resp.get("usage", {})
        usage["input_tokens"] += u.get("input_tokens", 0)
        usage["output_tokens"] += u.get("output_tokens", 0)
        content = resp.get("content", [])
        stop = resp.get("stop_reason")
        messages.append({"role": "assistant", "content": content})
        await emit({"type": "agent_turn", "agent": agent, "turn": turn,
                    "stop_reason": stop, "content": content, "usage": usage})

        texts = []
        client_calls = []
        for block in content:
            t = block.get("type")
            if t == "text":
                texts.append(block.get("text", ""))
            elif t == "server_tool_use":
                await emit({"type": "tool_call", "agent": agent, "kind": "server",
                            "name": block.get("name"), "input": block.get("input")})
            elif t == "web_search_tool_result":
                await emit({"type": "tool_result", "agent": agent, "kind": "server",
                            "name": "web_search", "result": _summarise_search_result(block)})
            elif t == "tool_use":
                client_calls.append(block)
        if texts:
            last_text = "".join(texts)

        if stop == "tool_use" and client_calls:
            results = []
            for call in client_calls:
                name = call.get("name")
                await emit({"type": "tool_call", "agent": agent, "kind": "client",
                            "name": name, "input": call.get("input")})
                impl = client_tools.get(name)
                if impl is None:
                    out, is_error = f"Unknown tool {name}", True
                else:
                    try:
                        out, is_error = await impl(call.get("input") or {}), False
                    except Exception as exc:  # tool failures go back to the model
                        out, is_error = f"Tool error: {exc}", True
                await emit({"type": "tool_result", "agent": agent, "kind": "client",
                            "name": name, "result": out[:1500], "is_error": is_error})
                results.append({"type": "tool_result", "tool_use_id": call["id"],
                                "content": out, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            continue
        if stop == "max_tokens":
            await emit({"type": "log", "level": "warn", "msg": f"{agent} hit max_tokens, output may be cut"})
        if stop == "pause_turn":
            continue  # long server-side search: send the paused turn back as-is
        await emit({"type": "agent_messages", "agent": agent, "messages": messages})
        return AgentResult(text=last_text, messages=messages, usage=usage, turns=turn)

    await emit({"type": "log", "level": "warn", "msg": f"{agent} reached max_turns={max_turns}"})
    await emit({"type": "agent_messages", "agent": agent, "messages": messages})
    return AgentResult(text=last_text, messages=messages, usage=usage, turns=max_turns)


def extract_json(text: str) -> dict:
    """Pull the last JSON object out of a model reply (tolerates ```json fences)."""
    if "```" in text:
        parts = text.split("```")
        for part in reversed(parts):
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                try:
                    return json.loads(p)
                except json.JSONDecodeError:
                    pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in agent output")
    return json.loads(text[start:end + 1])


def make_client():
    if os.getenv("MOCK", "").lower() in {"1", "true", "yes"} or not os.getenv("ANTHROPIC_API_KEY"):
        from .mock import MockClient
        return MockClient()
    return MessagesClient(os.environ["ANTHROPIC_API_KEY"])
