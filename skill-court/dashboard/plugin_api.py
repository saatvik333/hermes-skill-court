"""Desktop backend: verdicts and quarantine, under /api/plugins/skill-court/."""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Hermes loads this file on its own, outside the plugin package, so the package's modules are
# imported through a bare package shell (its ``register`` is not run again).
_PACKAGE = "skill_court_dashboard"
if _PACKAGE not in sys.modules:
    shell = types.ModuleType(_PACKAGE)
    shell.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[_PACKAGE] = shell
store = importlib.import_module(f"{_PACKAGE}.store")
skills = importlib.import_module(f"{_PACKAGE}.skills")

router = APIRouter()


class QuarantineBody(BaseModel):
    skill: str
    model: str
    reason: str = "quarantined from Hermes Desktop"


class ReleaseBody(BaseModel):
    skill: str
    model: str | None = None


@router.get("/verdicts")
async def verdicts() -> dict:
    library = skills.library()
    hidden = {(skill, model): reason for skill, model, reason, _ in store.quarantine_entries()}
    rows = []
    for trial in store.latest_trials():
        current = library.get(trial["skill"])
        rows.append({
            "skill": trial["skill"],
            "model": trial["model"],
            "verdict": trial["verdict"],
            "judged_at": trial["finished_at"],
            "trial_id": trial["id"],
            "skill_changed": current is None or current.content_hash() != trial["skill_hash"],
            "quarantined": (trial["skill"], trial["model"]) in hidden or (trial["skill"], store.ANY_MODEL) in hidden,
            **json.loads(trial["summary"] or "{}"),
        })
    quarantine = [{"skill": s, "model": m, "reason": r, "since": c} for s, m, r, c in store.quarantine_entries()]
    return {"verdicts": rows, "quarantine": quarantine}


@router.post("/quarantine")
async def quarantine(body: QuarantineBody) -> dict:
    if body.skill not in skills.library():
        raise HTTPException(status_code=404, detail=f"no skill named {body.skill}")
    store.quarantine(body.skill, body.model, body.reason)
    return {"ok": True}


@router.post("/release")
async def release(body: ReleaseBody) -> dict:
    return {"released": store.release(body.skill, body.model)}
