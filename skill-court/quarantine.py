"""Live enforcement of quarantine verdicts.

The skills index is not the only way a model finds a skill, so three points enforce a quarantine:
the ``llm_request`` middleware removes the skill's row from ``<available_skills>``, the
``transform_tool_result`` hook removes it from ``skills_list`` results, and the ``pre_tool_call``
hook blocks a direct ``skill_view`` of it. Skill files are never touched.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from . import store
from .skills_index import filter_skills_list, skill_key, strip_request

_SESSION_MODEL_CAP = 1024
# Tool hooks carry no model, so remember the model each session last sent a request with.
_session_model: OrderedDict[str, str] = OrderedDict()


def _remember_model(session_id: str, model: str) -> None:
    if not session_id or not model:
        return
    _session_model[session_id] = model
    _session_model.move_to_end(session_id)
    while len(_session_model) > _SESSION_MODEL_CAP:
        _session_model.popitem(last=False)


def on_llm_request(request: Any = None, session_id: str = "", model: str = "", **_: Any) -> dict | None:
    _remember_model(session_id, model)
    if not isinstance(request, dict):
        return None
    hidden = store.hidden_for(model)
    if not hidden:
        return None
    new_request, removed = strip_request(request, frozenset(hidden))
    if new_request is None:
        return None
    return {
        "request": new_request,
        "source": store.PLUGIN,
        "reason": "quarantined for this model: " + ", ".join(sorted(removed)),
    }


def on_transform_tool_result(tool_name: str = "", result: Any = None, session_id: str = "", **_: Any) -> str | None:
    if tool_name != "skills_list" or not isinstance(result, str):
        return None
    hidden = store.hidden_for(_session_model.get(session_id, ""))
    return filter_skills_list(result, frozenset(hidden)) if hidden else None


def on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> dict | None:
    if tool_name != "skill_view" or not isinstance(args, dict) or not isinstance(args.get("name"), str):
        return None
    model = _session_model.get(session_id, "")
    skill = skill_key(args["name"])
    reason = store.hidden_for(model).get(skill)
    if reason is None:
        return None
    return {
        "action": "block",
        "message": (
            f"Skill '{skill}' is quarantined by Skill Court for model {model or 'unknown'}: {reason}. "
            "Do not load it; continue the task without it."
        ),
    }
