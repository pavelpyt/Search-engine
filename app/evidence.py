"""Evidence ledger: claims, the evidence behind them, and checks done in code.

Three separate things, never mixed:
- source quality  : prior score of the source (source-evaluation skill),
- claim support   : does a verbatim quote from the fetched page back the claim
                    (the Analyst grades it, code checks the quote really exists),
- corroboration   : are there really independent sources (different sites,
                    different wording), not two copies of one press release.

`quote_verified` only means the quote was found in the text we downloaded.
It does not prove the quote is true, and a missing quote in a truncated page
does not prove the quote is absent from the full page.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

SUPPORTING = {"SUPPORTED", "PARTIALLY_SUPPORTED"}
STATUSES = {"SUPPORTED", "PARTIALLY_SUPPORTED", "CONTRADICTED", "UNSUPPORTED", "NOT_CHECKED"}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[‘’‚‛′]", "'", text)
    text = re.sub(r"[“”„‟″]", '"', text)
    text = re.sub(r"[–—−]", "-", text)
    text = re.sub(r"[…]|\.\.\.", " ", text)
    text = re.sub(r"[^\w%$€.,:;'\"-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def verify_quote(doc: dict | None, quote: str) -> str:
    """exact | normalized | not_found | not_in_fetched_part | not_fetched | no_quote"""
    if not quote or len(quote.strip()) < 8:
        return "no_quote"
    if not doc or not doc.get("text"):
        return "not_fetched"
    if quote.strip() in doc["text"]:
        return "exact"
    nq = normalize(quote)
    if nq and nq in normalize(doc["text"]):
        return "normalized"
    return "not_in_fetched_part" if doc.get("truncated") else "not_found"


def site_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "ac", "gov", "org"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def annotate_findings(research: dict, docs: dict) -> list[dict]:
    """Give every finding a stable id and check its quote against the fetched page."""
    out = []
    for i, f in enumerate(research.get("findings") or [], 1):
        f["id"] = f.get("id") or f"f{i}"
        f["quote_check"] = verify_quote(docs.get(f.get("source_url", "")), f.get("quote", ""))
        f["quote_verified"] = f["quote_check"] in {"exact", "normalized"}
        out.append(f)
    research["findings"] = out
    return out


def number_sources(findings: list[dict], extra_urls: list[str], scores: dict) -> list[dict]:
    seen, out = {}, []
    for f in findings:
        url = f.get("source_url")
        if url and url not in seen:
            seen[url] = len(out) + 1
            sc = scores.get(url, {})
            out.append({"n": seen[url], "id": f"src{seen[url]}", "url": url, "title": f.get("source_title") or url,
                        "tier": sc.get("tier"), "score": sc.get("score")})
    for url in extra_urls:
        if url and url not in seen and str(url).startswith("http"):
            seen[url] = len(out) + 1
            sc = scores.get(url, {})
            out.append({"n": seen[url], "id": f"src{seen[url]}", "url": url, "title": url,
                        "tier": sc.get("tier"), "score": sc.get("score")})
    return out


def build_ledger(findings: list[dict], analysis: dict, sources: list[dict]) -> list[dict]:
    """Join the Analyst's graded facts with the findings (quotes) and numbered sources."""
    by_id = {f["id"]: f for f in findings}
    n_of = {s["url"]: s["n"] for s in sources}
    ledger = []
    for i, fact in enumerate(analysis.get("verified_facts") or [], 1):
        fid = fact.get("id") or f"F{i}"
        evidence = []
        for ref in fact.get("based_on") or []:
            f = by_id.get(ref)
            if not f:
                continue
            evidence.append({"finding_id": ref, "source_n": n_of.get(f.get("source_url")),
                             "url": f.get("source_url"), "quote": f.get("quote", ""),
                             "quote_check": f.get("quote_check"), "quote_verified": f.get("quote_verified", False)})
        status = str(fact.get("verification_status", "NOT_CHECKED")).upper()
        if status not in STATUSES:
            status = "NOT_CHECKED"
        verified = [e for e in evidence if e["quote_verified"]]
        sites = {site_of(e["url"]) for e in verified if e["url"]}
        quotes = {normalize(e["quote"]) for e in verified}
        independent = len(sites) >= 2 and len(quotes) >= 2
        limitations = []
        if not evidence:
            limitations.append("not linked to any finding")
        if evidence and not verified:
            limitations.append("quote not found in the fetched text")
        if len(sites) < 2:
            limitations.append("single site")
        confidence = str(fact.get("confidence", "low")).lower()
        if confidence == "high" and not (verified and (independent or fact.get("primary"))):
            confidence = "medium"   # high needs a verified quote and independence (or a primary source)
        if not verified and confidence != "low":
            confidence = "low"
        if status in SUPPORTING and not evidence:
            status = "NOT_CHECKED"
        ledger.append({"fact_id": fid, "statement": fact.get("fact", ""), "verification_status": status,
                       "confidence": confidence, "evidence": evidence,
                       "source_ns": sorted({e["source_n"] for e in evidence if e["source_n"]}),
                       "quote_verified": bool(verified), "independent_corroboration": independent,
                       "limitations": limitations, "analyst_note": fact.get("note", "")})
    return ledger


def check_writer_evidence(writer: dict, ledger: list[dict], markdown: str) -> dict:
    """Do the citations in the answer belong to the facts the Writer says it used?"""
    facts = {f["fact_id"]: f for f in ledger}
    used = [u for u in (writer.get("used_facts") or []) if isinstance(u, str)]
    unknown = [u for u in used if u not in facts]
    used_ok = [facts[u] for u in used if u in facts]
    unsupported_used = [f["fact_id"] for f in used_ok if f["verification_status"] not in SUPPORTING]
    allowed_ns = {n for f in used_ok for n in f["source_ns"]}
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", markdown)}
    not_backed = sorted(cited - allowed_ns)
    supported = [f for f in used_ok if f["verification_status"] in SUPPORTING and f["quote_verified"]]
    return {
        "used_facts": used, "unknown_fact_ids": unknown, "unsupported_facts_used": unsupported_used,
        "citations_not_backed_by_used_facts": not_backed,
        "evidence_support": {"supported": len(supported), "of": len(used_ok)},
        "unsupported_in_ledger": sum(1 for f in ledger if f["verification_status"] not in SUPPORTING),
        "independent_corroboration": sum(1 for f in used_ok if f["independent_corroboration"]),
        "ok": bool(used_ok) and not unknown and not unsupported_used and not not_backed,
    }
