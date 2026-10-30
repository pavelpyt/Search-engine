import asyncio
import json
import os
import socket

os.environ["MOCK"] = "1"
os.environ.setdefault("MOCK_FETCH", "1")

import pytest

from app import contracts as C
from app import evidence as E
from app import orchestrator as O
from app import tools as T
from app.llm import extract_json, run_agent
from app.mock import MockClient
from app.orchestrator import Pipeline
from app.skills import list_skills, load_skill
from app.tools import _score


def run(coro):
    return asyncio.run(coro)


def collect():
    events = []

    async def emit(e):
        events.append(e)
    return events, emit


def text_resp(obj_or_text, stop="end_turn"):
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
    return {"stop_reason": stop, "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": text}]}


class Scripted:
    """Per-agent scripted replies; falls back to the mock for agents without a script."""
    def __init__(self, **scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.mock = MockClient()
        self.calls = {}

    async def create(self, **kw):
        agent = kw["system"].split("AGENT: ")[1].split()[0]
        self.calls[agent] = self.calls.get(agent, 0) + 1
        if self.scripts.get(agent):
            return self.scripts[agent].pop(0)
        return await self.mock.create(**kw)


ROUTER = text_resp({"complexity": "simple", "goal": "g", "language": "en", "subquestions": ["a", "b"]})


# ---------------------------------------------------------------- basics
def test_skills_have_frontmatter():
    names = {s["name"] for s in list_skills()}
    assert {"source-evaluation", "plain-writing", "question-decomposition"} <= names
    s = load_skill("source-evaluation")
    assert "evaluate_source" in s.body and "prior" in s.body and s.description


def test_score_source_tiers():
    assert _score.score("https://www.czso.cz/x", "2026")["tier"] == "A"
    assert _score.score("https://www.reddit.com/r/x")["tier"] == "D"
    assert _score.score("http://example.com/old", "2015")["score"] < 50


def test_domain_rules_match_whole_labels():
    assert _score.score("https://fun.org")["source_type"] == "unknown"
    assert _score.score("https://microsoft.com")["source_type"] == "unknown"
    assert _score.score("https://news.un.org/x")["source_type"] == "official"
    assert _score.score("https://docs.python.org/3/")["tier"] == "B"


def test_research_contract_needs_quotes():
    assert C.check_research({"findings": []})
    good = {"findings": [{"claim": "a", "source_url": "https://a", "quote": "q a long enough"},
                         {"claim": "b", "source_url": "https://b", "quote": "q b long enough"},
                         {"claim": "c", "source_url": "https://a", "quote": "q c long enough"}]}
    assert C.check_research(good) == []
    good["findings"][0]["quote"] = ""
    assert any("quote" in m for m in C.check_research(good))


def test_extract_json():
    assert extract_json('text\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('I searched {two} sources.\n{"findings": [{"claim": "x"}]}')["findings"][0]["claim"] == "x"


def test_full_pipeline_mock_completed_with_one_return():
    events, emit = collect()
    result = run(Pipeline(MockClient(), emit).run("Jaký je stav AI v Evropě v roce 2026?"))
    assert result["status"] == "COMPLETED", result["status_reasons"]
    assert sum(1 for e in events if e["type"] == "handoff_return") == 1
    assert any(e["type"] == "skill_loaded" and e["skill"] == "source-evaluation" for e in events)
    sr = next(e for e in events if e["type"] == "skill_result")
    assert sr["executed"] and sr["not_scored"] == []
    assert result["evidence"]["evidence_support"] == {"supported": 2, "of": 2}


# ---------------------------------------------------------------- T01-T03 gates
def test_T01_empty_research_after_returns_is_insufficient(monkeypatch):
    monkeypatch.setattr(O, "MAX_RETURNS", 0)
    empty = text_resp({"findings": [], "sources": [], "gaps": ["nothing public"]})
    client = Scripted(router=[ROUTER], researcher=[empty])
    events, emit = collect()
    r = run(Pipeline(client, emit).run("q?"))
    assert r["status"] == "INSUFFICIENT_EVIDENCE"
    assert "analyst" not in client.calls and "writer" not in client.calls
    v = [e for e in events if e["type"] == "validation_result"][-1]
    assert v["accepted"] == "rejected" and v["next_action"] == "abort"


def test_T02_invalid_analysis_after_returns_never_reaches_writer(monkeypatch):
    monkeypatch.setattr(O, "MAX_RETURNS", 0)
    bad = text_resp({"handoff_check": {"ok": True, "missing": []}, "source_scores": [{"url": "u"}],
                     "verified_facts": [{"id": "F1", "fact": "x", "verification_status": "UNSUPPORTED", "based_on": []}],
                     "outline": ["a"]})
    # analyst: first turn uses the mock's evaluate_source calls, second turn returns the bad analysis
    mock = MockClient()

    class C2(Scripted):
        async def create(self, **kw):
            agent = kw["system"].split("AGENT: ")[1].split()[0]
            self.calls[agent] = self.calls.get(agent, 0) + 1
            if agent == "analyst" and any(m["role"] == "assistant" for m in kw["messages"]):
                return bad
            if agent == "router":
                return ROUTER
            return await mock.create(**kw)
    client = C2()
    events, emit = collect()
    r = run(Pipeline(client, emit).run("Finding question"))
    assert r["status"] == "INSUFFICIENT_EVIDENCE"
    assert "writer" not in client.calls


def test_T03_writer_with_unbacked_citation_is_not_completed():
    mock = MockClient()

    class C3(Scripted):
        async def create(self, **kw):
            agent = kw["system"].split("AGENT: ")[1].split()[0]
            if agent == "writer":
                return text_resp({"handoff_check": {"ok": True}, "used_facts": ["F2"],
                                  "answer_markdown": "## Summary\n\n" + "Claim backed by F1 only [1]. " * 10
                                                     + "\n\n## Open questions\n\n- x"})
            return await mock.create(**kw)
    events, emit = collect()
    r = run(Pipeline(C3(), emit).run("Q about things"))
    assert r["status"] == "PARTIAL"
    assert r["evidence"]["citations_not_backed_by_used_facts"] == [1]


# ---------------------------------------------------------------- T04-T07 API turn handling
def test_T04_max_tokens_with_valid_json_is_not_accepted():
    client = Scripted(router=[text_resp({"complexity": "simple", "subquestions": ["a"]}, stop="max_tokens"),
                              text_resp({"complexity": "simple", "subquestions": ["a"]}, stop="max_tokens")])
    events, emit = collect()
    with pytest.raises(Exception, match="did not complete"):
        run(Pipeline(client, emit).call("router", "fast", "<question>q</question>"))
    assert client.calls["router"] == 2  # original + one repair, never accepted


def test_T05_max_turns_with_pause_turn_fails():
    pause = {"stop_reason": "pause_turn", "usage": {}, "content": [{"type": "text", "text": "{\"a\": 1}"}]}
    client = Scripted(router=[pause] * 20)
    events, emit = collect()
    r = run(Pipeline(client, emit).run("q?"))
    assert r["status"] == "FAILED"


def test_T06_search_error_inside_http_200_is_reported():
    resp = {"stop_reason": "end_turn", "usage": {}, "content": [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1",
         "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}},
        {"type": "text", "text": "{}"}]}
    client = Scripted(router=[resp])
    events, emit = collect()
    run(run_agent(client=client, agent="router", model="m", system="AGENT: router", user_content="q",
                  tools=[], client_tools={}, emit=emit))
    res = next(e for e in events if e["type"] == "tool_result")
    assert res["result"] == {"error": "max_uses_exceeded"}


def test_T07_each_parallel_tool_use_gets_its_own_result():
    first = {"stop_reason": "tool_use", "usage": {}, "content": [
        {"type": "tool_use", "id": "a1", "name": "echo", "input": {"v": 1}},
        {"type": "tool_use", "id": "a2", "name": "echo", "input": {"v": 2}}]}
    client = Scripted(router=[first, text_resp("{}")])
    seen = []

    class Rec(Scripted):
        async def create(self, **kw):
            seen.append(json.loads(json.dumps(kw["messages"])))
            return await super().create(**kw)
    client = Rec(router=[first, text_resp("{}")])

    async def echo(args):
        return f"echo{args['v']}"
    events, emit = collect()
    run(run_agent(client=client, agent="router", model="m", system="AGENT: router", user_content="q",
                  tools=[], client_tools={"echo": echo}, emit=emit))
    results = seen[1][-1]["content"]
    assert [(r["tool_use_id"], r["content"]) for r in results] == [("a1", "echo1"), ("a2", "echo2")]


def test_tool_loop_sends_server_blocks_back_unchanged():
    first = {"stop_reason": "tool_use", "usage": {}, "content": [
        {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "q"}},
        {"type": "web_search_tool_result", "tool_use_id": "srv1",
         "content": [{"type": "web_search_result", "url": "https://x", "title": "X", "encrypted_content": "ENC"}]},
        {"type": "tool_use", "id": "tu1", "name": "echo", "input": {"v": 1}}]}
    seen = []

    class Rec(Scripted):
        async def create(self, **kw):
            seen.append(json.loads(json.dumps(kw["messages"])))
            return await super().create(**kw)

    async def echo(args):
        return "ok"
    events, emit = collect()
    run(run_agent(client=Rec(router=[first, text_resp("{}")]), agent="router", model="m", system="AGENT: router",
                  user_content="q", tools=[], client_tools={"echo": echo}, emit=emit))
    assert seen[1][1]["content"][1]["content"][0]["encrypted_content"] == "ENC"
    assert "ENC" not in json.dumps([e for e in events if e["type"] == "api_request"])


# ---------------------------------------------------------------- T08-T11 evidence
DOC = {"text": "Eurostat: In 2024, 13.5 % of EU enterprises used AI technologies.", "truncated": False}


def test_T08_citation_coverage_is_not_evidence_support():
    findings = [{"id": "f1", "source_url": "https://a.org/x", "quote": "13.5 % of EU enterprises used AI"},
                {"id": "f2", "source_url": "https://b.org/y", "quote": "unrelated sentence here"}]
    E.annotate_findings({"findings": findings}, {"https://a.org/x": DOC})
    sources = E.number_sources(findings, [], {})
    ledger = E.build_ledger(findings, {"verified_facts": [
        {"id": "F1", "fact": "13.5 % used AI", "based_on": ["f1"], "verification_status": "SUPPORTED"}]}, sources)
    md = "## Summary\n\nSomething unrelated [2].\n\n## Open questions\n\n- x"
    assert C.quality_report(md, sources)["checks"][0]["ok"]          # mechanically fine
    ev = E.check_writer_evidence({"used_facts": ["F1"]}, ledger, md)
    assert ev["citations_not_backed_by_used_facts"] == [2] and not ev["ok"]


def test_T09_missing_quote_is_not_verified_and_caps_confidence():
    findings = [{"id": "f1", "source_url": "https://a.org/x", "quote": "a sentence that is not on the page"}]
    E.annotate_findings({"findings": findings}, {"https://a.org/x": DOC})
    assert findings[0]["quote_check"] == "not_found" and not findings[0]["quote_verified"]
    ledger = E.build_ledger(findings, {"verified_facts": [{"id": "F1", "fact": "x", "based_on": ["f1"],
                                                           "verification_status": "SUPPORTED", "confidence": "high"}]},
                            E.number_sources(findings, [], {}))
    assert ledger[0]["confidence"] == "low" and "quote not found in the fetched text" in ledger[0]["limitations"]
    assert E.verify_quote({"text": "abc", "truncated": True}, "not in the first part") == "not_in_fetched_part"


def test_T10_same_site_or_same_quote_is_not_independent():
    docs = {"https://www.a.org/1": DOC, "https://news.a.org/2": DOC, "https://b.org/3": DOC}
    q = "13.5 % of EU enterprises used AI"
    findings = [{"id": "f1", "source_url": "https://www.a.org/1", "quote": q},
                {"id": "f2", "source_url": "https://news.a.org/2", "quote": "In 2024, 13.5 % of EU"},
                {"id": "f3", "source_url": "https://b.org/3", "quote": q}]
    E.annotate_findings({"findings": findings}, docs)
    src = E.number_sources(findings, [], {})
    same_site = E.build_ledger(findings, {"verified_facts": [{"id": "F1", "fact": "x", "based_on": ["f1", "f2"],
                                                              "verification_status": "SUPPORTED"}]}, src)
    same_quote = E.build_ledger(findings, {"verified_facts": [{"id": "F1", "fact": "x", "based_on": ["f1", "f3"],
                                                               "verification_status": "SUPPORTED"}]}, src)
    assert not same_site[0]["independent_corroboration"] and not same_quote[0]["independent_corroboration"]


def test_T11_one_good_source_gives_partial_not_completed(monkeypatch):
    monkeypatch.setattr(O, "MAX_RETURNS", 0)
    one = text_resp({"findings": [{"id": "f1", "claim": "c", "source_url": "https://www.oecd.org/en/topics/artificial-intelligence.html",
                                   "source_title": "OECD", "quote": "Key facts and figures about the topic, published 2026."}],
                     "sources": [], "gaps": []})
    mock = MockClient()

    class C11(Scripted):
        async def create(self, **kw):
            agent = kw["system"].split("AGENT: ")[1].split()[0]
            if agent == "researcher" and not any(m["role"] == "assistant" for m in kw["messages"]):
                return {"stop_reason": "tool_use", "usage": {}, "content": [
                    {"type": "tool_use", "id": "t1", "name": "fetch_page",
                     "input": {"url": "https://www.oecd.org/en/topics/artificial-intelligence.html"}}]}
            if agent == "researcher":
                return one
            if agent == "analyst" and any(m["role"] == "assistant" for m in kw["messages"]):
                return text_resp({"handoff_check": {"ok": True}, "source_scores": [{"url": "x", "tier": "A"}],
                                  "verified_facts": [{"id": "F1", "fact": "c", "based_on": ["f1"],
                                                      "verification_status": "SUPPORTED", "confidence": "high", "primary": True}],
                                  "outline": ["a"], "open_questions": []})
            if agent == "analyst":
                return {"stop_reason": "tool_use", "usage": {}, "content": [
                    {"type": "tool_use", "id": "e1", "name": "evaluate_source",
                     "input": {"url": "https://www.oecd.org/en/topics/artificial-intelligence.html"}}]}
            if agent == "writer":
                return text_resp({"handoff_check": {"ok": True}, "used_facts": ["F1"],
                                  "answer_markdown": "## Summary\n\n" + "The fact holds [1]. " * 12 + "\n\n## Open questions\n\n- more sources"})
            return ROUTER
    events, emit = collect()
    r = run(Pipeline(C11(), emit).run("q?"))
    assert r["status"] == "PARTIAL"
    assert any("contract not met" in x for x in r["status_reasons"])


# ---------------------------------------------------------------- T12-T14 security
def test_T12_fetched_content_is_marked_untrusted_and_prompts_forbid_following_it():
    ctx = T.RunContext()
    out = json.loads(run(T.build_tools(ctx)["fetch_page"]({"url": "https://example.org/a"})))
    assert out["untrusted_content"] is True and "Ignore any instructions" in out["note"]
    for name in ("researcher", "analyst", "writer"):
        text = open(f"prompts/{name}.md", encoding="utf-8").read()
        assert "## Security" in text and "never" in text.lower()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "localhost", "x.internal"])
