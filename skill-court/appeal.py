"""Appeals: draft a fix for a harmful skill, prove it on trial, and apply it only when asked.

``draft_and_try`` asks the judge for the smallest edit to SKILL.md that removes a ``hurts``
verdict's recorded cause, refuses a draft Hermes would not load as the same skill or that Hermes'
skill security scan blocks, then tries it on the same tasks: the with-skill replay profile gets a
real copy of the skill directory with the draft as its SKILL.md, the without-skill profile is
unchanged. ``apply`` writes a granted draft over the live SKILL.md (atomically) and lifts the
quarantine for that model; ``undo`` writes the stored original back, byte for byte, and restores
the quarantine. Both hold the plugin's appeal lock, write only when the live file holds exactly the
bytes they expect, and never write through a SKILL.md that is a symbolic link. Nothing else in
Skill Court writes a skill file.
"""

from __future__ import annotations

import difflib
import fcntl
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator

from hermes_constants import get_hermes_home
from plugins.plugin_storage import plugin_data_dir

from . import arms, scoring, store, trial
from .judge import Judge
from .procs import CommandError
from .skills import Skill

GRANTED, DENIED = "granted", "denied"
# A draft longer than this, or than twice the original, is refused.
MAX_DRAFT_CHARS = 100_000
# The plugin-data file ``apply`` and ``undo`` hold an exclusive lock on.
APPEAL_LOCK = "appeal.lock"


# What a Hermes without the private helpers below raises: a missing module or name, or a changed
# signature or result. Each becomes a refusal before anything is written or tried.
_UNAVAILABLE = (ImportError, AttributeError, TypeError)


def _skill_file(text: str) -> tuple[dict[str, Any], str, bool]:
    """``(frontmatter, body, closed)`` of a SKILL.md as Hermes reads it: ``parse_frontmatter`` takes
    the YAML, or ``key: value`` lines when the YAML does not parse (a repeated key, for one), where
    the last ``name`` wins."""
    try:
        from agent.skill_utils import parse_frontmatter

        frontmatter, body = parse_frontmatter(text)
    except _UNAVAILABLE as exc:
        raise CommandError(f"Hermes' skill parser (agent.skill_utils.parse_frontmatter) is unavailable on this "
                           f"Hermes version ({type(exc).__name__}: {exc})") from None
    # Without a closing ``---`` line Hermes reads no frontmatter and returns the whole text as the body.
    return frontmatter, body, body != text.removeprefix("\ufeff")


def check_draft(original: str, draft: str) -> str:
    """The draft, ending in a newline. Refused unless Hermes would load it as the same skill: a
    closed frontmatter with the original's top-level ``name`` and a description, a body, and at most
    twice the original's characters (and ``MAX_DRAFT_CHARS``); a draft that changes nothing is
    refused too."""
    draft = draft if draft.endswith("\n") else draft + "\n"
    limit = min(MAX_DRAFT_CHARS, 2 * len(original))
    if len(draft) > limit:
        raise CommandError(f"the draft has {len(draft):,} characters, more than the {limit:,} allowed (twice the "
                           f"original's, at most {MAX_DRAFT_CHARS:,})")
    before, _body, _closed = _skill_file(original)
    after, body, closed = _skill_file(draft)
    if not closed:
        raise CommandError("the draft's frontmatter is not closed by a `---` line")
    if after.get("name") != before.get("name"):
        raise CommandError(f"the draft renames the skill ({before.get('name')!r} → {after.get('name')!r})")
    if not str(after.get("description") or "").strip():
        raise CommandError("the draft has no description")
    if not body.strip():
        raise CommandError("the draft has no body")
    if draft.strip() == original.strip():
        raise CommandError("the draft changes nothing")
    return draft


def scan_refusal(skill_dir: Path) -> str | None:
    """Hermes' skill security scan of ``skill_dir`` under its policy for agent-written skills: the
    scan report when that policy would not let the skill in without asking, else None."""
    try:
        from tools.skills_guard import format_scan_report, scan_skill, should_allow_install

        result = scan_skill(skill_dir, source="agent-created")
        allowed, _reason = should_allow_install(result)
        return None if allowed is True else format_scan_report(result)
    except _UNAVAILABLE as exc:
        raise CommandError(f"Hermes' skill security scanner (tools.skills_guard) is unavailable on this Hermes "
                           f"version ({type(exc).__name__}: {exc}); the draft could not be scanned, so nothing "
                           "was written or tried") from None


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


