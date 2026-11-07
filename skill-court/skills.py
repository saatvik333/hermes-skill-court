"""Read-only view of the profile's local skill library: names, authorship marker, content hash."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_hermes_home

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
# ``created_by`` values core writes into skills/.usage.json for skills the agent authored:
# "agent" (background review) and "learn" (written in the foreground at the user's request).
AGENT_AUTHORED = frozenset({"agent", "learn"})


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path  # directory holding SKILL.md
    created_by: str | None

    @property
    def agent_authored(self) -> bool:
        return self.created_by in AGENT_AUTHORED

    def content_hash(self) -> str:
        """SHA-256 over every file in the skill directory, so a verdict is tied to one version."""
        digest = hashlib.sha256()
        for file in sorted(p for p in self.path.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            digest.update(file.relative_to(self.path).as_posix().encode())
            digest.update(b"\0")
            digest.update(file.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()[:16]


def _skills_root() -> Path:
    return get_hermes_home() / "skills"


def _frontmatter_name(skill_md: Path) -> str | None:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    block = _FRONTMATTER_RE.match(text)
    found = _NAME_RE.search(block.group(1)) if block else None
    return found.group(1).strip("'\"") if found else None


def _usage() -> dict:
    try:
        data = json.loads((_skills_root() / ".usage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def library() -> dict[str, Skill]:
    """Every local skill by frontmatter name (archived and hidden directories excluded)."""
    root = _skills_root()
    usage = _usage()
    found: dict[str, Skill] = {}
    for skill_md in sorted(root.rglob("SKILL.md")):
        if any(part.startswith(".") for part in skill_md.relative_to(root).parts):
            continue
        name = _frontmatter_name(skill_md) or skill_md.parent.name
        record = usage.get(name)
        created_by = record.get("created_by") if isinstance(record, dict) else None
        found.setdefault(name, Skill(name=name, path=skill_md.parent, created_by=created_by))
    return found
