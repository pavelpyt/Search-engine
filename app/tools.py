"""Tool definitions (JSON schema sent to the API) and their implementations.

- web_search   : Anthropic server tool, executed on Anthropic's side.
- fetch_page   : client tool, we download the page and extract readable text
                 (trafilatura, open source) so the agent can read the source.
- evaluate_source : client tool backed by the source-evaluation skill script.
"""
from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import os
import socket
from urllib.parse import urlparse

import httpx
import trafilatura

from .skills import SKILLS_DIR


def web_search_tool(max_uses: int) -> dict:
    return {"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}


FETCH_PAGE = {
    "name": "fetch_page",
    "description": (
        "Download a web page and return its main readable text (max ~6000 chars). "
        "Use it to read a promising search result before you quote it. "
        "Only public http(s) URLs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
        "required": ["url"],
    },
}

EVALUATE_SOURCE = {
    "name": "evaluate_source",
    "description": (
        "Source-evaluation skill: deterministic base score (0-100) and tier A-D for a source URL, "
        "from its domain type, https and publication date. Call it for every source before you "
        "decide how much to trust a fact."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "page_age": {"type": "string", "description": "Publication date if known, e.g. 'March 2025'"},
        },
        "required": ["url"],
    },
}

MAX_FETCH_BYTES = 2_000_000


def _is_public(host: str) -> bool:
    if host in {"localhost"} or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable here. Allowed only behind an egress proxy that resolves
        # names itself and cannot reach our network (set ALLOW_UNRESOLVED=1).
        return os.getenv("ALLOW_UNRESOLVED") == "1"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


MAX_REDIRECTS = 3


async def fetch_page(args: dict) -> str:
    url = str(args.get("url", ""))
    if os.getenv("MOCK_FETCH") == "1":
        return f"[mock] Readable text of {url}: key facts and figures about the topic, published 2026."
    async with httpx.AsyncClient(timeout=15, follow_redirects=False,
                                 headers={"User-Agent": "ResearchAgents/1.0 (+demo)"}) as http:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return "Rejected: only absolute public http(s) URLs."
            # Every hop is checked, so a public URL cannot redirect to a private one.
            if not await asyncio.to_thread(_is_public, parsed.hostname):
                return "Rejected: private or unresolvable host."
            async with http.stream("GET", url) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = str(resp.url.join(resp.headers["location"]))
                    continue
                if resp.status_code >= 400:
                    return f"HTTP {resp.status_code} for {url}"
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= MAX_FETCH_BYTES:  # stop downloading at 2 MB
                        break
                html = bytes(body[:MAX_FETCH_BYTES]).decode(resp.encoding or "utf-8", errors="replace")
                break
        else:
            return f"Too many redirects for {args.get('url')}"
    text = await asyncio.to_thread(trafilatura.extract, html, include_comments=False, include_tables=True)
    if not text:
        return f"No readable text extracted from {url}"
    return text[:6000]


def _load_score_module():
    path = SKILLS_DIR / "source-evaluation" / "scripts" / "score_source.py"
    spec = importlib.util.spec_from_file_location("score_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_score = _load_score_module()


async def evaluate_source(args: dict) -> str:
    return json.dumps(_score.score(str(args.get("url", "")), args.get("page_age")), ensure_ascii=False)
