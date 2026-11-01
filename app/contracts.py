"""Handoff contracts: what each agent must deliver before the next one starts.

These checks run in code (deterministic). On top of them the receiving agent
does its own check (`handoff_check` in its output). If either fails, the
orchestrator sends the work back once with the list of what is missing.
"""
from __future__ import annotations

import re

COMPLEXITY = {"simple", "moderate", "complex"}
# Honest verdicts: "we have no evidence for this" needs no finding behind it.
NO_EVIDENCE_NEEDED = {"UNSUPPORTED", "NOT_CHECKED"}
# Sections without sourced claims: gaps and advice have nothing to cite (plain-writing skill).
UNCITED_SECTIONS = re.compile(r"(?i)#+\s*(co nevíme|open questions|what we don.t know|co dál|další krok|next steps|what next)")
# A number that states something. Years alone do not, and neither do digits inside names (CO2, R32).
FIGURE = re.compile(r"(?<![\w.,])\d+(?:[.,\u00a0 ]\d+)*")
YEAR = re.compile(r"(19|20)\d\d")
# Blocks are paragraphs, list items and table rows; a dot after these words does not end a sentence.
BLOCK = re.compile(r"\n\s*\n|\n(?=\s*(?:[-*+|]|\d+\.)\s)")
ABBREVIATION = re.compile(r"(?i)\b(mil|mld|tis|např|tj|tzv|resp|mj|č|vs|e\.g|i\.e)\.(?=\s)")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„\"*])")


def check_router(r: dict) -> list[str]:
    missing = []
    if r.get("complexity") not in COMPLEXITY:
        missing.append("complexity must be simple|moderate|complex")
    sq = r.get("subquestions")
    if not isinstance(sq, list) or not 1 <= len(sq) <= 6 or not all(isinstance(s, str) and s.strip() for s in sq):
        missing.append("1-6 subquestions")
    return missing


def usable_subquestions(r: dict) -> list[str]:
    """What survives of the Router's list; the caller falls back to the question itself."""
    sq = r.get("subquestions")
    return [s for s in sq if isinstance(s, str) and s.strip()][:6] if isinstance(sq, list) else []


def check_research(r: dict) -> list[str]:
    missing = []
    findings = r.get("findings") or []
    if len(findings) < 3:
        missing.append(f"at least 3 findings (got {len(findings)})")
    no_src = [i for i, f in enumerate(findings, 1) if not f.get("source_url")]
    no_quote = [f.get("id", i) for i, f in enumerate(findings, 1) if not (f.get("quote") or "").strip()]
    if no_quote:
        missing.append(f"findings without a verbatim quote: {no_quote}")
    if no_src:
        missing.append(f"findings without source_url: {no_src}")
    urls = {f.get("source_url") for f in findings if f.get("source_url")}
    if len(urls) < 2:
        missing.append(f"at least 2 distinct sources (got {len(urls)})")
    return missing


def check_analysis(a: dict, finding_ids: set[str] | None = None) -> list[str]:
    missing = []
    facts = a.get("verified_facts") or []
    unlinked = [f.get("id") for f in facts if not f.get("based_on")
                and str(f.get("verification_status", "")).upper() not in NO_EVIDENCE_NEEDED]
    if unlinked:
        missing.append(f"facts without based_on finding ids: {unlinked}")
    if finding_ids is not None:
        bad = sorted({r for f in facts for r in (f.get("based_on") or []) if r not in finding_ids})
        if bad:
            missing.append(f"based_on refers to unknown findings: {bad}")
    if not a.get("source_scores"):
        missing.append("source_scores from the source-evaluation skill")
    if not a.get("outline"):
        missing.append("outline for the writer")
    return missing


def check_ledger(supported: list[dict]) -> list[str]:
    """Counted on the ledger, after code re-checked the quotes, not on what the Analyst declared."""
    if len(supported) < 2:
        return [f"at least 2 SUPPORTED/PARTIALLY_SUPPORTED facts with a quote found in the page (got {len(supported)})"]
    return []


def check_writer(w: dict) -> list[str]:
    missing = []
    if not w.get("used_facts"):
        missing.append("used_facts (ids of the ledger facts the answer relies on)")
    md = w.get("answer_markdown") or ""
    if len(md) < 200:
        missing.append("answer_markdown too short")
    if not re.search(r"\[\d+\]", md):
        missing.append("no [n] citations in the answer")
    return missing


def uncited_paragraphs(markdown: str) -> int:
    """Longer paragraphs without a citation, outside the sections that have nothing to cite."""
    parts = re.split(r"(?m)^(#+[ \t].*)$", markdown)
    count = 0
    for heading, body in zip([""] + parts[1::2], parts[0::2]):
        if not UNCITED_SECTIONS.match(heading):
            count += sum(1 for p in re.split(r"\n\s*\n", body) if len(p.strip()) > 120 and not re.search(r"\[\d+\]", p))
    return count


def uncited_figures(markdown: str) -> list[str]:
    """Sentences, list items and table rows stating a number with no citation of their own."""
    parts = re.split(r"(?m)^(#+[ \t].*)$", markdown)
    found = []
    for heading, body in zip([""] + parts[1::2], parts[0::2]):
        if UNCITED_SECTIONS.match(heading):
            continue
        for block in BLOCK.split(body):
            text = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", re.sub(r"https?://\S+", "", block))
            for sentence in SENTENCE_END.split(ABBREVIATION.sub(r"\1", text)):
                if not re.search(r"\[\d+\]", sentence) and any(not YEAR.fullmatch(n.strip()) for n in FIGURE.findall(sentence)):
                    found.append(sentence.strip())
    return found


def quality_report(markdown: str, sources: list[dict]) -> dict:
    """Final output check shown in the UI."""
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", markdown)})
    valid = set(range(1, len(sources) + 1))
    invalid = [n for n in cited if n not in valid]
    unused = [n for n in sorted(valid) if n not in cited]
    uncited = uncited_paragraphs(markdown)
    figures = uncited_figures(markdown)
    checks = [
        {"name": "Every citation points to a listed source (mechanical, not a fact check)", "ok": not invalid,
         "detail": f"invalid: {invalid}" if invalid else f"{len(cited)} sources cited"},
        {"name": "Longer paragraphs with claims carry a citation", "ok": uncited == 0,
         "detail": f"{uncited} uncited paragraph(s)" if uncited else "all cited"},
        {"name": "Every sentence with a figure carries its own citation (presence, not meaning)", "ok": not figures,
         "detail": " | ".join(s[:90] for s in figures[:3]) if figures else "all cited"},
        {"name": "Open questions section present", "ok": bool(re.search(r"(?im)^#+\s*(co nevíme|open questions|what we don.t know)", markdown)),
         "detail": ""},
    ]
    return {"cited": cited, "invalid_citations": invalid, "unused_sources": unused,
            "words": len(markdown.split()), "checks": checks,
            "passed": all(c["ok"] for c in checks)}
