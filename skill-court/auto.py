"""Automatic trials: test a skill after the agent writes or changes it, within a daily budget.

Off until ``hermes court auto on``. The ``on_skill_lifecycle`` hook queues each skill the agent
creates, patches or edits, keyed by the session that changed it. When that session ends
(``on_session_finalize``), or once the change has been quiet for ``quiet_minutes`` (checked at the
start of any later turn), a detached ``hermes -p <profile> court trial --auto`` runs the queue in
the background with the same sandboxed replay profiles as a manual trial, appending its output
to ``plugin-data/skill-court/auto.log``. A runner is started again for changes it left unfinished
at most every ``RETRY_MINUTES``. A skill that hurts is quarantined for the model it hurt; a
changed skill that no longer hurts gets the quarantine an earlier trial set lifted. Each verdict is
posted once with ``ctx.post_message`` (a notice beside the conversation, never text in the model's
context) into the next turn of the session that changed the skill or another session of the same
sender on the same platform; a change made on the owner's own clients, or by a sender Hermes did
not record, is posted only on those clients (CLI, TUI, Desktop).
"""

from __future__ import annotations

import fcntl
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from hermes_constants import get_hermes_home, profile_name_for_home
from plugins.plugin_storage import plugin_data_dir

from . import mining, scoring, store, trial
from .appeal import GRANTED
from .procs import CommandError, child_env, hermes_binary

logger = logging.getLogger(__name__)

# Skill lifecycle actions that change what the agent will be told (``tools/skill_usage.py``).
CHANGES = frozenset({"created", "patched", "edited"})
LOG = "auto.log"
# A background runner is started again for changes still unfinished (Docker was down, the runner
# was killed or could not start) at most this often.
RETRY_MINUTES = 30
# The profile owner's own clients (``ctx.session_surface()`` and the hook's ``platform``).
LOCAL_SURFACES = frozenset({"cli", "tui", "desktop"})
# Session sources of the owner's clients: Hermes stores them as ``local``; older rows, editor
# (``acp``) and one-shot CLI runs keep the client's own label.
_LOCAL_SOURCES = frozenset({"local", "cli", "tui", "gui", "desktop", "acp", "oneshot", "tool"})


def on_skill_lifecycle(action: str = "", skill_name: str = "", session_id: str = "", **_: Any) -> None:
    if action in CHANGES and skill_name and store.auto_settings().enabled:
        store.queue_change(skill_name, session_id or "")


def _launch(settings: store.AutoSettings) -> None:
    """``launch_ready`` from a hook: a runner that cannot start must not take the hook down with it."""
    try:
        launch_ready(settings)
    except Exception:
        logger.warning("Skill Court: could not start automatic trials", exc_info=True)


def on_session_finalize(session_id: Any = None, **_: Any) -> None:
    settings = store.auto_settings()
    if settings.enabled and session_id and store.end_session(str(session_id)):
        _launch(settings)


def _delivered(handle: Any) -> bool:
    """A post the user can see: a local session with no viewer reports ``delivered`` False."""
    return handle.ok and handle.result.get("delivered") is not False


def _read_origin(session_id: str) -> tuple[str | None, str | None, str | None]:
    """``mining.session_origin``; Nones when the history cannot be read, which leaves a notice to
    the narrowest audience: its own session, or the owner."""
    try:
        return mining.session_origin(session_id)
    except Exception:
        logger.warning("Skill Court: could not read session %s", session_id, exc_info=True)
        return None, None, None


def notifier(ctx: Any) -> Callable[..., None]:
    """The ``pre_llm_call`` hook: start trials whose changes went quiet, and post the finished
    verdicts meant for this turn's session. It returns nothing, so nothing is added to the model's
    context."""
    def on_pre_llm_call(session_id: str = "", platform: str = "", sender_id: str = "", **_: Any) -> None:
        settings = store.auto_settings()
        if settings.enabled:
            _launch(settings)
        surface = platform or ctx.session_surface()
        local = surface in LOCAL_SURFACES
        # The turn's chat, read only when a notice waits for this sender on this platform.
        chat_id = (_read_origin(session_id)[2] if not local and sender_id and session_id
                   and store.has_notices_for(surface, sender_id) else None)
        claimed = store.claim_notices(session_id or "", surface, sender_id or "", chat_id, local)
        for index, (notice_id, text) in enumerate(claimed):
            handle = ctx.post_message(text)
            if _delivered(handle):
                continue
            # A post the gateway has not finished in time still lands; the rest wait for a later turn.
            for unposted, _text in claimed[index + 1 if handle.error == "timeout" else index:]:
                store.unclaim_notice(unposted)
            break
    return on_pre_llm_call


def origin(session_id: str) -> store.Origin:
    """Who a notice about a change made in ``session_id`` is for. A session whose platform or sender
    Hermes did not record, or that the owner ran on a local client, belongs to the owner."""
    source, sender, chat_id = _read_origin(session_id)
    if source in _LOCAL_SOURCES:
        return store.Origin(session_id or None, store.LOCAL, None)
    return store.Origin(session_id or None, source, sender, chat_id)