def _draft_copy(skill: Skill, drafted: str, into: Path) -> Path:
    """A real copy of the skill's directory (links followed) under ``into``, with ``drafted`` as its
    SKILL.md: what the skill would be with the draft applied."""
    target = into / skill.path.name
    shutil.copytree(skill.path, target, ignore_dangling_symlinks=True)
    (target / "SKILL.md").write_bytes(drafted.encode("utf-8"))
    return target


def place_draft(profile: str, relative: Path, draft_dir: Path) -> Path:
    """Put a real copy of ``draft_dir`` at ``skills/<relative>`` in the replay profile ``profile`` and
    return its SKILL.md. A clone keeps the source's symbolic links, so whatever is there (a link to
    the live skill, or a copy) is removed first instead of written through."""
    home = arms.profile_home(profile).resolve()
    target = arms.profile_home(profile) / "skills" / relative
    if not target.parent.resolve().is_relative_to(home):
        raise CommandError(f"{target.parent} leads outside the replay profile {home}; the draft was not placed")
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        raise CommandError(f"the with-skill replay profile has no copy of the skill at {target}")
    shutil.copytree(draft_dir, target)
    replica = target / "SKILL.md"
    if not replica.resolve().is_relative_to(home):
        raise CommandError(f"{replica} resolves outside the replay profile {home}")
    return replica


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

    # Hashed before the bytes are read: a change in between leaves them disagreeing, and apply refuses.
    base_hash = skill.content_hash()
    original_bytes = (skill.path / "SKILL.md").read_bytes()
    original = original_bytes.decode("utf-8")
    drafted, tokens = judge.draft(original, appealed["cause"], appealed["cause_quote"], json.loads(appealed["evidence"]))
    try:
        drafted = check_draft(original, drafted)
    except CommandError as exc:
        raise CommandError(f"{exc}; nothing was tried") from None
    changes = diff(skill.name, original, drafted)
    say(f"Drafted a fix for {skill.name} ({tokens:,} judge tokens):\n{changes}")

    with tempfile.TemporaryDirectory(prefix="skill-court-appeal-") as scratch:
        # The draft as a whole skill directory, so its content hash is the one the live skill has once
        # the draft is applied.
        draft_dir = _draft_copy(skill, drafted, Path(scratch))
        refusal = scan_refusal(draft_dir)
        if refusal is not None:
            raise CommandError(f"Hermes' skill security scan blocks the draft; nothing was tried.\n{refusal}")
        drafted_skill = replace(skill, path=draft_dir)
        appeal_id = store.add_appeal(skill.name, model, appealed["id"], base_hash, original_bytes,
                                     drafted, drafted_skill.content_hash(), changes)
        hidden = [*arms.source_disabled_skills(source), *store.hidden_for(model)]
        replay_arms = arms.for_source(source)
        say(f"\nTrying the draft: {len(tasks)} task(s) x {opts.reps} rep(s), with the draft and without the skill, on {model}")
        try:
            arms.prepare(replay_arms)
            arms.set_skill_under_test(replay_arms, skill.name, hidden)
            place_draft(replay_arms.profiles["with"], relative, draft_dir)
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


