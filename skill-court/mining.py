"""Find replayable tasks in the profile's session history.

A task is the user message that led the agent to call ``skill_view`` on a skill: the request the
skill was loaded to serve. History is read through the public, read-only ``SessionDB``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from .skills_index import skill_key

MIN_PROMPT_CHARS = 12
_PAGE = 200


@dataclass(frozen=True)
class MinedTask:
    skill: str
    prompt: str
    session_id: str


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)).strip()
    return ""


def _viewed_skills(tool_calls: Any) -> Iterable[str]:
    for call in tool_calls if isinstance(tool_calls, list) else ():
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or function.get("name") != "skill_view":
            continue
        arguments = function.get("arguments")
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            continue
        if isinstance(args, dict) and isinstance(args.get("name"), str) and not args.get("file_path"):
            yield skill_key(args["name"])


def tasks_in_session(messages: list[dict], wanted: frozenset[str] | None) -> dict[str, str]:
    """``{skill: prompt}`` for the first time each wanted skill was loaded in one session."""
    found: dict[str, str] = {}
    last_user = ""
    for message in messages:
        role = message.get("role")
        if role == "user":
            last_user = _text(message.get("content"))
        elif role == "assistant":
            for skill in _viewed_skills(message.get("tool_calls")):
                if (wanted is None or skill in wanted) and skill not in found and len(last_user) >= MIN_PROMPT_CHARS:
                    found[skill] = last_user
    return found


def mine(wanted: Iterable[str] | None, per_skill: int) -> dict[str, list[MinedTask]]:
    """Newest-first distinct prompts per skill, at most ``per_skill`` each."""
    from hermes_state import SessionDB

    wanted_set = frozenset(wanted) if wanted is not None else None
    found: dict[str, list[MinedTask]] = {}
    seen: set[tuple[str, str]] = set()
    db = SessionDB(read_only=True)
    try:
        offset = 0
        while True:
            sessions = db.list_sessions_rich(
                limit=_PAGE, offset=offset, include_children=True, project_compression_tips=False,
                include_hidden=True, include_subagents=True, order_by_last_active=False)
            if not sessions:
                break
            offset += len(sessions)
            for session in sessions:
                session_id = session.get("id")
                if not session_id:
                    continue
                for skill, prompt in tasks_in_session(db.get_messages(session_id), wanted_set).items():
                    bucket = found.setdefault(skill, [])
                    if len(bucket) < per_skill and (skill, prompt) not in seen:
                        seen.add((skill, prompt))
                        bucket.append(MinedTask(skill=skill, prompt=prompt, session_id=session_id))
            if wanted_set is not None and all(len(found.get(s, ())) >= per_skill for s in wanted_set):
                break
    finally:
        db.close()
    return found
