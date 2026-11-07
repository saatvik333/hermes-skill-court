import argparse
import fcntl
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from skill_court import auto, cli, scoring, store, trial
from skill_court.procs import CommandError

ON = store.AutoSettings(enabled=True, daily_budget_usd=1.0, quiet_minutes=15, daily_judge_tokens=1_000)
MODEL = "claude-sonnet-5-5"
OWNER = store.Origin("cli-1", store.LOCAL, None)


@pytest.fixture(autouse=True)
def started(monkeypatch):
    """Profiles a background runner was started for (none is really started)."""
    profiles: list[str] = []
    monkeypatch.setattr(auto, "_spawn", profiles.append)
    return profiles


def rows():
    return store.auto_queue_rows(100)


def test_changes_are_recorded_only_when_auto_mode_is_on():
    auto.on_skill_lifecycle(action="created", skill_name="csv-totals", session_id="s1")
    assert rows() == []
    store.set_auto_settings(ON)
    auto.on_skill_lifecycle(action="loaded", skill_name="csv-totals", session_id="s1")
    auto.on_skill_lifecycle(action="installed", skill_name="hub-skill", session_id="s1")
    auto.on_skill_lifecycle(action="patched", skill_name="csv-totals", session_id="s1")
    assert [(r["skill"], r["session_id"], r["state"]) for r in rows()] == [("csv-totals", "s1", "pending")]


def test_a_change_waits_for_its_session_to_end(started):
    store.set_auto_settings(ON)
    auto.on_skill_lifecycle(action="created", skill_name="csv-totals", session_id="s1")
    assert auto.launch_ready(ON) == []
    auto.on_session_finalize(session_id="other-session")
    assert started == []
    auto.on_session_finalize(session_id="s1")
    assert started == ["default"]
    assert [r["state"] for r in rows()] == ["queued"]


def test_an_unfinished_change_gets_a_new_runner_at_most_every_retry_interval(started, hermes_home):
    store.set_auto_settings(ON)
    auto.on_skill_lifecycle(action="edited", skill_name="csv-totals", session_id="s1")
    now = datetime.now(timezone.utc)
    assert auto.launch_ready(ON, now + timedelta(minutes=14)) == []
    assert auto.launch_ready(ON, now + timedelta(minutes=16)) == ["csv-totals"]
    assert started == ["default"]
    # That runner died with the change still queued: no new one before the retry interval.
    auto.launch_ready(ON, now + timedelta(minutes=16 + auto.RETRY_MINUTES - 1))
    assert started == ["default"]
    later = now + timedelta(minutes=16 + auto.RETRY_MINUTES)
    lock = hermes_home / "plugin-data" / store.PLUGIN / store.TRIAL_LOCK
    with open(lock, "w") as runner:  # a runner holds the lock: no second one
        fcntl.flock(runner, fcntl.LOCK_EX)
        auto.launch_ready(ON, later)
    assert started == ["default"]
    auto.launch_ready(ON, later)
    assert started == ["default", "default"]


def test_a_change_put_back_after_a_failed_attempt_waits_unless_the_agent_changes_it_again(started):
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    now = datetime.now(timezone.utc)
    auto.launch_ready(ON, now)
    store.start_queued()
    store.defer_queued("csv-totals", "waiting: Docker is not running")  # what the runner does then
    assert auto.launch_ready(ON, now + timedelta(minutes=1)) == []
    assert auto.launch_ready(ON, now + timedelta(minutes=auto.RETRY_MINUTES)) == ["csv-totals"]
    assert started == ["default", "default"]
    store.start_queued()
    store.defer_queued("csv-totals", "waiting: Docker is not running")
    store.queue_change("csv-totals", "s1")  # a new change is tried as soon as it is ready
    store.end_session("s1")
    assert auto.launch_ready(ON, now + timedelta(minutes=auto.RETRY_MINUTES + 1)) == ["csv-totals"]


def test_a_further_change_during_a_trial_waits_for_its_own_trial():
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    auto.launch_ready(ON)
    assert store.start_queued() == {"csv-totals": ["s1"]}
    store.queue_change("csv-totals", "s1")  # the agent patched it again mid-trial
    store.finish_queued("csv-totals", None, "neutral")
    assert [r["state"] for r in rows()] == ["pending"]


def test_a_killed_runner_leaves_its_rows_for_the_next_one():
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    auto.launch_ready(ON)
    store.start_queued()
    assert store.start_queued() == {}
    assert store.requeue_orphans() == 1
    assert store.start_queued() == {"csv-totals": ["s1"]}


