"""Remove quarantined skills from the ``<available_skills>`` index of an outgoing LLM request.

Pure functions over the provider request; no Hermes imports, so they test standalone.
The index shapes handled are the ones ``agent/prompt_builder.py`` renders:

    <available_skills>
      category:
        - name: description
      - single-skill: a one-skill category renders as a row at category depth
      other [names only]: a, b, c
    </available_skills>
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_BLOCK_RE = re.compile(r"(<available_skills>\n?)(.*?)(</available_skills>)", re.DOTALL)
_ROW_RE = re.compile(r"^(\s*)- ([^:\s]+)(?::.*)?$")
_NAMES_ONLY_RE = re.compile(r"^(\s*\S.*? \[names only\]: )(.+)$")
_HEADER_RE = re.compile(r"^(\s*)[^\s-][^:]*:$")


def skill_key(name: str) -> str:
    """Bare skill name for a ``skill_view`` argument or index row (``category/name`` → ``name``)."""
    return name.strip().rsplit("/", 1)[-1]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_body(body: str, hidden: frozenset[str], removed: set[str]) -> str:
    kept: list[str] = []
    for line in body.split("\n"):
        row = _ROW_RE.match(line)
        if row and skill_key(row.group(2)) in hidden:
            removed.add(skill_key(row.group(2)))
            continue
        names_only = _NAMES_ONLY_RE.match(line)
        if names_only:
            names = [n.strip() for n in names_only.group(2).split(",") if n.strip()]
            visible = [n for n in names if skill_key(n) not in hidden]
            removed.update(skill_key(n) for n in names if skill_key(n) in hidden)
            if not visible:
                continue
            line = names_only.group(1) + ", ".join(visible)
        kept.append(line)
    # A category header whose rows were all removed would advertise an empty category.
    out: list[str] = []
    for i, line in enumerate(kept):
        if _HEADER_RE.match(line) and not _ROW_RE.match(line):
            nxt = kept[i + 1] if i + 1 < len(kept) else ""
            if not (_ROW_RE.match(nxt) and _indent(nxt) > _indent(line)):
                continue
        out.append(line)
    return "\n".join(out)


def strip_text(text: str, hidden: frozenset[str]) -> tuple[str | None, set[str]]:
    """``(new_text, removed)``; ``new_text`` is None when nothing was removed."""
    removed: set[str] = set()
    match = _BLOCK_RE.search(text)
    if not match or not hidden:
        return None, removed
    body = _strip_body(match.group(2), hidden, removed)
    if not removed:
        return None, removed
    return text[: match.start(2)] + body + text[match.end(2) :], removed


def _strip_blocks(blocks: Iterable[Any], hidden: frozenset[str], removed: set[str]) -> list[Any] | None:
    """Content-block list (``[{"type": "text", "text": ...}, ...]``) with the index rewritten, or None."""
    out: list[Any] = []
    changed = False
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            new, gone = strip_text(block["text"], hidden)
            if new is not None:
                block = {**block, "text": new}
                removed |= gone
                changed = True
        out.append(block)
    return out if changed else None


def _strip_content(content: Any, hidden: frozenset[str], removed: set[str]) -> Any | None:
    if isinstance(content, str):
        new, gone = strip_text(content, hidden)
        removed |= gone
        return new
    if isinstance(content, list):
        return _strip_blocks(content, hidden, removed)
    return None


def strip_request(request: dict[str, Any], hidden: frozenset[str]) -> tuple[dict[str, Any] | None, set[str]]:
    """Rewrite every place a provider request carries the system prompt.

    Covers ``instructions`` (Responses API), ``system`` (Anthropic, string or blocks) and a leading
    ``system``/``developer`` message (Chat Completions). Never mutates ``request``.
    """
    removed: set[str] = set()
    if not hidden:
        return None, removed
    out = dict(request)
    changed = False
    for key in ("instructions", "system"):
        new = _strip_content(out.get(key), hidden, removed)
        if new is not None:
            out[key] = new
            changed = True
    messages = out.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict) and first.get("role") in {"system", "developer"}:
            new = _strip_content(first.get("content"), hidden, removed)
            if new is not None:
                out["messages"] = [{**first, "content": new}, *messages[1:]]
                changed = True
    return (out if changed else None), removed
