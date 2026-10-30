import asyncio
import os

os.environ["MOCK"] = "1"

from app import contracts as C
from app.llm import extract_json, run_agent
from app.mock import MockClient
from app.orchestrator import Pipeline
from app.skills import list_skills, load_skill
from app.tools import _score


def run(coro):
    return asyncio.run(coro)


def test_skills_have_frontmatter():
    names = {s["name"] for s in list_skills()}
    assert {"source-evaluation", "plain-writing", "question-decomposition"} <= names
    s = load_skill("source-evaluation")
    assert "evaluate_source" in s.body and s.description


def test_score_source_tiers():
    assert _score.score("https://www.czso.cz/x", "2026")["tier"] == "A"
    assert _score.score("https://www.reddit.com/r/x")["tier"] == "D"
    assert _score.score("http://example.com/old", "2015")["score"] < 50


def test_research_contract():
    assert C.check_research({"findings": []})
    good = {"findings": [{"claim": "a", "source_url": "https://a"}, {"claim": "b", "source_url": "https://b"},
                         {"claim": "c", "source_url": "https://a"}]}
    assert C.check_research(good) == []


def test_quality_report_flags_bad_citation():
    q = C.quality_report("## Summary\n\nFact [1] and [5].\n\n## Open questions\n\n- x", [{"n": 1}])
    assert q["invalid_citations"] == [5] and not q["passed"]


def test_extract_json_from_fence():
    assert extract_json('text\n```json\n{"a": 1}\n```') == {"a": 1}


class FakeClient:
    """Returns a server tool block, then a client tool call, then the answer."""
    def __init__(self):
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw["messages"])
        n = len(self.calls)
        if n == 1:
            return {"stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [
                {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "q"}},
                {"type": "web_search_tool_result", "tool_use_id": "srv1",
                 "content": [{"type": "web_search_result", "url": "https://x", "title": "X", "encrypted_content": "ENC"}]},
                {"type": "tool_use", "id": "tu1", "name": "echo", "input": {"v": 1}}]}
        return {"stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "text", "text": '{"done": true}'}]}


def test_tool_loop_sends_server_blocks_back_unchanged():
    client, events = FakeClient(), []

    async def emit(e):
        events.append(e)

    async def echo(args):
        return "echo:" + str(args["v"])

    res = run(run_agent(client=client, agent="t", model="m", system="AGENT: t", user_content="hi",
                        tools=[], client_tools={"echo": echo}, emit=emit))
    second_request = client.calls[1]
    assert second_request[1]["content"][1]["content"][0]["encrypted_content"] == "ENC"
    assert second_request[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu1",
                                               "content": "echo:1", "is_error": False}
    assert extract_json(res.text) == {"done": True}
    assert {e["type"] for e in events} >= {"tool_call", "tool_result", "agent_messages"}


def test_full_pipeline_in_mock_mode_with_one_return():
    events = []

    async def emit(e):
        events.append(e)

    result = run(Pipeline(MockClient(), emit).run("Jaký je stav AI v Evropě v roce 2026?"))
    types = [e["type"] for e in events]
    assert types.count("handoff_return") == 1          # analyst sent research back once
    assert [e["from"] for e in events if e["type"] == "handoff"][:2] == ["router", "researcher"]
    assert any(e["type"] == "skill_used" and e["skill"] == "source-evaluation" for e in events)
    assert any(e["type"] == "tool_call" and e["name"] == "evaluate_source" for e in events)
    assert result["quality"]["passed"]


def test_domain_rules_match_whole_labels():
    assert _score.score("https://fun.org")["source_type"] == "unknown"
    assert _score.score("https://microsoft.com")["source_type"] == "unknown"
    assert _score.score("https://news.un.org/x")["source_type"] == "official"
    assert _score.score("https://docs.python.org/3/")["tier"] == "B"  # matches SKILL.md


def test_extract_json_skips_braces_in_narration():
    text = 'I searched {two} sources. Final:\n{"findings": [{"claim": "x"}], "gaps": []}'
    assert extract_json(text)["findings"][0]["claim"] == "x"


class BadJsonThenGood:
    """First reply is not JSON; the repair turn returns JSON."""
    def __init__(self):
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        text = "Sorry, here you go: findings are..." if self.calls == 1 else '{"complexity": "simple", "subquestions": ["a"]}'
        return {"stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "text", "text": text}]}


def test_invalid_json_gets_one_repair_turn():
    events = []

    async def emit(e):
        events.append(e)

    client = BadJsonThenGood()
    data = run(Pipeline(client, emit).call("router", "fast", "<question>q</question>"))
    assert data["complexity"] == "simple" and client.calls == 2
    assert any(e["type"] == "log" and "invalid JSON" in e["msg"] for e in events)


def test_retry_on_overload(monkeypatch):
    import httpx
    from app import llm

    responses = [httpx.Response(529, json={"error": "overloaded"}, headers={"retry-after": "0"}),
                 httpx.Response(200, json={"content": [], "stop_reason": "end_turn", "usage": {}})]

    async def fake_post(*a, **kw):
        return responses.pop(0)

    client = llm.MessagesClient("test-key")
    monkeypatch.setattr(client.http, "post", fake_post)
    retries = []

    async def on_retry(n, d):
        retries.append(n)

    resp = run(client.create(model="m", system="s", messages=[], on_retry=on_retry))
    assert resp["stop_reason"] == "end_turn" and retries == [1]


def test_display_copy_hides_encrypted_content():
    from app.llm import for_display
    msg = [{"role": "assistant", "content": [{"type": "web_search_tool_result",
                                              "content": [{"url": "u", "encrypted_content": "SECRETBLOB"}]}]}]
    shown = for_display(msg)
    assert "SECRETBLOB" not in str(shown) and msg[0]["content"][0]["content"][0]["encrypted_content"] == "SECRETBLOB"
