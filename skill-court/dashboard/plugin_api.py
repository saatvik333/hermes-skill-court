"""Desktop backend: verdicts, usage evidence, quarantine, appeals and the automatic-trials switch,
under /api/plugins/skill-court/."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

from fastapi import APIRouter, HTTPException
from hermes_constants import get_hermes_home, profile_name_for_home
from pydantic import BaseModel

# Hermes imports this file on its own, outside the plugin package. The plugin's folder is mounted as
# this package, without running its ``__init__`` (which registers hooks), so its modules import each
# other as they do in the plugin.
_PACKAGE = "skill_court_dashboard"
if _PACKAGE not in sys.modules:
    _package = ModuleType(_PACKAGE)
    _package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[_PACKAGE] = _package

store = importlib.import_module(f"{_PACKAGE}.store")
skills = importlib.import_module(f"{_PACKAGE}.skills")
procs = importlib.import_module(f"{_PACKAGE}.procs")
usage = importlib.import_module(f"{_PACKAGE}.usage")

router = APIRouter()


class QuarantineBody(BaseModel):
    skill: str
    model: str
    reason: str = "quarantined from Hermes Desktop"


class SkillModelBody(BaseModel):
    skill: str
    model: str


class ApplyBody(SkillModelBody):
    appeal_id: int  # the appeal the page's dialog showed; nothing else is applied


class AutoBody(BaseModel):
    enabled: bool


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
            "mode": trial["mode"],  # sandbox or answer-only
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


def _usage() -> dict:
    """``usage.describe`` per skill with a verdict that session history shows loaded."""
    found = usage.scan(frozenset(t["skill"] for t in store.latest_trials()))
    return {"label": usage.LABEL, "skills": {skill: usage.describe(by_model) for skill, by_model in found.items()}}


def _quarantine(body: QuarantineBody) -> dict:
    if body.skill not in skills.library():
        raise HTTPException(status_code=404, detail=f"no skill named {body.skill}")
    store.quarantine(body.skill, body.model, body.reason)
    return {"ok": True}


def _set_auto(body: AutoBody) -> dict:
    # What `hermes court auto on` without options and `auto off` store: only the switch changes, the
    # limits stay as they were. The page asked the user to confirm turning it on.
    store.set_auto_settings(replace(store.auto_settings(), enabled=body.enabled))
    return {"enabled": body.enabled}


# SQLite and skill hashing block, so they run off the event loop; to_thread carries the request's
# context (the profile it is scoped to) into the worker thread.
@router.get("/verdicts")
async def verdicts() -> dict:
    return await asyncio.to_thread(_verdicts)


# Separate from /verdicts, which the page polls: this reads the whole session history, so the page
# asks for it when it opens and on Refresh.
@router.get("/usage")
async def usage_evidence() -> dict:
    return await asyncio.to_thread(_usage)


@router.post("/quarantine")
async def quarantine(body: QuarantineBody) -> dict:
    return await asyncio.to_thread(_quarantine, body)


@router.post("/release")
async def release(body: SkillModelBody) -> dict:
    return {"released": await asyncio.to_thread(store.release, body.skill, body.model)}


@router.post("/auto")
async def set_auto(body: AutoBody) -> dict:
    return await asyncio.to_thread(_set_auto, body)


@router.get("/appeal")
async def appeal(skill: str, model: str) -> dict:
    found = await asyncio.to_thread(lambda: next(
        (a for a in _appeals(skills.library()) if (a["skill"], a["model"]) == (skill, model)), None))
    if found is None:
        raise HTTPException(status_code=404, detail=f"{skill} has no appeal on {model}")
    return found


def _apply(body: ApplyBody) -> dict:
    # The same command a terminal runs, so its checks (granted, unchanged since the draft, not
    # locked, a loadable draft that passes Hermes' security scan) and its atomic write live in one
    # place. The page asked the user to confirm already, hence --yes; --appeal refuses any appeal
    # but the one the dialog named.
    profile = profile_name_for_home(get_hermes_home())
    if profile is None:
        raise HTTPException(status_code=409, detail="Skill Court needs a Hermes profile home")
    proc = procs.run([procs.hermes_binary(), "-p", profile, "court", "appeal", "apply", body.skill, "--model", body.model,
                      "--appeal", str(body.appeal_id), "--yes"], timeout=120, check=False)
    message = " ".join((proc.stdout or proc.stderr).split())
    if proc.returncode != 0:
        raise HTTPException(status_code=409, detail=message.removeprefix("Skill Court: "))
    return {"ok": True, "message": message}


@router.post("/appeal/apply")
async def apply_appeal(body: ApplyBody) -> dict:
    return await asyncio.to_thread(_apply, body)
