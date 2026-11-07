"""Appeals: draft a fix for a harmful skill, prove it on trial, and apply it only when asked.

``draft_and_try`` asks the judge for the smallest edit to SKILL.md that removes a ``hurts``
verdict's recorded cause, then tries the draft on the same tasks: the with-skill replay profile
gets the draft in place of the original, the without-skill profile is unchanged. ``apply`` writes a
granted draft over the live SKILL.md (atomically) and lifts the quarantine for that model; ``undo``
writes the stored original back, byte for byte, and restores the quarantine. Nothing else in
Skill Court writes a skill file.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable

from hermes_constants import get_hermes_home

from . import arms, scoring, store, trial
from .judge import Judge
from .procs import CommandError
from .skills import Skill

GRANTED, DENIED = "granted", "denied"


def frontmatter_name(text: str) -> str | None:
    """The ``name`` in a SKILL.md's leading ``---`` block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip().strip("\"'")
    return None


def check_draft(original: str, draft: str) -> str:
    """The draft, ending in a newline; refused when it renames the skill or changes nothing."""
    if frontmatter_name(draft) != frontmatter_name(original):
        raise CommandError(f"the draft renames the skill ({frontmatter_name(original)!r} → {frontmatter_name(draft)!r}); "
                           "nothing was tried")
    if draft.strip() == original.strip():
        raise CommandError("the draft changes nothing; nothing was tried")
    return draft if draft.endswith("\n") else draft + "\n"


def diff(name: str, original: str, draft: str) -> str:
    return "".join(difflib.unified_diff(original.splitlines(keepends=True), draft.splitlines(keepends=True),
                                        f"a/{name}/SKILL.md", f"b/{name}/SKILL.md"))


def decide(original_success: float | None, draft: scoring.Score) -> tuple[str, str]:
    """Granted when the draft no longer hurts and its success with the skill beats the original's."""
    if draft.verdict == scoring.INCONCLUSIVE:
        return DENIED, f"the draft's trial was inconclusive: {draft.reason}"
    if draft.verdict == scoring.HURTS:
        return DENIED, f"the draft still hurts: {draft.reason}"
    if draft.success_with is None or original_success is None or draft.success_with <= original_success:
        shown = "–" if draft.success_with is None else f"{draft.success_with:.0%}"
        before = "–" if original_success is None else f"{original_success:.0%}"
        return DENIED, f"the draft did not beat the original's success ({shown} with the draft, {before} with the original)"
    return GRANTED, (f"the draft no longer hurts ({draft.verdict}) and succeeded on {draft.success_with:.0%} of "
                     f"tasks, up from {original_success:.0%} with the original")


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` in one rename, keeping its permissions."""
    mode = path.stat().st_mode & 0o7777
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _appealed(skill: str, model: str) -> dict:
    current = next((t for t in store.latest_trials([skill]) if t["model"] == model), None)
    if current is None or current["verdict"] != scoring.HURTS:
        verdict = "no verdict" if current is None else f"'{current['verdict']}'"
        raise CommandError(f"{skill} has {verdict} on {model}; only a skill that hurts can be appealed")
    if not current["cause"] or not current["evidence"]:
        raise CommandError(f"trial #{current['id']} recorded no cause to fix; run `hermes court trial {skill} "
                           f"--model {model}` again to record one")
    return current


