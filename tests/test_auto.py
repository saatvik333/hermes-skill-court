import fcntl
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from skill_court import auto, store

ON = store.AutoSettings(enabled=True, daily_budget_usd=1.0, quiet_minutes=15)


@pytest.fixture(autouse=True)
def started(monkeypatch):
    """Profiles a background runner was started for (none is really started)."""
    profiles: list[str] = []
    monkeypatch.setattr(auto, "_spawn", profiles.append)
    return profiles


def test_changes_are_recorded_only_when_auto_mode_is_on():
    auto.on_skill_lifecycle(action="created", skill_name="csv-totals", session_id="s1")
    assert store.auto_queue_rows() == []
    store.set_auto_settings(ON)
    auto.on_skill_lifecycle(action="loaded", skill_name="csv-totals", session_id="s1")
    auto.on_skill_lifecycle(action="installed", skill_name="hub-skill", session_id="s1")
    auto.on_skill_lifecycle(action="patched", skill_name="csv-totals", session_id="s1")
    assert [(r["skill"], r["session_id"], r["state"]) for r in store.auto_queue_rows()] == [("csv-totals", "s1", "pending")]


def test_a_change_waits_for_its_session_to_end(started):
    store.set_auto_settings(ON)
    auto.on_skill_lifecycle(action="created", skill_name="csv-totals", session_id="s1")
    assert auto.launch_ready(ON) == []
    auto.on_session_finalize(session_id="other-session")
    assert started == []
    auto.on_session_finalize(session_id="s1")
    assert started == ["default"]
    assert [r["state"] for r in store.auto_queue_rows()] == ["queued"]


def test_a_change_in_a_session_that_stays_open_starts_after_the_quiet_period(started, hermes_home):
    store.set_auto_settings(ON)
    auto.on_skill_lifecycle(action="edited", skill_name="csv-totals", session_id="s1")
    now = datetime.now(timezone.utc)
    assert auto.launch_ready(ON, now + timedelta(minutes=14)) == []
    assert auto.launch_ready(ON, now + timedelta(minutes=16)) == ["csv-totals"]
    assert started == ["default"]
    lock = hermes_home / "plugin-data" / store.PLUGIN / store.TRIAL_LOCK
    with open(lock, "w") as runner:  # the runner it started holds the lock: no second one
        fcntl.flock(runner, fcntl.LOCK_EX)
        assert auto.launch_ready(ON, now + timedelta(minutes=30)) == []
    assert started == ["default"]
    # That runner died with the change still queued: the next turn starts another.
    auto.launch_ready(ON, now + timedelta(minutes=31))
    assert started == ["default", "default"]


def test_a_further_change_during_a_trial_waits_for_its_own_trial():
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    auto.launch_ready(ON)
    assert store.start_queued() == {"csv-totals": ["s1"]}
    store.queue_change("csv-totals", "s1")  # the agent patched it again mid-trial
    store.finish_queued("csv-totals", None, "neutral")
    assert [r["state"] for r in store.auto_queue_rows()] == ["pending"]


def test_a_killed_runner_leaves_its_rows_for_the_next_one():
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    auto.launch_ready(ON)
    store.start_queued()
    assert store.start_queued() == {}
    assert store.requeue_orphans() == 1
    assert store.start_queued() == {"csv-totals": ["s1"]}


def auto_trial(started_at: datetime, cost: float, automatic: bool = True) -> None:
    trial = store.start_trial("s", "h", "m", auto=automatic)
    store.record_run(trial, {"task_id": 1, "arm": "with", "rep": 0, "cost_usd": cost})
    with closing(store._connect()) as conn, conn:
        conn.execute("UPDATE trials SET started_at = ? WHERE id = ?",
                     (started_at.astimezone(timezone.utc).isoformat(timespec="seconds"), trial))


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
    assert [r["state"] for r in store.auto_queue_rows()] == ["pending"]  # waits for tomorrow
    assert auto.launch_ready(ON, now + timedelta(days=1)) == ["csv-totals"]


class Ctx:
    def __init__(self, ok):
        self.ok = ok
        self.posted = []

    def post_message(self, text):
        self.posted.append(text)
        return SimpleNamespace(ok=self.ok)


def test_a_verdict_is_posted_once_at_the_next_turn():
    store.add_notice(None, "first")
    store.add_notice(None, "second")
    ctx = Ctx(ok=True)
    assert auto.notifier(ctx)(session_id="s2", user_message="hi") is None  # nothing for the model
    assert ctx.posted == ["first", "second"]
    auto.notifier(ctx)(session_id="s3")
    assert ctx.posted == ["first", "second"]


def test_a_notice_that_could_not_be_posted_waits_for_the_next_turn():
    store.add_notice(None, "first")
    store.add_notice(None, "second")
    failing = Ctx(ok=False)
    auto.notifier(failing)()
    assert failing.posted == ["first"]
    working = Ctx(ok=True)
    auto.notifier(working)()
    assert working.posted == ["first", "second"]