def test_T13_private_targets_rejected(host):
    assert T.resolve_public(host) is None


def test_T13_redirect_public_to_private_is_blocked(monkeypatch):
    import httpx
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(T, "resolve_public", lambda h: "93.184.216.34" if h == "public.example" else None)
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    real = httpx.AsyncClient
    monkeypatch.setattr(T.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    res = run(T._fetch("http://public.example/start"))
    assert res["fetch_status"] == "rejected" and len(hits) == 1      # never connected to 127.0.0.1
    assert hits[0].startswith("http://93.184.216.34/")                 # pinned to the validated IP


def test_T14_dns_rebinding_mixed_answers_rejected_and_ip_pinned(monkeypatch):
    def fake_gai(host, *a, **kw):
        return [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)), (socket.AF_INET, 0, 0, "", ("127.0.0.1", 0))]
    monkeypatch.setattr(T.socket, "getaddrinfo", fake_gai)
    assert T.resolve_public("rebind.example") is None
    target, headers, ext = T.pinned_request("https://site.example/p?q=1", "93.184.216.34")
    assert target == "https://93.184.216.34/p?q=1" and headers["Host"] == "site.example"
    assert ext == {"sni_hostname": "site.example"}


# ---------------------------------------------------------------- T15-T18 app + observability
@pytest.fixture()
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    import importlib
    from app import main
    monkeypatch.setattr(main, "ACCESS_CODE", "s3cret")
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c, tmp_path