def draft_and_try(source: str, skill: Skill, model: str, opts: trial.Options, budget: trial.Budget,
                  judge: Judge, say: Callable[[str], None], *, task_limit: int | None = None,
                  auto: bool = False) -> dict:
    """Draft a fix for ``skill``'s latest ``hurts`` verdict on ``model`` and put it on the same tasks
    (the first ``task_limit`` of them, when given); returns the decided appeal row."""
    appealed = _appealed(skill.name, model)
    task_ids = {run["task_id"] for run in store.runs_for(appealed["id"])}
    tasks = [task for task in store.tasks_for(skill.name) if task.id in task_ids][:task_limit]
    if not tasks:
        raise CommandError(f"the tasks of trial #{appealed['id']} were removed; nothing to try a draft on")
    try:
        relative = skill.path.relative_to(get_hermes_home() / "skills")
    except ValueError:
        raise CommandError(f"{skill.name} lives outside the profile's skills directory ({skill.path}), so its "
                           "replay copy cannot be swapped for a draft") from None

    original_bytes = (skill.path / "SKILL.md").read_bytes()
    original = original_bytes.decode("utf-8")
    drafted, tokens = judge.draft(original, appealed["cause"], appealed["cause_quote"], json.loads(appealed["evidence"]))
    drafted = check_draft(original, drafted)
    changes = diff(skill.name, original, drafted)
    say(f"Drafted a fix for {skill.name} ({tokens:,} judge tokens):\n{changes}")

    with tempfile.TemporaryDirectory(prefix="skill-court-appeal-") as scratch:
        # The draft as a whole skill directory, so its content hash is the one the live skill has once
        # the draft is applied.
        draft_dir = Path(scratch) / skill.path.name
        shutil.copytree(skill.path, draft_dir)
        (draft_dir / "SKILL.md").write_bytes(drafted.encode("utf-8"))
        drafted_skill = replace(skill, path=draft_dir)
        appeal_id = store.add_appeal(skill.name, model, appealed["id"], skill.content_hash(), original_bytes,
                                     drafted, drafted_skill.content_hash(), changes)
        hidden = [*arms.source_disabled_skills(source), *store.hidden_for(model)]
        replay_arms = arms.for_source(source)
        say(f"\nTrying the draft: {len(tasks)} task(s) x {opts.reps} rep(s), with the draft and without the skill, on {model}")
        try:
            arms.prepare(replay_arms)
            arms.set_skill_under_test(replay_arms, skill.name, hidden)
            replica = arms.profile_home(replay_arms.profiles["with"]) / "skills" / relative / "SKILL.md"
            if not replica.is_file():
                raise CommandError(f"the with-skill replay profile has no copy of {skill.name} at {replica}")
            replica.write_bytes(drafted.encode("utf-8"))
            outcome = trial.run(drafted_skill, tasks, replay_arms, model, replace(opts, tasks=len(tasks), apply=False),
                                budget, say, judge, auto, appeal_id)
        finally:
            arms.remove(replay_arms)

    verdict, reason = decide(json.loads(appealed["summary"] or "{}").get("success_with"), outcome.score)
    store.decide_appeal(appeal_id, outcome.trial_id, verdict, reason)
    appeal = store.latest_appeal(skill.name, model)
    assert appeal is not None and appeal["id"] == appeal_id
    return appeal


def _quarantine_reason(skill: str, model: str) -> str | None:
    return next((reason for s, m, reason, _since in store.quarantine_entries() if (s, m) == (skill, model)), None)


def apply(skill: Skill, model: str) -> dict:
    """Write the granted draft over the live SKILL.md and lift the quarantine for ``model``."""
    appeal = store.latest_appeal(skill.name, model)
    if appeal is None:
        raise CommandError(f"{skill.name} has no appeal on {model}; draft one with `hermes court appeal {skill.name} "
                           f"--model {model}`")
    if appeal["applied_at"]:
        raise CommandError(f"the fix from appeal #{appeal['id']} is already applied")
    if appeal["verdict"] != GRANTED:
        state = f"was {appeal['verdict']}: {appeal['reason']}" if appeal["verdict"] else "has no verdict yet"
        raise CommandError(f"appeal #{appeal['id']} {state}; only a granted fix is applied")
    live = skill.content_hash()
    if live != appeal["base_hash"]:
        raise CommandError(f"{skill.name} changed since appeal #{appeal['id']} was drafted (content hash "
                           f"{appeal['base_hash'][:12]} → {live[:12]}); draft a new appeal")
    if skill.locked:
        raise CommandError(f"{skill.name} is locked (`hermes curator lock`); unlock it to apply the fix")
    path = skill.path / "SKILL.md"
    write_atomic(path, appeal["draft"].encode("utf-8"))
    if skill.content_hash() != appeal["draft_hash"]:
        write_atomic(path, appeal["original"])
        raise CommandError(f"{skill.name}'s files changed while the fix was written; the original was put back")
    lifted = _quarantine_reason(skill.name, model)
    if lifted is not None:
        store.release(skill.name, model)
    store.set_applied(appeal["id"], True, lifted)
    return appeal


def undo(skill: Skill, model: str) -> dict:
    """Write the original SKILL.md back over an applied draft and restore the quarantine it lifted."""
    appeal = store.latest_appeal(skill.name, model)
    if appeal is None or not appeal["applied_at"]:
        raise CommandError(f"no applied fix of {skill.name} on {model} to undo")
    live = skill.content_hash()
    if live != appeal["draft_hash"]:
        raise CommandError(f"{skill.name} changed since the fix was applied (content hash {appeal['draft_hash'][:12]} → "
                           f"{live[:12]}); not overwriting it")
    if skill.locked:
        raise CommandError(f"{skill.name} is locked (`hermes curator lock`); unlock it to undo the fix")
    write_atomic(skill.path / "SKILL.md", appeal["original"])
    if appeal["lifted"] is not None:
        store.quarantine(skill.name, model, appeal["lifted"])
    store.set_applied(appeal["id"], False)
    return appeal
