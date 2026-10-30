"""The orchestration, explicit in code: a sequential pipeline with gated handoffs.

    Router ──▶ Researcher ──▶ Analyst ──▶ Writer ──▶ evidence + output audit
                   ▲              │          │
                   └── return ────┘          │   (max MAX_RETURNS per edge)
                                  ▲          │
                                  └─ return ─┘

Every handoff is a gate: JSON → contract check in code → the receiving agent's
own `handoff_check`. A failed gate is returned once; when the returns are used
up the run never pretends it is complete: it continues as PARTIAL (a supported
subset exists) or stops as INSUFFICIENT_EVIDENCE. Technical failures end as
FAILED. Agents never talk to each other; only this module moves data.
"""
from __future__ import annotations

import json
import os
import time
from typing import Awaitable, Callable

from . import contracts as C
from . import evidence as E
from .agents import AGENTS, MODEL_PLAN, MODELS, estimate_cost
from .llm import COMPLETE_STOPS, AgentIncompleteError, extract_json, run_agent
from .tools import RunContext, build_tools

MAX_RETURNS = int(os.getenv("MAX_RETURNS", "1"))
Emit = Callable[[dict], Awaitable[None]]

COMPLETED, PARTIAL, INSUFFICIENT, FAILED = "COMPLETED", "PARTIAL", "INSUFFICIENT_EVIDENCE", "FAILED"