def auto_trial(started_at: datetime, cost: float, automatic: bool = True) -> None:
    trial_id = store.start_trial("s", "h", "m", auto=automatic)
    store.record_run(trial_id, {"task_id": 1, "arm": "with", "rep": 0, "cost_usd": cost})
    with closing(store._connect()) as conn, conn:
        conn.execute("UPDATE trials SET started_at = ? WHERE id = ?",
                     (started_at.astimezone(timezone.utc).isoformat(timespec="seconds"), trial_id))


def test_the_daily_budget_counts_todays_automatic_trials_only():
    now = datetime.now(timezone.utc)
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    auto_trial(midnight - timedelta(minutes=1), 5.0)  # yesterday
    auto_trial(now, 3.0, automatic=False)  # a manual trial
    auto_trial(midnight + timedelta(seconds=1), 0.4)
    auto_trial(now, 0.2)
    assert round(store.auto_spent_today(now), 6) == 0.6


def test_nothing_starts_once_todays_budget_is_spent(started):
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    now = datetime.now(timezone.utc)
    auto_trial(now, 1.0)
    assert auto.launch_ready(ON, now) == [] and started == []
    assert [r["state"] for r in rows()] == ["pending"]  # waits for tomorrow
    assert auto.launch_ready(ON, now + timedelta(days=1)) == ["csv-totals"]


def test_nothing_starts_once_todays_judge_tokens_are_used(started):
    store.record_judge_call("judge", "m", 5_000, None, auto=False)  # a manual trial's judge does not count
    store.record_judge_call("judge", "m", 600, None, auto=True)
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    assert auto.launch_ready(ON) == ["csv-totals"]
    store.record_judge_call("cause", "m", 400, None, auto=True)
    assert store.auto_judge_tokens_today() == ON.daily_judge_tokens
    store.queue_change("json-format", "s2")
    store.end_session("s2")
    assert auto.launch_ready(ON) == [] and started == ["default"]
    assert [r["state"] for r in rows() if r["skill"] == "json-format"] == ["pending"]  # waits for tomorrow


class Ctx:
    """A plugin context whose posts return ``results`` in turn (then a delivered post)."""

    def __init__(self, *results, surface="cli"):
        self.results = list(results)
        self.surface = surface
        self.posted = []

    def session_surface(self):
        return self.surface

    def post_message(self, text):
        self.posted.append(text)
        result = self.results.pop(0) if self.results else {"ok": True, "message_id": "m1"}
        return SimpleNamespace(ok=bool(result.get("ok")), error=result.get("error"), result=result)


def turn(ctx=None, session_id="cli-2", platform="cli", sender=""):
    ctx = ctx or Ctx()
    assert auto.notifier(ctx)(session_id=session_id, platform=platform, sender_id=sender, user_message="hi") is None
    return ctx.posted


def test_a_verdict_is_posted_once_at_the_next_turn():
    store.add_notice(None, "first", OWNER)
    store.add_notice(None, "second", OWNER)
    assert turn() == ["first", "second"]  # nothing goes to the model
    assert turn() == []


def test_a_notice_that_could_not_be_posted_waits_for_the_next_turn():
    store.add_notice(None, "first", OWNER)
    store.add_notice(None, "second", OWNER)
    assert turn(Ctx({"ok": False, "error": "no_conversation"})) == ["first"]
    assert turn() == ["first", "second"]


def test_a_post_that_timed_out_still_lands_and_one_nobody_saw_waits():
    store.add_notice(None, "first", OWNER)
    store.add_notice(None, "second", OWNER)
    # The gateway did not answer in time: the post still lands, so it is not posted twice.
    assert turn(Ctx({"ok": False, "error": "timeout"})) == ["first"]
    # A local session without a viewer accepts the post but nobody sees it.
    assert turn(Ctx({"ok": True, "message_id": "m2", "delivered": False})) == ["second"]
    assert turn() == ["second"]
    assert turn() == []


