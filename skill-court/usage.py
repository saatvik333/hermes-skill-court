"""Usage evidence from session history, with no model call and no spend.

Per skill and model, over the ``RECENT_SESSIONS`` most recently started sessions: the sessions and
turns in which the agent loaded the skill (a ``skill_view`` that did not fail), how its tool calls
went from the turn it was first loaded in onwards compared with the same sessions' earlier turns, and
how often the agent changed the skill (a ``skill_manage`` that neither failed nor only staged its
write) after loading it in a session. A session counts under the model Hermes recorded for it, and
its turns include those an in-place compaction archived.

This is evidence, never a verdict: a skill loads on the tasks it is for, so the turns after it
loads are not comparable with the rest. History is read through the public, read-only ``SessionDB``
(``mining.sessions``).
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, fields
from typing import Any, Iterable

from .mining import calls, sessions, text_of, viewed_skill
from .skills_index import skill_key

# ``skill_manage`` actions that change a skill's files (``tools/skill_manager_tool.py``).
CHANGE_ACTIONS = frozenset({"patch", "edit", "write_file", "remove_file"})
UNKNOWN_MODEL = "an unrecorded model"
# The report and the Desktop page read this many of the newest sessions, so they stay quick on a
# long history.
RECENT_SESSIONS = 500
# How every place that shows it labels this evidence.
LABEL = (f"evidence from the {RECENT_SESSIONS} most recent sessions, not a verdict: a skill loads on the tasks it "
         "is for, so the turns after it loads are not comparable with the others")


@dataclass
class Usage:
    sessions: int = 0
    loads: int = 0  # turns in which the agent loaded the skill
    calls_after: int = 0  # tool results from the turn it was first loaded in onwards
    failed_after: int = 0
    calls_before: int = 0  # tool results in the same sessions' earlier turns
    failed_before: int = 0
    changes: int = 0  # skill_manage changes to it after it was loaded in the session

    def add(self, other: Usage) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))


def _result(content: Any) -> dict | None:
    """A stored tool result's JSON object, if it is one."""
    text = text_of(content)
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def tool_failed(content: Any) -> bool:
    """Whether a stored tool result failed by Hermes' conventions: the executor's ``Error executing
    tool`` text, or a JSON object with an ``error``, a false ``success`` or ``ok``, or a non-zero
    ``exit_code``."""
    if text_of(content).startswith("Error executing tool '"):
        return True
    data = _result(content)
    if data is None:
        return False
    exit_code = data.get("exit_code")
    return (bool(data.get("error")) or data.get("success") is False or data.get("ok") is False
            or (type(exit_code) is int and exit_code != 0))


def tool_staged(content: Any) -> bool:
    """Whether a stored ``skill_manage`` result only staged the write for approval
    (``skills.write_approval``), so nothing changed yet."""
    data = _result(content)
    return data is not None and data.get("staged") is True


def changed_skills(tool_calls: Any) -> Iterable[tuple[str | None, str]]:
    """``(call id, skill)`` for each skill a ``skill_manage`` call changes, once per operation, in its
    operations-array form or the legacy flat form."""
    for call_id, tool, args in calls(tool_calls):
        if tool != "skill_manage" or not isinstance(args, dict):
            continue
        operations = args["operations"] if isinstance(args.get("operations"), list) else [args]
        for op in operations:
            name = (op.get("name") or args.get("name")) if isinstance(op, dict) else None
            if isinstance(name, str) and op.get("action") in CHANGE_ACTIONS:
                yield call_id, skill_key(name)


def in_session(messages: list[dict]) -> dict[str, Usage]:
    """``{skill: Usage}`` for the skills one session loaded."""
    failed = {i: tool_failed(m.get("content")) for i, m in enumerate(messages) if m.get("role") == "tool"}
    failed_calls = {messages[i].get("tool_call_id") for i, bad in failed.items() if bad} - {None, ""}
    # A change that failed or only waits for approval left the skill as it was.
    unchanged_calls = failed_calls | ({m.get("tool_call_id") for m in messages
                                       if m.get("role") == "tool" and tool_staged(m.get("content"))} - {None, ""})
    turn = -1
    load_turns: dict[str, set[int]] = {}
    changes: dict[str, int] = {}
    results: list[tuple[int, bool]] = []  # (turn, failed) per tool result
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "user":
            turn += 1
        elif role == "assistant":
            for call_id, tool, args in calls(message.get("tool_calls")):
                skill = viewed_skill(tool, args)
                if skill is not None and call_id not in failed_calls:
                    load_turns.setdefault(skill, set()).add(turn)
            for call_id, skill in changed_skills(message.get("tool_calls")):
                if skill in load_turns and call_id not in unchanged_calls:
                    changes[skill] = changes.get(skill, 0) + 1
        elif role == "tool":
            results.append((turn, failed[index]))
    found: dict[str, Usage] = {}
    for skill, turns in load_turns.items():
        first = min(turns)
        usage = found[skill] = Usage(sessions=1, loads=len(turns), changes=changes.get(skill, 0))
        for result_turn, bad in results:
            if result_turn >= first:
                usage.calls_after += 1
                usage.failed_after += bad
            else:
                usage.calls_before += 1
                usage.failed_before += bad
    return found


def aggregate(history: Iterable[tuple[str | None, list[dict]]],
              wanted: frozenset[str] | None = None) -> dict[str, dict[str, Usage]]:
    """``{skill: {model: Usage}}`` over ``(model, messages)`` per session."""
    found: dict[str, dict[str, Usage]] = {}
    for model, messages in history:
        for skill, usage in in_session(messages).items():
            if wanted is None or skill in wanted:
                found.setdefault(skill, {}).setdefault(model or UNKNOWN_MODEL, Usage()).add(usage)
    return found


def scan(wanted: frozenset[str] | None = None) -> dict[str, dict[str, Usage]]:
    """``aggregate`` over the active profile's ``RECENT_SESSIONS`` most recent sessions, compacted turns
    included."""
    with closing(sessions(RECENT_SESSIONS, include_compacted=True)) as history:
        return aggregate(((session.get("model"), messages) for session, messages in history), wanted)


def _times(n: int, what: str) -> str:
    return f"{n} {what}{'' if n == 1 else 's'}"


def describe(by_model: dict[str, Usage]) -> str:
    """One line, e.g. ``loaded in 9 turns of 7 sessions (5 on m1, 2 on m2); 3 of 40 tool calls failed
    from the turn it loaded in on, 1 of 25 before it; changed 2 times after it loaded``."""
    total = Usage()
    for usage in by_model.values():
        total.add(usage)
    split = ", ".join(f"{u.sessions} on {model}" for model, u in sorted(by_model.items(), key=lambda kv: (-kv[1].sessions, kv[0])))
    text = f"loaded in {_times(total.loads, 'turn')} of {_times(total.sessions, 'session')} ({split})"
    if total.calls_after or total.calls_before:
        text += (f"; {total.failed_after} of {_times(total.calls_after, 'tool call')} failed from the turn it loaded in "
                 f"on, {total.failed_before} of {total.calls_before} before it")
    if total.changes:
        text += f"; changed {_times(total.changes, 'time')} after it loaded"
    return text
