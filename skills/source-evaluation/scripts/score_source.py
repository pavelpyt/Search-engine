"""Deterministic first-pass score for a web source.

Part of the `source-evaluation` skill. The analysis agent calls it through the
`evaluate_source` tool, then adjusts the result with its own judgement as the
SKILL.md rubric describes. Keeping the base score in code makes it repeatable:
the same URL always starts from the same number.

Usage: python score_source.py <url> [page_age]
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from urllib.parse import urlparse

# (source_type, base score, matcher). First match wins.
RULES: list[tuple[str, int, re.Pattern]] = [
    ("official", 90, re.compile(r"(\.gov(\.[a-z]{2})?$|\.gov\.cz$|europa\.eu$|who\.int$|un\.org$|oecd\.org$|czso\.cz$|cnb\.cz$|mfcr\.cz$|justice\.cz$)")),
    ("research", 85, re.compile(r"(\.edu$|\.ac\.[a-z]{2}$|arxiv\.org$|doi\.org$|nature\.com$|sciencedirect\.com$|ncbi\.nlm\.nih\.gov$|pubmed|springer\.com$|acm\.org$|ieee\.org$)")),
    ("reputable_media", 75, re.compile(r"(reuters\.com$|apnews\.com$|bbc\.(co\.uk|com)$|ft\.com$|economist\.com$|nytimes\.com$|wsj\.com$|bloomberg\.com$|theguardian\.com$|ct24\.ceskatelevize\.cz$|irozhlas\.cz$|seznamzpravy\.cz$|hn\.cz$|e15\.cz$|forbes\.(com|cz)$|denikn\.cz$)")),
    ("reference", 65, re.compile(r"(wikipedia\.org$|britannica\.com$)")),
    ("vendor_docs", 60, re.compile(r"(^docs\.|^developer\.|^platform\.|github\.com$|readthedocs\.io$)")),
    ("blog", 45, re.compile(r"(medium\.com$|substack\.com$|blogspot\.|wordpress\.com$|^blog\.)")),
    ("forum", 35, re.compile(r"(reddit\.com$|quora\.com$|stackexchange\.com$|stackoverflow\.com$|discord\.)")),
    ("social", 30, re.compile(r"(x\.com$|twitter\.com$|facebook\.com$|tiktok\.com$|instagram\.com$|linkedin\.com$|youtube\.com$)")),
]

TIERS = [(80, "A"), (65, "B"), (45, "C"), (0, "D")]


def _year(page_age: str | None) -> int | None:
    if not page_age:
        return None
    m = re.search(r"(19|20)\d{2}", page_age)
    return int(m.group(0)) if m else None


def score(url: str, page_age: str | None = None) -> dict:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    signals: list[str] = []

    source_type, base = "unknown", 50
    for name, points, pattern in RULES:
        if pattern.search(host):
            source_type, base = name, points
            break
    signals.append(f"type={source_type} (base {base})")

    s = base
    if parsed.scheme != "https":
        s -= 10
        signals.append("no https (-10)")

    year = _year(page_age)
    if year:
        age = date.today().year - year
        if age <= 1:
            s += 5
            signals.append(f"recent ({year}, +5)")
        elif age >= 4:
            s -= 10
            signals.append(f"old ({year}, -10)")
    else:
        signals.append("publication date unknown")

    s = max(0, min(100, s))
    tier = next(t for limit, t in TIERS if s >= limit)
    return {"url": url, "host": host, "source_type": source_type,
            "score": s, "tier": tier, "signals": signals}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    print(json.dumps(score(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None), indent=2))
