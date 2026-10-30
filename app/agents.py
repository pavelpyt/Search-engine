"""Agent definitions: role, system prompt file, skills, tools, model tier."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import tools as T
from .skills import Skill, load_skill

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

MODELS = {
    "fast": os.getenv("MODEL_FAST", "claude-haiku-5-5"),
    "balanced": os.getenv("MODEL_BALANCED", "claude-sonnet-5-5"),
    "strong": os.getenv("MODEL_STRONG", "claude-opus-5-5"),
}

# USD per million tokens (input, output, cache read). Estimates for the cost shown in
# the UI; override with PRICE_<TIER>="in,out,read" if your contract differs.
def _price(tier: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
    raw = os.getenv(f"PRICE_{tier.upper()}")
    price = tuple(float(x) for x in raw.split(",")) if raw else default
    return price if len(price) == 3 else (*price, price[0] * 0.1)


PRICES = {
    MODELS["fast"]: _price("fast", (0.10, 0.50, 0.01)),
    MODELS["balanced"]: _price("balanced", (2.0, 10.0, 0.10)),
    MODELS["strong"]: _price("strong", (4.0, 20.0, 0.20)),
}
CACHE_WRITE_FACTOR = 1.25  # 5-minute cache
# Models that charge more for one long prompt: model -> (prompt tokens, price factor).
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 5)}
WEB_SEARCH_USD = float(os.getenv("PRICE_WEB_SEARCH", "0.01"))  # per search


def estimate_cost(model: str, usage: dict) -> float:
    """Cost of ONE API response from its `usage`; long-prompt pricing is decided per request."""
    p_in, p_out, p_read = PRICES.get(model, (2.0, 10.0, 0.10))
    fresh = usage.get("input_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    tokens = fresh * p_in + read * p_read + written * p_in * CACHE_WRITE_FACTOR + (usage.get("output_tokens") or 0) * p_out
    limit, factor = LONG_PROMPT.get(model, (0, 1))
    if limit and fresh + read + written > limit:
        tokens *= factor
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    return tokens / 1e6 + searches * WEB_SEARCH_USD


# The router decides complexity; this table (in code, not in a prompt) turns it into
# a model per agent, a search budget and a limit on findings (None = as many as needed).
MODEL_PLAN = {
    "simple":   {"researcher": "fast",     "analyst": "fast",     "writer": "fast",     "searches": 2, "max_findings": 6},
    "moderate": {"researcher": "balanced", "analyst": "balanced", "writer": "balanced", "searches": 5, "max_findings": 12},
    "complex":  {"researcher": "balanced", "analyst": "strong",   "writer": "strong",   "searches": 8, "max_findings": None},
}


@dataclass
class AgentSpec:
    key: str
    label: str
    emoji: str
    role: str
    prompt_file: str
    skill_names: list[str] = field(default_factory=list)

    @property
    def skills(self) -> list[Skill]:
        return [load_skill(n) for n in self.skill_names]

    def system_prompt(self) -> str:
        base = (PROMPTS_DIR / self.prompt_file).read_text(encoding="utf-8")
        return f"AGENT: {self.key}\n\n{base}" + "".join(s.as_prompt() for s in self.skills)

    def tools(self, searches: int, impls: dict) -> tuple[list[dict], dict]:
        """Tool schemas for the API + this run's client-side implementations."""
        if self.key == "researcher":
            return [T.web_search_tool(searches), T.FETCH_PAGE], {"fetch_page": impls["fetch_page"]}
        if self.key == "analyst":
            return ([T.web_search_tool(max(1, searches // 2)), T.EVALUATE_SOURCE, T.FIND_IN_SOURCE],
                    {"evaluate_source": impls["evaluate_source"], "find_in_source": impls["find_in_source"]})
        return [], {}


AGENTS = {
    "router": AgentSpec("router", "Router", "🧭", "Určí náročnost, rozdělí otázku, vybere modely",
                        "router.md", ["question-decomposition"]),
    "researcher": AgentSpec("researcher", "Researcher", "🔎", "Hledá na webu, čte stránky, sbírá fakta se zdroji",
                            "researcher.md"),
    "analyst": AgentSpec("analyst", "Analyst", "🧪", "Hodnotí zdroje, ověřuje klíčová tvrzení, hledá souvislosti a mezery",
                         "analyst.md", ["source-evaluation"]),
    "writer": AgentSpec("writer", "Writer", "✍️", "Píše finální odpověď s citacemi",
                        "writer.md", ["plain-writing"]),
}