def origins(sessions: list[str]) -> list[store.Origin]:
    """One origin per audience of the sessions that changed a skill: each sender in each chat, and
    the owner."""
    found: dict[tuple[str | None, str, str | None] | None, store.Origin] = {}
    for session in sessions:
        o = origin(session)
        found.setdefault((o.platform, o.sender, o.chat_id) if o.sender else None, o)
    return list(found.values())


def notify(audience: list[store.Origin], trial_id: int | None, text: str) -> None:
    for o in audience:
        store.add_notice(trial_id, text, o)


def _spawn(profile: str) -> None:
    data = plugin_data_dir(store.PLUGIN)
    with open(data / LOG, "a", encoding="utf-8") as log:
        log.write(f"\n=== {datetime.now().astimezone().isoformat(timespec='seconds')} starting automatic trials\n")
        log.flush()
        # Its own process session, so it outlives the turn and the session that started it.
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


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def launch_ready(settings: store.AutoSettings, now: datetime | None = None) -> list[str]:
    """Queue the changes that are ready and start a background runner for them; nothing starts once
    today's budget or judge tokens are spent. A runner also starts for queued changes that no runner
    holds (one was killed, for example by a gateway restart), at most every ``RETRY_MINUTES``.
    Returns the skills newly queued."""
    now = now or datetime.now(timezone.utc)
    profile = profile_name_for_home(get_hermes_home())
    if (profile is None or store.auto_spent_today(now) >= settings.daily_budget_usd
            or store.auto_judge_tokens_today(now) >= settings.daily_judge_tokens):
        return []
    retry_before = _iso(now - timedelta(minutes=RETRY_MINUTES))
    skills = store.claim_ready(_iso(now - timedelta(minutes=settings.quiet_minutes)), retry_before)
    if skills or (store.unfinished(retry_before) and _no_trial_running()):
        try:
            _spawn(profile)
        except (OSError, CommandError) as exc:
            store.mark_attempt(_iso(now), f"the background runner could not start: {exc}")
            raise
        store.mark_attempt(_iso(now))
    return skills


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _still_hidden(skill: str, model: str) -> str:
    """What to add when ``skill`` is still hidden from ``model``: why, and how to show it again."""
    reason = store.hidden_for(model).get(skill)
    if reason is None:
        return ""
    everyone = any(s == skill and m == store.ANY_MODEL for s, m, *_ in store.quarantine_entries())
    command = f"hermes court release {skill}" + ("" if everyone else f" --model {model}")
    return f" It is still hidden from {model} ({reason.rstrip('.')}); `{command}` shows it again."


def notice(skill: str, outcome: trial.Outcome, proposed: dict | None = None) -> str:
    """What the user is told about one automatic trial; ``proposed``: the appeal tried after a
    ``hurts`` verdict, if any."""
    score, model = outcome.score, outcome.model
    evidence = trial.describe_sources(outcome.sources)
    if score.verdict == scoring.HURTS:
        what = (f"made {model} fail {outcome.failed_with} of {outcome.solved_without} task(s) it solved without it"
                if outcome.failed_with else f"hurt {model}: {score.reason}")
        lines = [f"Skill Court: `{skill}` {what} ({evidence})."]
        if outcome.quarantined:
            lines.append(f"Hidden for {model}; `hermes court release {skill} --model {model}` shows it again.")
        if outcome.cause:
            lines.append(f"Why: {_sentence(outcome.cause)}")
            if outcome.quote:
                lines.append(f"Skill line: \"{outcome.quote}\"")
        if proposed is not None and proposed["verdict"] == GRANTED:
            lines.append(f"Fix proven: {_sentence(proposed['reason'])} See it with `hermes court appeal show {skill} "
                         f"--model {model}`, apply it with `hermes court appeal apply {skill} --model {model}`.")
        elif proposed is not None:
            lines.append(f"A drafted fix did not pass its trial: {_sentence(proposed['reason'])}")
        return "\n".join(lines)
    hidden = _still_hidden(skill, model)
    lifted = f" The quarantine an earlier trial set for {model} is lifted." if outcome.released else ""
    if score.verdict == scoring.HELPS:
        return f"Skill Court: `{skill}` helps {model}: {score.reason} ({evidence}).{lifted}{hidden}"
    if score.verdict == scoring.NEUTRAL:
        stays = "" if hidden else "; it stays available"
        return f"Skill Court: `{skill}` made no measurable difference for {model} ({evidence}){stays}.{lifted}{hidden}"
    text = f"Skill Court: no verdict for `{skill}` on {model} yet: {score.reason}."
    failed = [j["error"] for j in store.judgments_for(outcome.trial_id) if j["error"]]
    if failed:
        text += (f" The judge failed on {len(failed)} task(s): {_sentence(failed[0])} "
                 f"Run `hermes court trial {skill}` once it works again.")
    else:
        text += f" Add tasks with `hermes court tasks add {skill} --prompt …`."
    return text + hidden
