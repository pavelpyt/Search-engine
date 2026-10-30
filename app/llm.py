"""Direct client for the Anthropic Messages API.

No SDK wrappers, no LangChain: we build the request body ourselves, send it
with httpx and run the tool-use loop by hand, so every message, system prompt
and tool call is visible (and streamed to the UI as events).
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
MAX_RETRIES = int(os.getenv("API_MAX_RETRIES", "4"))

Emit = Callable[[dict], Awaitable[None]]
ClientTool = Callable[[dict], Awaitable[str]]


@dataclass
class AgentResult:
    text: str
    messages: list[dict]
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "web_searches": 0})
    turns: int = 0
    stop_reason: str | None = None


class MessagesClient:
    """Thin wrapper around POST /v1/messages with retries on overload."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        # Non-streaming turns with several web searches can take minutes.
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))

    async def create(self, *, model: str, system: str, messages: list[dict],
                     tools: list[dict] | None = None, max_tokens: int = 4096, on_retry=None) -> dict:
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {"x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await self.http.post(API_URL, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"Anthropic API unreachable: {exc}") from exc
                delay = 2 ** attempt + random.random()
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code not in RETRY_STATUS or attempt == MAX_RETRIES:
                    raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else 2 ** attempt + random.random()
            if on_retry:
                await on_retry(attempt + 1, round(delay, 1))
            await asyncio.sleep(min(delay, 30))
        raise RuntimeError("unreachable")


_CLIENT: MessagesClient | None = None


def is_mock() -> bool:
    return os.getenv("MOCK", "").lower() in {"1", "true", "yes"} or not os.getenv("ANTHROPIC_API_KEY")


def make_client():
    """One shared HTTP client for the whole app (mock client when no key)."""
    global _CLIENT
    if is_mock():
        from .mock import MockClient
        return MockClient()
    if _CLIENT is None:
        _CLIENT = MessagesClient(os.environ["ANTHROPIC_API_KEY"])
    return _CLIENT


def for_display(obj):
    """Copy of messages/content for the UI and logs, without encrypted search payloads.

    The real request keeps them (the API requires them unchanged); the browser
    only needs to see that they are there.
    """
    obj = copy.deepcopy(obj)

    def walk(x):
        if isinstance(x, dict):
            for key in ("encrypted_content", "encrypted_index"):
                if key in x:
                    x[key] = "…(encrypted, kept in the API request)"
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return obj


def _summarise_search_result(block: dict) -> dict:
    content = block.get("content")
    if isinstance(content, dict):  # error object
        return {"error": content.get("error_code", "unknown")}
    return {"results": [{"title": r.get("title"), "url": r.get("url"), "page_age": r.get("page_age")}
                        for r in (content or [])]}


async def run_agent(
    *,
    client,
    agent: str,
    model: str,
    system: str,
    user_content: str | None = None,
    messages: list[dict] | None = None,
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
    Pass `messages` to continue an existing conversation (e.g. a JSON repair turn).
    """
    messages = list(messages) if messages else [{"role": "user", "content": user_content}]
    usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
    await emit({"type": "agent_request", "agent": agent, "model": model, "system": system,
                "tools": tools, "messages": for_display(messages)})

    async def on_retry(attempt, delay):
        await emit({"type": "log", "level": "warn",
                    "msg": f"{agent}: API overloaded, retry {attempt}/{MAX_RETRIES} in {delay}s"})

    last_text, stop = "", None
    for turn in range(1, max_turns + 1):
        kwargs = {"model": model, "system": system, "messages": messages,
                  "tools": tools or None, "max_tokens": max_tokens}
        if isinstance(client, MessagesClient):
            kwargs["on_retry"] = on_retry
        resp = await client.create(**kwargs)
        u = resp.get("usage", {})
        usage["input_tokens"] += u.get("input_tokens", 0)
        usage["output_tokens"] += u.get("output_tokens", 0)
        usage["web_searches"] += (u.get("server_tool_use") or {}).get("web_search_requests", 0)
        content = resp.get("content", [])
        stop = resp.get("stop_reason")
        messages.append({"role": "assistant", "content": content})
        await emit({"type": "agent_turn", "agent": agent, "turn": turn, "stop_reason": stop,
                    "content": for_display(content), "usage": dict(usage)})

        texts, client_calls = [], []
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
        await emit({"type": "agent_messages", "agent": agent, "messages": for_display(messages)})
        return AgentResult(text=last_text, messages=messages, usage=usage, turns=turn, stop_reason=stop)

    await emit({"type": "log", "level": "warn", "msg": f"{agent} reached max_turns={max_turns}"})
    await emit({"type": "agent_messages", "agent": agent, "messages": for_display(messages)})
    return AgentResult(text=last_text, messages=messages, usage=usage, turns=max_turns, stop_reason=stop)


def extract_json(text: str) -> dict:
    """Return the JSON object in a model reply.

    Tries fenced ```json blocks first, then every '{' position with
    raw_decode, and keeps the last object that parses (the final answer
    usually comes after any narration).
    """
    if "```" in text:
        for part in reversed(text.split("```")):
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                try:
                    return json.loads(p)
                except json.JSONDecodeError:
                    pass
    decoder = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
            if isinstance(obj, dict):
                found = obj
            i = text.find("{", end)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
    if found is None:
        raise ValueError("No valid JSON object in agent output")
    return found