def test_a_notice_reaches_only_its_session_or_the_same_sender_in_the_same_chat(monkeypatch):
    chats = {"tg-2": "dm-bob", "tg-3": "group-7", "tg-4": "dm-alice", "dc-1": "dm-alice"}
    monkeypatch.setattr(auto.mining, "session_origin", lambda session: ("telegram", None, chats.get(session)))
    store.add_notice(None, "for alice", store.Origin("tg-1", "telegram", "alice", "dm-alice"))
    store.add_notice(None, "for an api client", store.Origin("api-1", "api_server", None))
    store.add_notice(None, "for the owner", OWNER)
    store.add_notice(None, "origin unknown", store.Origin(None, None, None))
    assert turn(session_id="tg-2", platform="telegram", sender="bob") == []
    assert turn(session_id="dc-1", platform="discord", sender="alice") == []  # same ids, another platform
    # A group alice writes in never gets the verdict about her direct chat.
    assert turn(session_id="tg-3", platform="telegram", sender="alice") == []
    assert turn(session_id="api-1", platform="api_server") == ["for an api client"]
    assert turn(session_id="tg-4", platform="telegram", sender="alice") == ["for alice"]
    store.add_notice(None, "for alice again", store.Origin("tg-1", "telegram", "alice", "dm-alice"))
    # A notice nobody else can be told goes to the owner's own clients, never someone else's chat.
    assert turn(session_id="tui-1", platform="tui") == ["for the owner", "origin unknown"]
    assert turn(Ctx(surface="desktop"), session_id="", platform="") == []
    assert turn(session_id="tg-1", platform="telegram", sender="alice") == ["for alice again"]


def test_who_a_notice_is_for_comes_from_the_session_record(monkeypatch):
    records = {"tg": ("telegram", "42", "dm-42"), "tg-group": ("telegram", "42", "group-1"),
               "desk": ("local", "nous-account", None), "old": ("tui", None, None), "cron": ("cron", None, None)}

    def session_origin(session_id):
        if session_id == "broken":
            raise sqlite3.OperationalError("database is locked")
        return records.get(session_id, (None, None, None))

    monkeypatch.setattr(auto.mining, "session_origin", session_origin)
    assert auto.origin("tg") == store.Origin("tg", "telegram", "42", "dm-42")
    # A local client's label (Desktop stamps an account) is not a sender to look for elsewhere.
    assert auto.origin("desk") == store.Origin("desk", store.LOCAL, None)
    assert auto.origin("old") == store.Origin("old", store.LOCAL, None)
    assert auto.origin("cron") == store.Origin("cron", "cron", None)
    assert auto.origin("broken") == store.Origin("broken", None, None)
    assert auto.origin("") == store.Origin(None, None, None)
    # One notice per audience: each sender in each chat, and the owner once.
    assert auto.origins(["desk", "old", "tg", "tg-group", "cron"]) == [
        store.Origin("desk", store.LOCAL, None), store.Origin("tg", "telegram", "42", "dm-42"),
        store.Origin("tg-group", "telegram", "42", "group-1")]


def test_a_runner_that_cannot_start_never_stops_notices(monkeypatch):
    def no_hermes(_profile):
        raise CommandError("the `hermes` command is not on PATH")

    monkeypatch.setattr(auto, "_spawn", no_hermes)
    store.set_auto_settings(ON)
    auto.on_skill_lifecycle(action="created", skill_name="csv-totals", session_id="s1")
    auto.on_session_finalize(session_id="s1")  # the hook itself never fails
    store.add_notice(None, "verdict", OWNER)
    assert turn() == ["verdict"]
    [row] = rows()
    assert row["state"] == "queued" and "not on PATH" in row["note"] and row["last_attempt_at"]


def score(verdict):
    return scoring.Score(3, 3, 1.0, 1.0, 0.0, None, 0.0, None, verdict, f"{verdict} reason")


def outcome(verdict, trial_id=1, released=False, sources=None):
    return trial.Outcome(trial_id, MODEL, score(verdict), verdict == scoring.HURTS, released,
                         {"judge": 3} if sources is None else sources, 0, 0, None, None, 0)


def test_a_notice_never_says_a_hidden_skill_is_available():
    assert "it stays available" in auto.notice("csv-totals", outcome(scoring.NEUTRAL))
    store.quarantine("csv-totals", MODEL, "quarantined manually")
    text = auto.notice("csv-totals", outcome(scoring.NEUTRAL))
    assert "stays available" not in text
    assert f"still hidden from {MODEL} (quarantined manually)" in text
    assert f"`hermes court release csv-totals --model {MODEL}` shows it again" in text
    store.release("csv-totals", None)
    store.quarantine("csv-totals", store.ANY_MODEL, "manual")
    assert "`hermes court release csv-totals` shows it again" in auto.notice("csv-totals", outcome(scoring.HELPS))
    lifted = auto.notice("csv-totals", outcome(scoring.HELPS, released=True))
    assert "quarantine an earlier trial set" in lifted and "still hidden" in lifted


