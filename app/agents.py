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

# USD per million tokens (input, output). Estimates for the cost shown in the UI;
# override with PRICE_<TIER>="in,out" if your contract differs.
def _price(tier: str, default: tuple[float, float]) -> tuple[float, float]:
    raw = os.getenv(f"PRICE_{tier.upper()}")
    return tuple(float(x) for x in raw.split(",")) if raw else default


PRICES = {
    MODELS["fast"]: _price("fast", (1.0, 5.0)),
    MODELS["balanced"]: _price("balanced", (2.0, 10.0)),
    MODELS["strong"]: _price("strong", (4.0, 20.0)),
}
WEB_SEARCH_USD = float(os.getenv("PRICE_WEB_SEARCH", "0.01"))  # per search


def estimate_cost(model: str, input_tokens: int, output_tokens: int, web_searches: int = 0) -> float:
    p_in, p_out = PRICES.get(model, (2.0, 10.0))
    return round(input_tokens / 1e6 * p_in + output_tokens / 1e6 * p_out + web_searches * WEB_SEARCH_USD, 4)


# The router decides complexity; this table (in code, not in a prompt) turns it
# into a model per agent and a search budget.
MODEL_PLAN = {
    "simple":   {"researcher": "fast",     "analyst": "fast",     "writer": "fast",     "searches": 3},
    "moderate": {"researcher": "balanced", "analyst": "balanced", "writer": "balanced", "searches": 5},
    "complex":  {"researcher": "balanced", "analyst": "strong",   "writer": "strong",   "searches": 8},
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

    def tools(self, searches: int) -> tuple[list[dict], dict]:
        """Tool schemas for the API + client-side implementations."""
        if self.key == "researcher":
            return [T.web_search_tool(searches), T.FETCH_PAGE], {"fetch_page": T.fetch_page}
        if self.key == "analyst":
            return ([T.web_search_tool(max(2, searches // 2)), T.EVALUATE_SOURCE],
                    {"evaluate_source": T.evaluate_source})
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
