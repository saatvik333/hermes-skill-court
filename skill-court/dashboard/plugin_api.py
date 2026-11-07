"""Desktop backend: verdicts, quarantine and appeals, under /api/plugins/skill-court/."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from fastapi import APIRouter, HTTPException
from hermes_constants import get_hermes_home, profile_name_for_home
from pydantic import BaseModel


def _sibling(name: str) -> ModuleType:
    """Load one of the plugin's own modules from the folder above. Hermes imports this file on its
    own, outside the plugin package; ``store``, ``skills`` and ``procs`` have no relative imports.

    The module is registered under its own ``skill_court_dashboard_*`` name before it runs, as the
    standard importlib recipe does: its dataclasses look their module up in ``sys.modules``.
    """
    module_name = f"skill_court_dashboard_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = Path(__file__).resolve().parents[1] / f"{name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[module_name]
        raise
    return module


store = _sibling("store")
skills = _sibling("skills")
procs = _sibling("procs")

router = APIRouter()


class QuarantineBody(BaseModel):
    skill: str
    model: str
    reason: str = "quarantined from Hermes Desktop"


class SkillModelBody(BaseModel):
    skill: str
    model: str


def _appeals(library: dict) -> list[dict]:
    """The newest appeal per skill and model: its draft's diff, verdict, and whether it still
    applies to the skill as it is (``stale`` once the skill changed after the draft)."""
    rows = []
    for a in store.latest_appeals():
        current = library.get(a["skill"])
        rows.append({
            "id": a["id"], "skill": a["skill"], "model": a["model"], "verdict": a["verdict"], "reason": a["reason"],
            "diff": a["diff"], "created_at": a["created_at"], "applied": a["applied_at"] is not None,
            "stale": current is None or current.content_hash() not in (a["base_hash"], a["draft_hash"]),
            "path": None if current is None else str(current.path / "SKILL.md"),
        })
    return rows


def _verdicts() -> dict:
    library = skills.library()
    hidden = {(skill, model) for skill, model, _reason, _since in store.quarantine_entries()}
    rows = []
    for trial in store.latest_trials():
        current = library.get(trial["skill"])
        rows.append({
            # success rates, intervals, token change and ``sources`` ({check, judge}: pairs scored each way)
            **json.loads(trial["summary"] or "{}"),
            "skill": trial["skill"],
            "model": trial["model"],
            "verdict": trial["verdict"],
            "judged_at": trial["finished_at"],
            "trial_id": trial["id"],
            "auto": bool(trial["auto"]),
            "cause": trial["cause"],
            "cause_quote": trial["cause_quote"],
            "skill_changed": current is None or current.content_hash() != trial["skill_hash"],
            "quarantined": (trial["skill"], trial["model"]) in hidden or (trial["skill"], store.ANY_MODEL) in hidden,
        })
    quarantine = [{"skill": s, "model": m, "reason": r, "since": c} for s, m, r, c in store.quarantine_entries()]
    settings = store.auto_settings()
    auto = {"enabled": settings.enabled, "daily_budget_usd": settings.daily_budget_usd,
            "spent_today_usd": store.auto_spent_today(), "daily_judge_tokens": settings.daily_judge_tokens,
            "judge_tokens_today": store.auto_judge_tokens_today()}
    return {"verdicts": rows, "quarantine": quarantine, "auto": auto, "appeals": _appeals(library)}


def _quarantine(body: QuarantineBody) -> dict:
    if body.skill not in skills.library():
        raise HTTPException(status_code=404, detail=f"no skill named {body.skill}")
    store.quarantine(body.skill, body.model, body.reason)
    return {"ok": True}


# SQLite and skill hashing block, so they run off the event loop; to_thread carries the request's
# context (the profile it is scoped to) into the worker thread.
@router.get("/verdicts")
async def verdicts() -> dict:
    return await asyncio.to_thread(_verdicts)


@router.post("/quarantine")
async def quarantine(body: QuarantineBody) -> dict:
    return await asyncio.to_thread(_quarantine, body)


@router.post("/release")
async def release(body: SkillModelBody) -> dict:
    return {"released": await asyncio.to_thread(store.release, body.skill, body.model)}


@router.get("/appeal")
async def appeal(skill: str, model: str) -> dict:
    found = await asyncio.to_thread(lambda: next(
        (a for a in _appeals(skills.library()) if (a["skill"], a["model"]) == (skill, model)), None))
    if found is None:
        raise HTTPException(status_code=404, detail=f"{skill} has no appeal on {model}")
    return found


def _apply(body: SkillModelBody) -> dict:
    # The same command a terminal runs, so its checks (granted, unchanged since the draft, not
    # locked) and its atomic write live in one place.
    profile = profile_name_for_home(get_hermes_home())
    if profile is None:
        raise HTTPException(status_code=409, detail="Skill Court needs a Hermes profile home")
    proc = procs.run([procs.hermes_binary(), "-p", profile, "court", "appeal", "apply", body.skill, "--model", body.model],
                     timeout=120, check=False)
    message = " ".join((proc.stdout or proc.stderr).split())
    if proc.returncode != 0:
        raise HTTPException(status_code=409, detail=message.removeprefix("Skill Court: "))
    return {"ok": True, "message": message}


@router.post("/appeal/apply")
async def apply_appeal(body: SkillModelBody) -> dict:
    return await asyncio.to_thread(_apply, body)