def test_every_command_in_a_harmful_verdicts_notice_names_the_model():
    text = auto.notice("csv-totals", outcome(scoring.HURTS), {"verdict": "granted", "reason": "it no longer hurts"})
    for command in ("release", "appeal show", "appeal apply"):
        assert f"`hermes court {command} csv-totals --model {MODEL}`" in text


def test_a_trial_the_judge_could_not_decide_names_the_judge_error():
    assert "Add tasks" in auto.notice("csv-totals", outcome(scoring.INCONCLUSIVE, sources={}))
    trial_id = store.start_trial("csv-totals", "h", MODEL, auto=True)
    for task_id in (1, 2, 3):
        store.record_judgment(trial_id, {"task_id": task_id, "rep": 0, "error": "APIError: provider overloaded"})
    text = auto.notice("csv-totals", outcome(scoring.INCONCLUSIVE, trial_id, sources={}))
    assert "The judge failed on 3 task(s): APIError: provider overloaded." in text
    assert "Add tasks" not in text


@pytest.fixture
def runner(monkeypatch):
    """``hermes court trial --auto`` with Docker, the skill library and history stubbed."""
    monkeypatch.setattr(cli, "container_binary", lambda: "docker")
    monkeypatch.setattr(cli, "run", lambda argv, **_: None)
    monkeypatch.setattr(cli.skills, "library", lambda: {"csv-totals": SimpleNamespace(name="csv-totals")})
    monkeypatch.setattr(cli.arms, "source_disabled_skills", lambda _source: [])
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    store.set_auto_settings(ON)
    return lambda: cli.dispatch(parser.parse_args(["trial", "--auto"]), None)


def queue(*sessions):
    for session in sessions:
        store.queue_change("csv-totals", session)
        store.end_session(session)
    auto.launch_ready(ON)


def test_the_runner_addresses_each_verdict_to_whoever_changed_the_skill(runner, monkeypatch):
    monkeypatch.setattr(auto.mining, "session_origin",
                        {"tg-1": ("telegram", "42", "dm-42"), "cli-1": ("local", None, None)}.get)
    monkeypatch.setattr(cli, "_auto_tasks", lambda *_: [store.Task(1, "csv-totals", "Total it", None, "mined", None)])

    def try_skills(_source, _library, selected, *_args, after, **_kwargs):
        done = [(name, outcome(scoring.NEUTRAL, store.start_trial(name, "h", MODEL, auto=True))) for name in selected]
        for name, result in done:
            after(name, result)
        return done

    monkeypatch.setattr(cli, "_try_skills", try_skills)
    queue("tg-1", "cli-1")
    assert runner() == 0
    assert sorted((n["session_id"], n["platform"], n["sender"], n["chat_id"]) for n in store.notices(10)) == [
        ("cli-1", store.LOCAL, None, None), ("tg-1", "telegram", "42", "dm-42")]
    assert {r["state"] for r in rows()} == {"done"}


def test_a_runner_that_cannot_reach_docker_puts_the_changes_back(runner, monkeypatch, started):
    def no_docker(argv, **_):
        raise CommandError("`docker version …` exited 1: Cannot connect to the Docker daemon")

    monkeypatch.setattr(cli, "run", no_docker)
    queue("s1")
    assert runner() == 1
    [row] = rows()
    assert row["state"] == "pending" and "Cannot connect to the Docker daemon" in row["note"]
    assert store.notices(10) == []
    assert auto.launch_ready(ON) == [] and started == ["default"]  # not again before the retry interval


def test_an_unexpected_runner_failure_closes_the_change_and_says_so(runner, monkeypatch, started):
    def broken(*_):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(cli, "_auto_tasks", broken)
    monkeypatch.setattr(auto.mining, "session_origin", lambda _session: ("telegram", "42", "dm-42"))
    queue("tg-1")
    assert runner() == 1
    [row] = rows()
    assert row["state"] == "done" and "malformed" in row["note"]
    [sent] = store.notices(10)
    assert "automatic trial of `csv-totals` failed" in sent["text"] and "malformed" in sent["text"]
    assert (sent["platform"], sent["sender"]) == ("telegram", "42")
    later = datetime.now(timezone.utc) + timedelta(minutes=auto.RETRY_MINUTES + 1)
    assert auto.launch_ready(ON, later) == [] and started == ["default"]  # never retried by itself
