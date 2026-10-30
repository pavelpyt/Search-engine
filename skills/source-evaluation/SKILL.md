---
name: source-evaluation
description: Score web sources and grade the facts that rest on them, so a research answer only states with confidence what good sources support. Use when you receive findings with URLs and must decide what to trust.
version: "1.0"
---

# Source evaluation

You decide how far each fact can be trusted. You work in two passes: first score the
**sources**, then grade the **facts** that rest on them.

## Pass 1 – score every source

1. Call the `evaluate_source` tool for every distinct URL (pass `page_age` when you know it).
   It returns a deterministic base score 0–100, a tier and the signals it used:

   | Tier | Score | Typical source |
   |---|---|---|
   | A | 80–100 | official statistics, regulators, peer-reviewed research, primary documents |
   | B | 65–79 | reputable media, encyclopaedias, official vendor documentation |
   | C | 45–64 | blogs, company marketing, unknown sites |
   | D | 0–44 | forums, social media, anonymous or very old content |

2. Adjust the base score by at most ±15 with your own judgement, and say why in `reason`:
   - **+** the page is the primary source of the fact (the study, the law, the company's own report);
     it shows methodology; it is recent relative to the question.
   - **−** conflict of interest (a vendor rating its own product); no author or date; the page only
     repeats another source; the claim on the page is vaguer than the finding says.
3. Never move a source more than one tier with judgement alone.

## Pass 2 – grade every fact

For each fact you keep in `verified_facts`:

| Confidence | Rule |
|---|---|
| `high` | one A-tier primary source, **or** two independent sources at B or better that agree |
| `medium` | one B-tier source, or several C-tier sources that agree |
| `low` | only C/D sources, or sources that disagree |

Rules that always apply:
- **Numbers** (money, percentages, dates, counts) need `high` to be stated as fact. Otherwise the
  Writer must present them as "reported by …" or as an estimate.
- **Independence:** two articles quoting the same press release are one source.
- **Contradictions** are kept, not resolved silently: add them to `contradictions` with both positions.
- A fact resting only on D-tier sources is dropped or moved to `open_questions`.
- If a fact is important for the user's goal and is `low`, use `web_search` once to confirm it.

## Output fields this skill fills

- `source_scores`: `{url, score, tier, reason}` for every source.
- `verified_facts[].confidence` and `verified_facts[].note` (why that confidence).
- `contradictions`, `open_questions`.

## Example

Finding: "AI adoption in EU firms reached 13.5 % in 2024" from a blog (C) and Eurostat (A).
→ Eurostat is the primary source: confidence `high`, cite Eurostat first; the blog adds nothing
and can be left out of the facts.

## Files

- `scripts/score_source.py` – the base scoring used by the `evaluate_source` tool.
  Run it by hand: `python skills/source-evaluation/scripts/score_source.py https://www.oecd.org "March 2026"`.
