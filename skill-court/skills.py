"""Read-only view of the profile's skill library through Hermes' public skill-catalog helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# ``created_by`` values core records in the skills directory's ``.usage.json`` for skills the agent
# authored: "agent" (background review) and "learn" (written in the foreground at the user's request).
AGENT_AUTHORED = frozenset({"agent", "learn"})


@dataclass(frozen=True)
class Skill:
    name: str  # the name skill_view accepts
    path: Path  # directory holding SKILL.md
    created_by: str | None

    @property
    def agent_authored(self) -> bool:
        return self.created_by in AGENT_AUTHORED

    def content_hash(self) -> str:
        """Hash of the skill's files, the one Hermes' skill sync uses, so a verdict is tied to a version."""
        from tools.skills_sync import dir_hash

        return dir_hash(self.path)


def _usage(skills_dir: Path) -> dict:
    try:
        data = json.loads((skills_dir / ".usage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def library() -> dict[str, Skill]:
    """Every skill the profile can load (including disabled ones), by the name ``skill_view`` takes."""
    from tools.skills_tool import find_all_skills, locate_skill, skill_search_dirs

    roots, active_dir = skill_search_dirs()
    usage = _usage(active_dir)
    found: dict[str, Skill] = {}
    for entry in find_all_skills(skip_disabled=True):
        name = entry["name"]
        error, skill_dir, _skill_md = locate_skill(name, None, roots)
        if error or skill_dir is None:
            continue
        record = usage.get(name)
        created_by = record.get("created_by") if isinstance(record, dict) else None
        found[name] = Skill(name=name, path=Path(skill_dir), created_by=created_by)
    return found
