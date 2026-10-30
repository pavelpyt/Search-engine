"""Load Skills (SKILL.md with YAML frontmatter) and attach them to agents.

A skill is a folder: skills/<name>/SKILL.md (+ optional scripts/). The
frontmatter carries `name` and `description`; the body is the instruction set
that gets appended to the agent's system prompt. Scripts are exposed to the
agent as client tools (see tools.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@dataclass
class Skill:
    name: str
    description: str
    version: str
    body: str
    path: Path

    def as_prompt(self) -> str:
        return (f"\n\n<skill name=\"{self.name}\" version=\"{self.version}\">\n"
                f"{self.body.strip()}\n</skill>")


def load_skill(name: str) -> Skill:
    path = SKILLS_DIR / name / "SKILL.md"
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path} has no frontmatter")
    _, front, body = raw.split("---", 2)
    meta = yaml.safe_load(front) or {}
    return Skill(name=meta["name"], description=meta["description"],
                 version=str(meta.get("version", "1")), body=body, path=path)


def list_skills() -> list[dict]:
    out = []
    for d in sorted(SKILLS_DIR.iterdir()):
        if (d / "SKILL.md").exists():
            s = load_skill(d.name)
            out.append({"name": s.name, "description": s.description, "version": s.version})
    return out
