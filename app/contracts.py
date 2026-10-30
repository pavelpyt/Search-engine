"""Handoff contracts: what each agent must deliver before the next one starts.

These checks run in code (deterministic). On top of them the receiving agent
does its own check (`handoff_check` in its output). If either fails, the
orchestrator sends the work back once with the list of what is missing.
"""
from __future__ import annotations

import re

COMPLEXITY = {"simple", "moderate", "complex"}


def check_router(r: dict) -> list[str]:
    missing = []
    if r.get("complexity") not in COMPLEXITY:
        missing.append("complexity must be simple|moderate|complex")
    sq = r.get("subquestions")
    if not isinstance(sq, list) or not 1 <= len(sq) <= 6:
        missing.append("1-6 subquestions")
    return missing


def check_research(r: dict) -> list[str]:
    missing = []
    findings = r.get("findings") or []
    if len(findings) < 3:
        missing.append(f"at least 3 findings (got {len(findings)})")
    no_src = [i for i, f in enumerate(findings, 1) if not f.get("source_url")]
    if no_src:
        missing.append(f"findings without source_url: {no_src}")
    urls = {f.get("source_url") for f in findings if f.get("source_url")}
    if len(urls) < 2:
        missing.append(f"at least 2 distinct sources (got {len(urls)})")
    return missing


def check_analysis(a: dict) -> list[str]:
    missing = []
    facts = a.get("verified_facts") or []
    if len(facts) < 2:
        missing.append(f"at least 2 verified_facts (got {len(facts)})")
    if any(not f.get("sources") for f in facts):
        missing.append("every verified fact needs sources")
    if not a.get("source_scores"):
        missing.append("source_scores from the source-evaluation skill")
    if not a.get("outline"):
        missing.append("outline for the writer")
    return missing


def check_writer(w: dict) -> list[str]:
    missing = []
    md = w.get("answer_markdown") or ""
    if len(md) < 200:
        missing.append("answer_markdown too short")
    if not re.search(r"\[\d+\]", md):
        missing.append("no [n] citations in the answer")
    return missing


def quality_report(markdown: str, sources: list[dict]) -> dict:
    """Final output check shown in the UI."""
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", markdown)})
    valid = set(range(1, len(sources) + 1))
    invalid = [n for n in cited if n not in valid]
    unused = [n for n in sorted(valid) if n not in cited]
    paragraphs = [p for p in re.split(r"\n\s*\n", markdown) if p.strip() and not p.strip().startswith("#")]
    uncited = sum(1 for p in paragraphs if len(p) > 120 and not re.search(r"\[\d+\]", p))
    checks = [
        {"name": "Every citation points to a real source", "ok": not invalid,
         "detail": f"invalid: {invalid}" if invalid else f"{len(cited)} sources cited"},
        {"name": "Longer paragraphs carry a citation", "ok": uncited == 0,
         "detail": f"{uncited} uncited paragraph(s)" if uncited else "all cited"},
        {"name": "Open questions section present", "ok": bool(re.search(r"(?im)^#+\s*(co nevíme|open questions|what we don.t know)", markdown)),
         "detail": ""},
    ]
    return {"cited": cited, "invalid_citations": invalid, "unused_sources": unused,
            "words": len(markdown.split()), "checks": checks,
            "passed": all(c["ok"] for c in checks)}