class Pipeline:
    def __init__(self, client, emit: Emit):
        self.client = client
        self.emit = emit
        self.usage: dict[str, dict] = {}
        self.ctx = RunContext()
        self.impls = build_tools(self.ctx)
        self.partial_reasons: list[str] = []
        self.t0 = time.monotonic()

    # ---- helpers -------------------------------------------------------
    async def status(self, agent: str, status: str, step: str = ""):
        await self.emit({"type": "agent_status", "agent": agent, "status": status, "step": step})

    async def call(self, key: str, tier: str, user_content: str, searches: int = 3) -> dict:
        """Run one agent and return its JSON. Incomplete turns are never accepted."""
        spec = AGENTS[key]
        model = MODELS[tier]
        tool_defs, impls = spec.tools(searches, self.impls)
        for skill in spec.skills:  # loaded into the prompt; execution is reported separately
            await self.emit({"type": "skill_loaded", "agent": key, "skill": skill.name,
                             "version": skill.version, "description": skill.description})
        system = spec.system_prompt()
        result = await run_agent(client=self.client, agent=key, model=model, system=system,
                                 user_content=user_content, tools=tool_defs, client_tools=impls, emit=self.emit)
        await self.add_usage(key, model, result.usage)
        if result.stop_reason in {"refusal"}:
            raise AgentIncompleteError(key, result.stop_reason, "The model refused.")
        data, problem = None, None
        if result.stop_reason in COMPLETE_STOPS:
            try:
                data = extract_json(result.text)
            except Exception as exc:
                problem = f"not one valid JSON object ({exc})"
        else:
            problem = f"turn ended with stop_reason={result.stop_reason}"
        if data is None:
            # One bounded repair turn in the same conversation.
            await self.emit({"type": "log", "level": "warn", "msg": f"{key}: {problem}; one repair turn"})
            hint = " Your reply was cut off: be much more concise." if result.stop_reason == "max_tokens" else ""
            repair = result.messages + [{"role": "user", "content":
                     f"Your last reply was incomplete or invalid: {problem}.{hint} "
                     "Reply again with ONLY the complete JSON object described in your instructions."}]
            result = await run_agent(client=self.client, agent=key, model=model, system=system, messages=repair,
                                     tools=tool_defs, client_tools=impls, emit=self.emit, max_turns=2)
            await self.add_usage(key, model, result.usage)
            if result.stop_reason not in COMPLETE_STOPS:
                raise AgentIncompleteError(key, result.stop_reason, "Repair turn did not complete.")
            try:
                data = extract_json(result.text)
            except Exception as exc:
                raise AgentIncompleteError(key, result.stop_reason, f"Still no valid JSON: {exc}") from exc
        await self.emit({"type": "agent_output", "agent": key, "output": data})
        return data

    async def add_usage(self, key: str, model: str, usage: dict):
        u = self.usage.setdefault(key, {"model": model, "input_tokens": 0, "output_tokens": 0,
                                        "web_searches": 0, "cost_usd": 0.0})
        u["model"] = model
        for k in ("input_tokens", "output_tokens", "web_searches"):
            u[k] += usage.get(k, 0)
        u["cost_usd"] = estimate_cost(model, u["input_tokens"], u["output_tokens"], u["web_searches"])
        await self.emit({"type": "usage", "agent": key, **u})

    async def handoff(self, src: str, dst: str, payload: dict, code_missing: list[str]):
        await self.emit({"type": "handoff", "from": src, "to": dst, "payload": payload,
                         "contract_ok": not code_missing, "missing": code_missing})

    async def validation(self, gate: str, src: str, dst: str, attempt: int, code_missing: list[str],
                         agent_check: dict | None, accepted: str, next_action: str, **extra):
        """One auditable report per handoff attempt: code check + receiving agent's check + decision."""
        await self.emit({"type": "validation_result", "gate": gate, "from": src, "to": dst, "attempt": attempt,
                         "code_check": {"ok": not code_missing, "missing": code_missing},
                         "agent_check": agent_check, "accepted": accepted, "next_action": next_action, **extra})

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

    async def research(self, question: str, route: dict, feedback: list[str] | None = None,
                       previous: dict | None = None) -> dict:
        plan = MODEL_PLAN[route["complexity"]]
        await self.status("researcher", "working", "Dohledávám, co chybělo" if feedback else "Hledám na webu a čtu zdroje")
        payload = {"question": question, "goal": route.get("goal"), "subquestions": route["subquestions"],
                   "language": route.get("language", "same as question")}
        content = f"<handoff from=\"router\">\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</handoff>"
        if feedback:
            content += ("\n\n<feedback from=\"orchestrator/analyst\">Your work was returned. Missing:\n- "
                        + "\n- ".join(feedback) + "\nKeep your previous findings (below) and add what is missing. "
                        "Return the complete merged JSON.</feedback>")
            if previous:
                content += ("\n\n<previous_research>\n" + json.dumps(previous, ensure_ascii=False, indent=2)
                            + "\n</previous_research>")
        r = await self.call("researcher", plan["researcher"], content, searches=plan["searches"])
        findings = E.annotate_findings(r, self.ctx.docs)
        verified = sum(1 for f in findings if f["quote_verified"])
        await self.status("researcher", "done", f"{len(findings)} zjištění, {verified} citátů ověřeno v textu")
        return r

    async def analyse(self, question: str, route: dict, research: dict, feedback: list[str] | None = None,
                      previous: dict | None = None) -> dict:
        plan = MODEL_PLAN[route["complexity"]]
        await self.status("analyst", "working", "Doplňuji, co chtěl writer" if feedback else "Hodnotím zdroje a ověřuji tvrzení")
        content = (f"<question>{question}</question>\n<goal>{route.get('goal', '')}</goal>\n"
                   f"<handoff from=\"researcher\">\n{json.dumps(research, ensure_ascii=False, indent=2)}\n</handoff>")
        if feedback:
            content += ("\n\n<feedback from=\"orchestrator/writer\">Your work was returned. Missing:\n- "
                        + "\n- ".join(feedback) + "\nReturn the complete corrected JSON.</feedback>")
            if previous:
                content += ("\n\n<previous_analysis>\n" + json.dumps(previous, ensure_ascii=False, indent=2)
                            + "\n</previous_analysis>")
        a = await self.call("analyst", plan["analyst"], content, searches=plan["searches"])
        urls = sorted({f.get("source_url") for f in research.get("findings", []) if f.get("source_url")})
        not_scored = [u for u in urls if u not in self.ctx.scored]
        await self.emit({"type": "skill_result", "agent": "analyst", "skill": "source-evaluation",
                         "tool": "evaluate_source", "invocations": len(self.ctx.scored),
                         "sources_total": len(urls), "not_scored": not_scored, "executed": bool(self.ctx.scored)})
        a["_not_scored"] = not_scored
        await self.status("analyst", "done", f"{len(a.get('verified_facts', []))} faktů ohodnoceno")
        return a

    async def write(self, question: str, route: dict, ledger: list[dict], sources: list[dict],
                    outline: list, open_questions: list) -> dict:
        plan = MODEL_PLAN[route["complexity"]]
        await self.status("writer", "working", "Píšu odpověď")
        payload = {"question": question, "goal": route.get("goal"), "complexity": route["complexity"],
                   "language": route.get("language"), "outline": outline,
                   "facts": [f for f in ledger if f["verification_status"] in E.SUPPORTING],
                   "not_supported": [{"fact_id": f["fact_id"], "statement": f["statement"],
                                      "status": f["verification_status"]} for f in ledger
                                     if f["verification_status"] not in E.SUPPORTING],
                   "open_questions": open_questions,
                   "sources": [{"n": s["n"], "title": s["title"], "url": s["url"], "tier": s["tier"]} for s in sources]}
        content = f"<handoff from=\"analyst\">\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n</handoff>"
        w = await self.call("writer", plan["writer"], content)
        await self.status("writer", "done", "Odpověď hotová")
        return w

    # ---- the pipeline --------------------------------------------------
    async def run(self, question: str) -> dict:
        try:
            return await self._run(question)
        except Exception as exc:  # technical failure ≠ lack of evidence
            await self.emit({"type": "error", "msg": str(exc)})
            return await self.finish(FAILED, [str(exc)], "", [], [], None, None, None)

    async def _run(self, question: str) -> dict:
        route = await self.route(question)
        await self.handoff("router", "researcher", {"goal": route.get("goal"), "subquestions": route["subquestions"],
                                                    "complexity": route["complexity"],
                                                    "model_plan": route["model_plan"]}, [])

        # --- gate 1: Researcher -> Analyst
        research = await self.research(question, route)
        returns, attempt = 0, 1
        while True:
            missing = C.check_research(research)
            await self.handoff("researcher", "analyst", research, missing)
            if missing:
                if returns < MAX_RETURNS:
                    returns += 1
                    await self.validation("researcher_to_analyst", "researcher", "analyst", attempt, missing, None,
                                          "rejected", "retry")
                    await self.status("researcher", "returned", "Kontrakt nesplněn: " + "; ".join(missing))
                    research = await self.research(question, route, feedback=missing, previous=research)
                    attempt += 1
                    continue
                usable = [f for f in research.get("findings", []) if f.get("source_url")]
                if not usable:
                    await self.validation("researcher_to_analyst", "researcher", "analyst", attempt, missing, None,
                                          "rejected", "abort")
                    return await self.insufficient(route, research, ["Researcher found no sourced findings."])
                self.partial_reasons.append("Research contract not met after returns: " + "; ".join(missing))
            analysis = await self.analyse(question, route, research)
            check = analysis.get("handoff_check") or {"ok": True, "missing": []}
            if not check.get("ok", True) and returns < MAX_RETURNS:
                returns += 1
                await self.validation("researcher_to_analyst", "researcher", "analyst", attempt, missing, check,
                                      "rejected", "retry")
                await self.emit({"type": "handoff_return", "from": "analyst", "to": "researcher",
                                 "missing": check.get("missing", [])})
                await self.status("researcher", "returned", "Analytik chce doplnit")
                research = await self.research(question, route, feedback=check.get("missing", []), previous=research)
                attempt += 1
                continue
            if not check.get("ok", True):
                self.partial_reasons.append("Analyst still missing: " + "; ".join(check.get("missing", [])))
            accepted = "accepted" if not missing and check.get("ok", True) else "partial"
            await self.validation("researcher_to_analyst", "researcher", "analyst", attempt, missing, check,
                                  accepted, "continue" if accepted == "accepted" else "continue_partial")
            break

        # --- gate 2: Analyst -> Writer (ledger built in code)
        w_returns, attempt = 0, 1
        while True:
            findings = research.get("findings", [])
            missing = C.check_analysis(analysis, {f["id"] for f in findings})
            if analysis.get("_not_scored"):
                missing.append(f"evaluate_source not called for: {analysis['_not_scored']}")
            scores = {**self.ctx.scored, **{s.get("url"): s for s in analysis.get("source_scores", []) if s.get("url")}}
            sources = E.number_sources(findings, [], scores)
            ledger = E.build_ledger(findings, analysis, sources)
            supported = [f for f in ledger if f["verification_status"] in E.SUPPORTING]
            await self.handoff("analyst", "writer", {"facts": ledger, "outline": analysis.get("outline"),
                                                     "sources": sources}, missing)
            if missing and w_returns < MAX_RETURNS:
                w_returns += 1
                await self.validation("analyst_to_writer", "analyst", "writer", attempt, missing, None, "rejected", "retry")
                await self.status("analyst", "returned", "Kontrakt nesplněn: " + "; ".join(missing))
                analysis = await self.analyse(question, route, research, feedback=missing, previous=analysis)
                attempt += 1
                continue
            if missing:
                if not supported:
                    await self.validation("analyst_to_writer", "analyst", "writer", attempt, missing, None,
                                          "rejected", "abort", ledger_size=len(ledger))
                    return await self.insufficient(route, research, ["No fact passed the evidence check."] + missing,
                                                   sources, ledger)
                self.partial_reasons.append("Analysis contract not met after returns: " + "; ".join(missing))
            writer = await self.write(question, route, ledger, sources, analysis.get("outline") or [],
                                      analysis.get("open_questions") or [])
            check = writer.get("handoff_check") or {"ok": True, "missing": []}
            if not check.get("ok", True) and w_returns < MAX_RETURNS:
                w_returns += 1
                await self.validation("analyst_to_writer", "analyst", "writer", attempt, missing, check, "rejected", "retry")
                await self.emit({"type": "handoff_return", "from": "writer", "to": "analyst",
                                 "missing": check.get("missing", [])})
                await self.status("analyst", "returned", "Writer chce doplnit")
                analysis = await self.analyse(question, route, research, feedback=check.get("missing", []),
                                              previous=analysis)
                attempt += 1
                continue
            if not check.get("ok", True):
                self.partial_reasons.append("Writer still missing: " + "; ".join(check.get("missing", [])))
            accepted = "accepted" if not missing and check.get("ok", True) else "partial"
            await self.validation("analyst_to_writer", "analyst", "writer", attempt, missing, check, accepted,
                                  "continue" if accepted == "accepted" else "continue_partial",
                                  supported_facts=len(supported), ledger_size=len(ledger))
            break

        # --- gate 3: final audit (contract + evidence + mechanical citation check)
        markdown = writer.get("answer_markdown", "")
        if not markdown.strip():
            raise RuntimeError("Writer returned an empty answer")
        w_missing = C.check_writer(writer)
        ev = E.check_writer_evidence(writer, ledger, markdown)
        quality = C.quality_report(markdown, sources)
        problems = list(w_missing)
        if not ev["ok"]:
            problems.append("evidence check failed: " + json.dumps(
                {k: ev[k] for k in ("unknown_fact_ids", "unsupported_facts_used", "citations_not_backed_by_used_facts")}))
        if not quality["passed"]:
            problems.append("output check failed: " + "; ".join(c["name"] for c in quality["checks"] if not c["ok"]))
        await self.validation("final_audit", "writer", "user", 1, problems, None,
                              "accepted" if not problems else "partial", "finish", evidence=ev)
        reasons = self.partial_reasons + problems
        return await self.finish(COMPLETED if not reasons else PARTIAL, reasons, markdown, sources, ledger,
                                 quality, ev, route)

    async def insufficient(self, route: dict, research: dict, reasons: list[str],
                           sources: list[dict] | None = None, ledger: list[dict] | None = None) -> dict:
        """Honest stop: say what was found and what is missing, invent nothing."""
        cs = str(route.get("language", "")).lower().startswith("cs")
        gaps = research.get("gaps") or []
        found = [f for f in research.get("findings", []) if f.get("source_url")]
        lines = ["## " + ("Nedostatek doložitelných zdrojů" if cs else "Not enough verifiable evidence"), "",
                 ("Na tuto otázku jsem nenašel dost zdrojů, abych mohl odpovědět bez domýšlení."
                  if cs else "I could not find enough sources to answer without guessing."), ""]
        if found:
            lines += ["### " + ("Co se našlo" if cs else "What was found"), ""]
            lines += [f"- {f.get('claim')} ({f.get('source_url')})" for f in found[:5]] + [""]
        lines += ["### " + ("Co chybí" if cs else "What is missing"), ""]
        lines += [f"- {g}" for g in (gaps or reasons)] + [""]
        lines += ["### " + ("Další krok" if cs else "Next step"), "",
                  "- " + ("Zpřesnit otázku (období, země, zdroj dat) nebo hledat v oborových databázích."
                          if cs else "Narrow the question (period, country, data source) or search domain databases.")]
        return await self.finish(INSUFFICIENT, reasons, "\n".join(lines), sources or [], ledger or [],
                                 None, None, route)

    async def finish(self, status: str, reasons: list[str], markdown: str, sources: list[dict], ledger: list[dict],
                     quality: dict | None, ev: dict | None, route: dict | None = None) -> dict:
        result = {"status": status, "status_reasons": reasons, "answer_markdown": markdown, "sources": sources,
                  "ledger": ledger, "quality": quality, "evidence": ev,
                  "complexity": (route or {}).get("complexity"), "usage": self.usage,
                  "cost_usd": round(sum(u["cost_usd"] for u in self.usage.values()), 4),
                  "cost_is_estimate": True, "duration_s": round(time.monotonic() - self.t0, 1)}
        await self.emit({"type": "final", **result})
        return result
