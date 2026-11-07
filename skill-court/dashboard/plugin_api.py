"""Desktop backend: verdicts and quarantine, under /api/plugins/skill-court/."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


def _sibling(name: str) -> ModuleType:
    """Load one of the plugin's own modules from the folder above. Hermes imports this file on its
    own, outside the plugin package; ``store`` and ``skills`` have no relative imports."""
    path = Path(__file__).resolve().parents[1] / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"skill_court_dashboard_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _sibling("store")
skills = _sibling("skills")

router = APIRouter()


class QuarantineBody(BaseModel):
    skill: str
    model: str
    reason: str = "quarantined from Hermes Desktop"


class ReleaseBody(BaseModel):
    skill: str
    model: str


def _verdicts() -> dict:
    library = skills.library()
    hidden = {(skill, model) for skill, model, _reason, _since in store.quarantine_entries()}
    rows = []
    for trial in store.latest_trials():
        current = library.get(trial["skill"])
        rows.append({
            **json.loads(trial["summary"] or "{}"),
            "skill": trial["skill"],
            "model": trial["model"],
            "verdict": trial["verdict"],
            "judged_at": trial["finished_at"],
            "trial_id": trial["id"],
            "skill_changed": current is None or current.content_hash() != trial["skill_hash"],
            "quarantined": (trial["skill"], trial["model"]) in hidden or (trial["skill"], store.ANY_MODEL) in hidden,
        })
    quarantine = [{"skill": s, "model": m, "reason": r, "since": c} for s, m, r, c in store.quarantine_entries()]
    return {"verdicts": rows, "quarantine": quarantine}


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
async def release(body: ReleaseBody) -> dict:
    return {"released": await asyncio.to_thread(store.release, body.skill, body.model)}
