"""Offline stand-in for the Messages API (used when no API key is set).

It returns responses in exactly the API's shape (content blocks, stop_reason,
usage, server_tool_use / web_search_tool_result, client tool_use), so the real
tool loop and orchestration run unchanged. It also demonstrates a handoff
return: the analyst sends the first research back once.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import uuid

os.environ.setdefault("MOCK_FETCH", "1")

SOURCES = [
    ("https://www.oecd.org/en/topics/artificial-intelligence.html", "OECD – Artificial intelligence", "March 2026"),
    ("https://www.reuters.com/technology/ai-adoption-europe-2026/", "Reuters – AI adoption in Europe", "January 2026"),
    ("https://en.wikipedia.org/wiki/Artificial_intelligence", "Wikipedia – Artificial intelligence", "2026"),
    ("https://medium.com/@someone/ai-hype", "Medium – AI hype post", "2022"),
]


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _question(messages: list[dict]) -> str:
    first = messages[0]["content"]
    m = re.search(r'"question":\s*"([^"]+)"|<question>(.*?)</question>', first, re.S)
    return (m.group(1) or m.group(2)).strip() if m else "the topic"


def _text(obj: dict) -> list[dict]:
    return [{"type": "text", "text": "```json\n" + json.dumps(obj, ensure_ascii=False, indent=2) + "\n```"}]


class MockClient:
    async def create(self, *, model, system, messages, tools=None, max_tokens=4096):
        await asyncio.sleep(random.uniform(0.6, 1.4))
        agent = re.search(r"AGENT: (\w+)", system).group(1)
        turn = sum(1 for m in messages if m["role"] == "assistant") + 1
        q = _question(messages)
        content, stop = getattr(self, f"_{agent}")(q, turn, messages)
        return {"content": content, "stop_reason": stop,
                "usage": {"input_tokens": random.randint(800, 4000), "output_tokens": random.randint(150, 900)}}

    def _router(self, q, turn, messages):
        return _text({
            "complexity": "moderate" if len(q) > 40 else "simple",
            "reasoning": "Needs current data from several sources, but no deep expert reasoning.",
            "language": "cs" if re.search(r"[ěščřžýáíéůú]", q) else "en",
            "goal": f"Give a sourced, practical answer to: {q}",
            "subquestions": [f"What is the current state of: {q}?", "What numbers or data exist?",
                             "What are the risks or open issues?"],
        }), "end_turn"

    def _researcher(self, q, turn, messages):
        retry = "<feedback" in messages[0]["content"]
        if turn == 1:
            sid = _id("srvtoolu")
            return [
                {"type": "text", "text": "Searching for current sources."},
                {"type": "server_tool_use", "id": sid, "name": "web_search", "input": {"query": q[:80]}},
                {"type": "web_search_tool_result", "tool_use_id": sid, "content": [
                    {"type": "web_search_result", "url": u, "title": t, "page_age": a, "encrypted_content": "mock"}
                    for u, t, a in SOURCES]},
            ] + [{"type": "tool_use", "id": _id("toolu"), "name": "fetch_page", "input": {"url": u}}
                 for u, _, _ in SOURCES[:3]], "tool_use"
        quotes = ["The relevant sentence from the page states the main finding clearly.",
                  "Key facts and figures about the topic, published 2026.",
                  "A second paragraph adds context and a supporting figure for 2026.",
                  "The relevant sentence from the page states the main finding clearly."]
        n = 4 if retry else 3
        findings = [{"id": f"f{i + 1}", "claim": f"Finding {i + 1} about {q}", "source_url": SOURCES[i % 3][0],
                     "source_title": SOURCES[i % 3][1], "quote": quotes[i], "page_age": SOURCES[i % 3][2]}
                    for i in range(n)]
        return _text({"findings": findings,
                      "sources": [{"url": u, "title": t, "page_age": a} for u, t, a in SOURCES[:3]],
                      "gaps": [] if retry else ["No figures for 2026 yet"]}), "end_turn"

    def _analyst(self, q, turn, messages):
        retry = "feedback from=\"orchestrator/writer\"" in messages[0]["content"]
        research = messages[0]["content"]
        if turn == 1:
            return [{"type": "text", "text": "Scoring each source with the source-evaluation skill."}] + [
                {"type": "tool_use", "id": _id("toolu"), "name": "evaluate_source",
                 "input": {"url": u, "page_age": a}} for u, _, a in SOURCES[:3]], "tool_use"
        first_pass = "Finding 4" not in research and not retry
        return _text({
            "handoff_check": {"ok": not first_pass,
                              "missing": ["2026 figures from a second independent source"] if first_pass else []},
            "source_scores": [{"url": SOURCES[0][0], "score": 95, "tier": "A", "reason": "official, recent"},
                              {"url": SOURCES[1][0], "score": 80, "tier": "A", "reason": "reputable media"},
                              {"url": SOURCES[2][0], "score": 65, "tier": "B", "reason": "reference, verify"}],
            "verified_facts": [
                {"id": "F1", "fact": f"Key verified fact about {q}", "based_on": ["f1", "f2"],
                 "verification_status": "SUPPORTED", "confidence": "high", "primary": True,
                 "independence": "official body and a news agency, different authors",
                 "note": "two sources agree"},
                {"id": "F2", "fact": "A supporting figure for 2026", "based_on": ["f3"],
                 "verification_status": "PARTIALLY_SUPPORTED", "confidence": "medium",
                 "independence": "single source", "note": "reference work, verify at the origin"}],
            "contradictions": [],
            "open_questions": ["Exact 2026 numbers are not published yet"],
            "outline": ["Short answer", "What the data says", "Risks", "Open questions"],
        }), "end_turn"

    def _writer(self, q, turn, messages):
        md = (f"## Shrnutí\n\n- Hlavní zjištění k otázce „{q}“ potvrzují dva nezávislé zdroje [1][2].\n"
              "- Podpůrné číslo pochází z jednoho zdroje, ber ho opatrně [3].\n\n"
              "## Co říkají data\n\nOficiální zdroj a zpravodajská agentura se shodují na hlavním trendu [1][2]. "
              "Přehledová encyklopedie uvádí podpůrné číslo, ale slouží jen jako výchozí bod [3].\n\n"
              "## Rizika\n\nNejvětší nejistota je v aktuálních číslech za letošní rok, "
              "která zatím nejsou zveřejněná [3].\n\n"
              "## Co nevíme\n\n- Přesná čísla za rok 2026.\n\n"
              "*Toto je ukázková odpověď v mock režimu (bez API klíče).*")
        return _text({"handoff_check": {"ok": True, "missing": []}, "used_facts": ["F1", "F2"],
                      "answer_markdown": md}), "end_turn"