@contextmanager
def _appeal_lock() -> Iterator[None]:
    """One apply or undo at a time in the profile."""
    with open(plugin_data_dir(store.PLUGIN) / APPEAL_LOCK, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _skill_md(skill: Skill, model: str) -> Path:
    """The live SKILL.md; refused when it or the skill's directory is a symbolic link, whose target
    may be shared with other files or skills and is not Skill Court's to overwrite."""
    path = skill.path / "SKILL.md"
    for link in (skill.path, path):
        if link.is_symlink():
            raise CommandError(f"{link} is a symbolic link to {os.readlink(link)}; Skill Court does not write through "
                               f"links. Apply the change shown by `hermes court appeal show {skill.name} --model "
                               f"{model}` to the linked file yourself")
    return path


def _replace_if(path: Path, expected: bytes, data: bytes) -> bool:
    """Write ``data`` over ``path`` only while it still holds ``expected``, so a rollback never
    overwrites someone else's edit."""
    try:
        if path.read_bytes() != expected:
            return False
    except OSError:
        return False
    write_atomic(path, data)
    return True


def granted(skill: Skill, model: str, appeal_id: int | None = None) -> dict:
    """The granted, unapplied appeal ``apply`` writes for ``skill`` on ``model``; raises when there is
    none, or when it is not ``appeal_id`` (the one the user was shown) when that is given."""
    appeal = store.latest_appeal(skill.name, model)
    if appeal is None:
        raise CommandError(f"{skill.name} has no appeal on {model}; draft one with `hermes court appeal {skill.name} "
                           f"--model {model}`")
    if appeal_id is not None and appeal["id"] != appeal_id:
        raise CommandError(f"appeal #{appeal_id} is not the latest appeal of {skill.name} on {model} (#{appeal['id']} "
                           "is); nothing was written")
    if appeal["applied_at"]:
        raise CommandError(f"the fix from appeal #{appeal['id']} is already applied")
    if appeal["verdict"] != GRANTED:
        state = f"was {appeal['verdict']}: {appeal['reason']}" if appeal["verdict"] else "has no verdict yet"
        raise CommandError(f"appeal #{appeal['id']} {state}; only a granted fix is applied")
    return appeal


def apply(skill: Skill, model: str, appeal_id: int | None = None) -> dict:
    """Write the granted draft over the live SKILL.md and lift the quarantine for ``model``.
    ``appeal_id``: the appeal the user was shown; refused when another one is the latest."""
    with _appeal_lock():
        appeal = granted(skill, model, appeal_id)
        if skill.locked:
            raise CommandError(f"{skill.name} is locked (`hermes curator lock`); unlock it to apply the fix")
        path = _skill_md(skill, model)
        # Hashed before the bytes are read: a change in between fails one of the two comparisons.
        if skill.content_hash() != appeal["base_hash"] or path.read_bytes() != appeal["original"]:
            raise CommandError(f"{skill.name} changed since appeal #{appeal['id']} was drafted; draft a new appeal")
        draft = appeal["draft"]
        try:
            check_draft(appeal["original"].decode("utf-8"), draft)
        except CommandError as exc:
            raise CommandError(f"{exc}; nothing was written") from None
        with tempfile.TemporaryDirectory(prefix="skill-court-apply-") as scratch:
            refusal = scan_refusal(_draft_copy(skill, draft, Path(scratch)))
        if refusal is not None:
            raise CommandError(f"Hermes' skill security scan blocks the draft; nothing was written.\n{refusal}")
        if not store.claim_apply(appeal["id"]):
            raise CommandError(f"the fix from appeal #{appeal['id']} is already applied")
        written = draft.encode("utf-8")
        try:
            write_atomic(path, written)
        except BaseException:
            store.release_apply(appeal["id"])
            raise
        if skill.content_hash() != appeal["draft_hash"]:
            restored = _replace_if(path, written, appeal["original"])
            store.release_apply(appeal["id"])
            raise CommandError(f"{skill.name}'s files changed while the fix was written; "
                               + ("the original was put back" if restored
                                  else "SKILL.md was changed by something else meanwhile and is left as it is"))
        lifted = _quarantine_reason(skill.name, model)
        if lifted is not None:
            store.release(skill.name, model)
            store.set_lifted(appeal["id"], lifted)
        return appeal


def undo(skill: Skill, model: str) -> dict:
    """Write the original SKILL.md back over the latest applied draft and restore the quarantine
    applying it lifted."""
    with _appeal_lock():
        appeal = store.latest_appeal(skill.name, model, applied=True)
        if appeal is None:
            raise CommandError(f"no applied fix of {skill.name} on {model} to undo")
        if skill.locked:
            raise CommandError(f"{skill.name} is locked (`hermes curator lock`); unlock it to undo the fix")
        path = _skill_md(skill, model)
        # Hashed before the bytes are read, as in apply.
        if skill.content_hash() != appeal["draft_hash"] or path.read_bytes() != appeal["draft"].encode("utf-8"):
            raise CommandError(f"{skill.name} changed since the fix from appeal #{appeal['id']} was applied; "
                               "not overwriting it")
        if not store.release_apply(appeal["id"]):
            raise CommandError(f"the fix from appeal #{appeal['id']} is not applied")
        try:
            write_atomic(path, appeal["original"])
        except BaseException:
            store.claim_apply(appeal["id"])
            if appeal["lifted"] is not None:
                store.set_lifted(appeal["id"], appeal["lifted"])
            raise
        if appeal["lifted"] is not None:
            store.quarantine(skill.name, model, appeal["lifted"])
        return appeal