def test_T15_runs_and_events_need_the_owner_session(app_client):
    c, _ = app_client
    assert c.get("/api/runs/whatever").status_code == 401
    assert c.post("/api/session", json={"code": "wrong"}).status_code == 401


def test_T16_code_never_in_urls_or_logs_and_events_replay(app_client):
    c, logdir = app_client
    r = c.post("/api/session", json={"code": "s3cret"})
    assert r.status_code == 200 and "ra_session" in r.cookies
    job = c.post("/api/jobs", json={"question": "Jaký je stav AI v Evropě?"}).json()["job_id"]
    urls = [f"/api/jobs/{job}/events"]
    events = []
    with c.stream("GET", urls[0]) as s:
        for line in s.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
                if events[-1]["type"] == "run_finished":
                    break
    assert any(e["type"] == "final" for e in events)
    assert all("s3cret" not in u for u in urls)
    assert "s3cret" not in "".join(p.read_text() for p in logdir.glob("*.jsonl"))
    assert c.get(f"/api/runs/{job}").status_code == 200
    other = c.__class__(c.app)
    assert other.get(f"/api/runs/{job}").status_code == 401           # another browser, no session
    # replay from Last-Event-ID
    with c.stream("GET", urls[0], headers={"last-event-id": "3"}) as s:
        first = next(l for l in s.iter_lines() if l.startswith("id:"))
    assert first == "id: 4"


