"""The orchestration, explicit in code: a sequential pipeline with checked handoffs.

    Router ──▶ Researcher ──▶ Analyst ──▶ Writer ──▶ quality check
                   ▲              │          │
                   └── return ────┘          │   (max MAX_RETURNS per edge)
                                  ▲          │
                                  └─ return ─┘

Every handoff is: produce JSON → contract check in code → receiving agent's own
`handoff_check`. A failed check sends the work back once with what is missing.
Agents never talk to each other directly; only this module moves data.
"""
from __future__ import annotations

import json
import os
import time
from typing import Awaitable, Callable

from . import contracts as C
from .agents import AGENTS, MODEL_PLAN, MODELS
from .llm import run_agent, extract_json

MAX_RETURNS = int(os.getenv("MAX_RETURNS", "1"))
Emit = Callable[[dict], Awaitable[None]]


class Pipeline:
    def __init__(self, client, emit: Emit):
        self.client = client
        self.emit = emit
        self.usage: dict[str, dict] = {}

    # ---- helpers -------------------------------------------------------
    async def status(self, agent: str, status: str, step: str = ""):
        await self.emit({"type": "agent_status", "agent": agent, "status": status, "step": step})

    async def call(self, key: str, tier: str, user_content: str, searches: int = 3) -> dict:
        spec = AGENTS[key]
        model = MODELS[tier]
        tool_defs, impls = spec.tools(searches)
        for skill in spec.skills:
            await self.emit({"type": "skill_used", "agent": key, "skill": skill.name,
                             "version": skill.version, "description": skill.description})
        result = await run_agent(client=self.client, agent=key, model=model,
                                 system=spec.system_prompt(), user_content=user_content,
                                 tools=tool_defs, client_tools=impls, emit=self.emit)
        u = self.usage.setdefault(key, {"model": model, "input_tokens": 0, "output_tokens": 0})
        u["model"] = model
        u["input_tokens"] += result.usage["input_tokens"]
        u["output_tokens"] += result.usage["output_tokens"]
        await self.emit({"type": "usage", "agent": key, **u})
        try:
            data = extract_json(result.text)
        except Exception as exc:
            raise RuntimeError(f"{key} did not return valid JSON: {exc}") from exc
        await self.emit({"type": "agent_output", "agent": key, "output": data})
        return data

    async def handoff(self, src: str, dst: str, payload: dict, code_missing: list[str]):
        await self.emit({"type": "handoff", "from": src, "to": dst, "payload": payload,
                         "contract_ok": not code_missing, "missing": code_missing})

    # ---- steps ---------------------------------------------------------
    async def route(self, question: str) -> dict:
        await self.status("router", "working", "Hodnotím náročnost a dělím otázku")
        r = await self.call("router", "fast", f"<question>{question}</question>")
        missing = C.check_router(r)
        if missing:  # router is cheap: fall back to safe defaults instead of failing
            r.setdefault("subquestions", [question])
            if r.get("complexity") not in C.COMPLEXITY:
                r["complexity"] = "moderate"
            await self.emit({"type": "log", "level": "warn", "msg": f"Router output fixed: {missing}"})
        plan = MODEL_PLAN[r["complexity"]]
        r["model_plan"] = {k: MODELS[v] for k, v in plan.items() if k != "searches"}
        r["search_budget"] = plan["searches"]
        await self.status("router", "done", f"Náročnost: {r['complexity']}")
        return r

    async def research(self, question: str, route: dict, feedback: list[str] | None = None) -> dict:
        plan = MODEL_PLAN[route["complexity"]]
        step = "Dohledávám, co chtěl analytik" if feedback else "Hledám na webu a čtu zdroje"
        await self.status("researcher", "working", step)
        payload = {"question": question, "subquestions": route["subquestions"],
                   "language": route.get("language", "same as question")}
        content = f"<handoff from=\"router\">\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</handoff>"
        if feedback:
            content += ("\n\n<feedback from=\"analyst\">The analyst returned your work. Missing:\n- "
                        + "\n- ".join(feedback) + "\nKeep your previous findings and add what is missing.</feedback>")
        r = await self.call("researcher", plan["researcher"], content, searches=plan["searches"])
        await self.status("researcher", "done", f"{len(r.get('findings', []))} zjištění, "
                                                f"{len({f.get('source_url') for f in r.get('findings', [])})} zdrojů")
        return r

    async def analyse(self, question: str, route: dict, research: dict, feedback: list[str] | None = None) -> dict:
        plan = MODEL_PLAN[route["complexity"]]
        step = "Doplňuji, co chtěl writer" if feedback else "Hodnotím zdroje a ověřuji tvrzení"
        await self.status("analyst", "working", step)
        content = (f"<question>{question}</question>\n<handoff from=\"researcher\">\n"
                   f"{json.dumps(research, ensure_ascii=False, indent=2)}\n</handoff>")
        if feedback:
            content += ("\n\n<feedback from=\"writer\">The writer returned your work. Missing:\n- "
                        + "\n- ".join(feedback) + "</feedback>")
        a = await self.call("analyst", plan["analyst"], content, searches=plan["searches"])
        await self.status("analyst", "done", f"{len(a.get('verified_facts', []))} ověřených faktů")
        return a

    async def write(self, question: str, route: dict, analysis: dict, sources: list[dict]) -> dict:
        plan = MODEL_PLAN[route["complexity"]]
        await self.status("writer", "working", "Píšu odpověď")
        payload = {"question": question, "language": route.get("language"),
                   "analysis": analysis, "sources": sources}
        content = f"<handoff from=\"analyst\">\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</handoff>"
        w = await self.call("writer", plan["writer"], content)
        await self.status("writer", "done", "Odpověď hotová")
        return w

    # ---- the pipeline --------------------------------------------------
    async def run(self, question: str) -> dict:
        t0 = time.monotonic()
        route = await self.route(question)
        await self.handoff("router", "researcher", {"subquestions": route["subquestions"],
                                                    "complexity": route["complexity"],
                                                    "model_plan": route["model_plan"]}, [])

        # Researcher -> Analyst (code contract, then analyst's own check)
        research = await self.research(question, route)
        returns = 0
        while True:
            missing = C.check_research(research)
            await self.handoff("researcher", "analyst", research, missing)
            if missing and returns < MAX_RETURNS:
                returns += 1
                await self.status("researcher", "returned", "Kontrakt nesplněn: " + "; ".join(missing))
                research = await self.research(question, route, feedback=missing)
                continue
            analysis = await self.analyse(question, route, research)
            check = analysis.get("handoff_check") or {}
            if not check.get("ok", True) and returns < MAX_RETURNS:
                returns += 1
                await self.emit({"type": "handoff_return", "from": "analyst", "to": "researcher",
                                 "missing": check.get("missing", [])})
                await self.status("researcher", "returned", "Analytik chce doplnit")
                research = await self.research(question, route, feedback=check.get("missing", []))
                continue
            break

        # Number the sources once, here, so citations [n] are stable.
        sources = self.number_sources(research, analysis)

        # Analyst -> Writer
        w_returns = 0
        while True:
            missing = C.check_analysis(analysis)
            await self.handoff("analyst", "writer", {"verified_facts": analysis.get("verified_facts"),
                                                     "outline": analysis.get("outline"),
                                                     "sources": sources}, missing)
            if missing and w_returns < MAX_RETURNS:
                w_returns += 1
                await self.status("analyst", "returned", "Kontrakt nesplněn: " + "; ".join(missing))
                analysis = await self.analyse(question, route, research, feedback=missing)
                sources = self.number_sources(research, analysis)
                continue
            writer = await self.write(question, route, analysis, sources)
            check = writer.get("handoff_check") or {}
            if not check.get("ok", True) and w_returns < MAX_RETURNS:
                w_returns += 1
                await self.emit({"type": "handoff_return", "from": "writer", "to": "analyst",
                                 "missing": check.get("missing", [])})
                await self.status("analyst", "returned", "Writer chce doplnit")
                analysis = await self.analyse(question, route, research, feedback=check.get("missing", []))
                sources = self.number_sources(research, analysis)
                continue
            break

        missing = C.check_writer(writer)
        if missing:
            await self.emit({"type": "log", "level": "warn", "msg": f"Writer contract: {missing}"})
        markdown = writer.get("answer_markdown", "")
        quality = C.quality_report(markdown, sources)
        result = {"answer_markdown": markdown, "sources": sources, "quality": quality,
                  "complexity": route["complexity"], "usage": self.usage,
                  "duration_s": round(time.monotonic() - t0, 1)}
        await self.emit({"type": "final", **result})
        return result

    @staticmethod
    def number_sources(research: dict, analysis: dict) -> list[dict]:
        scores = {s.get("url"): s for s in analysis.get("source_scores", [])}
        seen, out = set(), []
        for item in (research.get("sources") or []) + [
                {"url": f.get("source_url"), "title": f.get("source_title")} for f in research.get("findings", [])]:
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sc = scores.get(url, {})
            out.append({"n": len(out) + 1, "url": url, "title": item.get("title") or url,
                        "tier": sc.get("tier"), "score": sc.get("score")})
        for f in analysis.get("verified_facts", []):  # sources the analyst added while verifying
            for url in f.get("sources", []):
                if url not in seen and str(url).startswith("http"):
                    seen.add(url)
                    sc = scores.get(url, {})
                    out.append({"n": len(out) + 1, "url": url, "title": url,
                                "tier": sc.get("tier"), "score": sc.get("score")})
        return out
