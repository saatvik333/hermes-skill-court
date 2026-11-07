"""Automatic trials: test a skill after the agent writes or changes it, within a daily budget.

Off until ``hermes court auto on``. The ``on_skill_lifecycle`` hook queues each skill the agent
creates, patches or edits, keyed by the session that changed it. When that session ends
(``on_session_finalize``), or once the change has been quiet for ``quiet_minutes`` (checked at the
start of any later turn), a detached ``hermes -p <profile> court trial --auto`` runs the queue in
the background with the same sandboxed replay profiles as a manual trial, appending its output
to ``plugin-data/skill-court/auto.log``. A skill that hurts is quarantined for the model it hurt.
Each verdict reaches the user once, posted into the next turn of any session of the profile with
``ctx.post_message`` (a notice beside the conversation, never text in the model's context).
"""

from __future__ import annotations

import fcntl
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from hermes_constants import get_hermes_home, profile_name_for_home
from plugins.plugin_storage import plugin_data_dir

from . import scoring, store, trial
from .procs import child_env, hermes_binary

# Skill lifecycle actions that change what the agent will be told (``tools/skill_usage.py``).
CHANGES = frozenset({"created", "patched", "edited"})
LOG = "auto.log"


def on_skill_lifecycle(action: str = "", skill_name: str = "", session_id: str = "", **_: Any) -> None:
    if action in CHANGES and skill_name and store.auto_settings().enabled:
        store.queue_change(skill_name, session_id or "")


def on_session_finalize(session_id: Any = None, **_: Any) -> None:
    settings = store.auto_settings()
    if settings.enabled and session_id and store.end_session(str(session_id)):
        launch_ready(settings)


def notifier(ctx: Any) -> Callable[..., None]:
    """The ``pre_llm_call`` hook: start trials whose changes went quiet, and post finished verdicts
    to the conversation. It returns nothing, so nothing is added to the model's context."""
    def on_pre_llm_call(**_: Any) -> None:
        settings = store.auto_settings()
        if settings.enabled:
            launch_ready(settings)
        claimed = store.claim_notices()
        for index, (notice_id, text) in enumerate(claimed):
            if not ctx.post_message(text).ok:
                for unposted, _text in claimed[index:]:
                    store.unclaim_notice(unposted)
                break
    return on_pre_llm_call


def _spawn(profile: str) -> None:
    data = plugin_data_dir(store.PLUGIN)
    with open(data / LOG, "a", encoding="utf-8") as log:
        log.write(f"\n=== {datetime.now().astimezone().isoformat(timespec='seconds')} starting automatic trials\n")
        log.flush()
        # Its own session, so it outlives the turn and the session that started it.
        subprocess.Popen([hermes_binary(), "-p", profile, "court", "trial", "--auto"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, cwd=data, env=child_env(), start_new_session=True)


def _no_trial_running() -> bool:
    """Whether the profile's trial lock is free, i.e. no trial (manual or automatic) is running."""
    with open(plugin_data_dir(store.PLUGIN) / store.TRIAL_LOCK, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


def launch_ready(settings: store.AutoSettings, now: datetime | None = None) -> list[str]:
    """Queue the changes that are ready and start a background runner for them; nothing starts once
    today's budget is spent. A runner also starts for queued changes that no runner holds (one was
    killed, for example by a gateway restart). Returns the skills newly queued."""
    now = now or datetime.now(timezone.utc)
    profile = profile_name_for_home(get_hermes_home())
    if profile is None or store.auto_spent_today(now) >= settings.daily_budget_usd:
        return []
    quiet_before = (now - timedelta(minutes=settings.quiet_minutes)).astimezone(timezone.utc)
    skills = store.claim_ready(quiet_before.isoformat(timespec="seconds"))
    if skills or (store.unfinished() and _no_trial_running()):
        _spawn(profile)
    return skills


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def notice(skill: str, outcome: trial.Outcome) -> str:
    """What the user is told about one automatic trial."""
    score, model = outcome.score, outcome.model
    evidence = trial.describe_sources(outcome.sources)
    if score.verdict == scoring.HURTS:
        what = (f"made {model} fail {outcome.failed_with} of {outcome.solved_without} task(s) it solved without it"
                if outcome.failed_with else f"hurt {model}: {score.reason}")
        text = f"Skill Court: `{skill}` {what} ({evidence})"
        text += "; hidden for that model." if outcome.quarantined else "."
        if outcome.cause:
            text += f" Cause: {_sentence(outcome.cause)}"
            text += f" Skill line: \"{outcome.quote}\"." if outcome.quote else ""
        if outcome.quarantined:
            text += f" `hermes court release {skill}` undoes it."
        return text
    if score.verdict == scoring.HELPS:
        return f"Skill Court: `{skill}` helps {model}: {score.reason} ({evidence})."
    if score.verdict == scoring.NEUTRAL:
        return f"Skill Court: `{skill}` made no measurable difference for {model} ({evidence}); it stays available."
    return (f"Skill Court: no verdict for `{skill}` on {model} yet: {score.reason}. "
            f"Add tasks with `hermes court tasks add {skill} --prompt …`.")