def test_T17_every_api_call_has_its_own_request_snapshot():
    events, emit = collect()
    run(Pipeline(MockClient(), emit).run("Jaký je stav AI?"))
    req = [e for e in events if e["type"] == "api_request" and e["agent"] == "researcher"]
    assert len(req) >= 2 and len(req[0]["messages"]) < len(req[1]["messages"])
    assert {e["call_id"] for e in events if e["type"] == "api_response"} >= {e["call_id"] for e in req}


def test_T18_skill_loaded_but_not_invoked_is_reported_and_returned():
    mock = MockClient()

    class NoTool(Scripted):
        async def create(self, **kw):
            agent = kw["system"].split("AGENT: ")[1].split()[0]
            if agent == "analyst":   # answers straight away, never calls evaluate_source
                return await mock.create(**{**kw, "messages": kw["messages"] + [{"role": "assistant", "content": []}]})
            return await mock.create(**kw)
    events, emit = collect()
    r = run(Pipeline(NoTool(), emit).run("Finding 4 question"))
    sr = [e for e in events if e["type"] == "skill_result"]
    assert sr and not sr[0]["executed"]
    assert any(e["type"] == "skill_loaded" for e in events)
    gate = [e for e in events if e["type"] == "validation_result" and e["gate"] == "analyst_to_writer"]
    assert any("evaluate_source not called" in m for e in gate for m in e["code_check"]["missing"])
    assert r["status"] != "COMPLETED"


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


def test_invalid_json_gets_one_repair_turn():
    client = Scripted(router=[text_resp("Sorry, here you go"), text_resp({"complexity": "simple", "subquestions": ["a"]})])
    events, emit = collect()
    data = run(Pipeline(client, emit).call("router", "fast", "<question>q</question>"))
    assert data["complexity"] == "simple" and client.calls["router"] == 2
