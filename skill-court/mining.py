"""Find replayable tasks in the profile's session history.

A task is the user message that led the agent to call ``skill_view`` on a skill: the request the
skill was loaded to serve. For an automatic trial, the tasks also include the requests of the
session in which the agent wrote the skill, and that session's platform and sender decide who is
told the verdict. History is read through the public, read-only ``SessionDB``.
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


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)).strip()
    return ""


def _calls(tool_calls: Any) -> Iterable[tuple[str, Any]]:
    """``(tool name, decoded arguments)`` per well-formed call."""
    for call in tool_calls if isinstance(tool_calls, list) else ():
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        arguments = function.get("arguments")
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            continue
        yield function["name"], args


def viewed_skills(tool_calls: Any) -> Iterable[str]:
    for name, args in _calls(tool_calls):
        if name == "skill_view" and isinstance(args, dict) and isinstance(args.get("name"), str) and not args.get("file_path"):
            yield skill_key(args["name"])


def tasks_in_session(messages: list[dict], wanted: frozenset[str] | None) -> dict[str, str]:
    """``{skill: prompt}`` for the first time each wanted skill was loaded in one session."""
    found: dict[str, str] = {}
    last_user = ""
    for message in messages:
        role = message.get("role")
        if role == "user":
            last_user = text_of(message.get("content"))
        elif role == "assistant":
            for skill in viewed_skills(message.get("tool_calls")):
                if (wanted is None or skill in wanted) and skill not in found and len(last_user) >= MIN_PROMPT_CHARS:
                    found[skill] = last_user
    return found


def requests_in_session(messages: list[dict]) -> list[str]:
    """The distinct user requests of one session, in order, except the turns in which the agent wrote
    a skill (``skill_manage``): asking it to save a skill is not a task the skill serves."""
    turns: list[tuple[str, bool]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            turns.append((text_of(message.get("content")), False))
        elif role == "assistant" and turns and any(name == "skill_manage" for name, _ in _calls(message.get("tool_calls"))):
            turns[-1] = (turns[-1][0], True)
    requests: list[str] = []
    for prompt, wrote_skill in turns:
        if not wrote_skill and len(prompt) >= MIN_PROMPT_CHARS and prompt not in requests:
            requests.append(prompt)
    return requests


def session_requests(session_id: str) -> list[str]:
    """``requests_in_session`` for one session of the active profile."""
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    if not session_id or not (get_hermes_home() / "state.db").exists():
        return []
    db = SessionDB(read_only=True)
    try:
        return requests_in_session(db.get_messages(session_id))
    finally:
        db.close()


def session_origin(session_id: str) -> tuple[str | None, str | None, str | None]:
    """``(source, user_id, chat_id)`` Hermes stored for one session of the active profile: the
    platform or client it ran on, its sender and its chat. Nones when the session is not stored or a
    field is empty."""
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    if not session_id or not (get_hermes_home() / "state.db").exists():
        return None, None, None
    db = SessionDB(read_only=True)
    try:
        row = db.get_session(session_id)
    finally:
        db.close()
    if not row:
        return None, None, None
    return row.get("source") or None, row.get("user_id") or None, row.get("chat_id") or None


def mine(wanted: Iterable[str] | None, per_skill: int) -> dict[str, list[MinedTask]]:
    """Newest-first distinct prompts per skill, at most ``per_skill`` each."""
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    if not (get_hermes_home() / "state.db").exists():
        return {}  # a profile that has never run a session has no history to mine
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
