import argparse
import fcntl
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from skill_court import auto, cli, quarantine, scoring, store, trial
from skill_court.arms import ANSWER_ONLY
from skill_court.judge import Attempt
from skill_court.procs import CommandError

ON = store.AutoSettings(enabled=True, daily_budget_usd=1.0, quiet_minutes=15, daily_judge_tokens=1_000)
MODEL = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
OWNER = store.Origin("cli-1", store.LOCAL, None)


@pytest.fixture(autouse=True)
def started(monkeypatch):
    """Profiles a background runner was started for (none is really started)."""
    profiles: list[str] = []
    monkeypatch.setattr(auto, "_spawn", profiles.append)
    return profiles


def rows():
    return store.auto_queue_rows(100)


def court(*argv):
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    return cli.dispatch(parser.parse_args(list(argv)), None)


def test_automatic_trials_are_off_until_the_user_turns_them_on_and_an_explicit_off_stays_off(started):
    assert store.auto_settings() == store.AutoSettings(False, 1.0, 15.0, 300_000)
    auto.on_skill_lifecycle(action="created", skill_name="csv-totals", session_id="s1")
    auto.on_session_finalize(session_id="s1")
    assert rows() == [] and started == []
    assert court("auto", "on") == 0
    assert store.auto_settings() == store.AutoSettings(True, 1.0, 15.0, 300_000)
    auto.on_skill_lifecycle(action="installed", skill_name="hub-skill", session_id="s2")
    auto.on_skill_lifecycle(action="created", skill_name="csv-totals", session_id="s2")
    assert [(r["skill"], r["session_id"], r["state"]) for r in rows()] == [("csv-totals", "s2", "pending")]
    assert court("auto", "off") == 0
    assert store.auto_settings() == store.AutoSettings(False, 1.0, 15.0, 300_000)
    auto.on_skill_lifecycle(action="patched", skill_name="json-format", session_id="s3")
    assert [r["skill"] for r in rows()] == ["csv-totals"]
    assert turn() == []  # the user has chosen: no first-run notice


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
    store.defer_queued("csv-totals", "", "waiting: the runner was killed")  # what the runner does then
    assert auto.launch_ready(ON, now + timedelta(minutes=1)) == []
    assert auto.launch_ready(ON, now + timedelta(minutes=auto.RETRY_MINUTES)) == ["csv-totals"]
    assert started == ["default", "default"]
    store.start_queued()
    store.defer_queued("csv-totals", "", "waiting: the runner was killed")
    store.queue_change("csv-totals", "s1")  # a new change is tried as soon as it is ready
    store.end_session("s1")
    assert auto.launch_ready(ON, now + timedelta(minutes=auto.RETRY_MINUTES + 1)) == ["csv-totals"]


def test_a_further_change_during_a_trial_waits_for_its_own_trial():
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    auto.launch_ready(ON)
    assert store.start_queued() == [store.Queued("csv-totals", "", None, ["s1"])]
    store.queue_change("csv-totals", "s1")  # the agent patched it again mid-trial
    store.finish_queued("csv-totals", "", None, "neutral")
    assert [r["state"] for r in rows()] == ["pending"]


def test_a_killed_runner_leaves_its_rows_for_the_next_one():
    store.queue_change("csv-totals", "s1")
    store.end_session("s1")
    auto.launch_ready(ON)
    store.start_queued()
    assert store.start_queued() == []
    assert store.requeue_orphans() == 1
    assert store.start_queued() == [store.Queued("csv-totals", "", None, ["s1"])]


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


@pytest.fixture
def chosen():
    """The user set automatic trials up, so no first-run notice joins the verdicts."""
    store.set_auto_settings(ON)


def test_a_verdict_is_posted_once_at_the_next_turn(chosen):
    store.add_notice(None, "first", OWNER)
    store.add_notice(None, "second", OWNER)
    assert turn() == ["first", "second"]  # nothing goes to the model
    assert turn() == []


def test_a_notice_that_could_not_be_posted_waits_for_the_next_turn(chosen):
    store.add_notice(None, "first", OWNER)
    store.add_notice(None, "second", OWNER)
    assert turn(Ctx({"ok": False, "error": "no_conversation"})) == ["first"]
    assert turn() == ["first", "second"]


def test_a_post_that_timed_out_still_lands_and_one_nobody_saw_waits(chosen):
    store.add_notice(None, "first", OWNER)
    store.add_notice(None, "second", OWNER)
    # The gateway did not answer in time: the post still lands, so it is not posted twice.
    assert turn(Ctx({"ok": False, "error": "timeout"})) == ["first"]
    # A local session without a viewer accepts the post but nobody sees it.
    assert turn(Ctx({"ok": True, "message_id": "m2", "delivered": False})) == ["second"]
    assert turn() == ["second"]
    assert turn() == []


def test_a_notice_reaches_only_its_session_or_the_same_sender_in_the_same_chat(chosen, monkeypatch):
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
    """``hermes court trial --auto`` with Docker, the skill library, the profile's model and history
    stubbed; it takes the host's LLM client (None: no judge call is made)."""
    monkeypatch.setattr(cli.arms, "sandbox_unavailable", lambda: None)
    monkeypatch.setattr(cli.skills, "library", lambda: {
        "csv-totals": SimpleNamespace(name="csv-totals", content_hash=lambda: "v1")})
    monkeypatch.setattr(cli.arms, "source_disabled_skills", lambda _source: [])
    monkeypatch.setattr(cli.arms, "source_model", lambda _source: MODEL)
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    store.set_auto_settings(ON)
    return lambda llm=None: cli.dispatch(parser.parse_args(["trial", "--auto"]), llm)


def queue(*sessions):
    for session in sessions:
        store.queue_change("csv-totals", session)
        store.end_session(session)
    auto.launch_ready(ON)


def test_a_change_whose_version_was_already_judged_on_the_profiles_model_is_not_tried_again(runner, monkeypatch):
    tried = []
    monkeypatch.setattr(cli, "_auto_tasks", lambda *_: [store.Task(1, "csv-totals", "Total it", None, "mined", None)])
    monkeypatch.setattr(cli, "_try_skills", lambda *args, **_kwargs: tried.append(args) or [])
    finished("csv-totals", "v1", MODEL)  # say, by an earlier batch's re-check on the profile's model
    queue("s1")
    assert runner() == 0 and tried == []
    [row] = rows()
    assert (row["state"], row["note"]) == ("done", f"its current version has a verdict on {MODEL}")


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


def test_a_runner_without_docker_tries_the_changes_answer_only(runner, monkeypatch):
    monkeypatch.setattr(cli.arms, "sandbox_unavailable", lambda: "`docker version …` exited 1: Cannot connect")
    monkeypatch.setattr(cli, "_auto_tasks", lambda *_: [store.Task(1, "csv-totals", "Total it", "check", "mined", None)])
    modes = []

    def try_skills(_source, _library, selected, *_args, mode, after, **_kwargs):
        modes.append(mode)
        done = [(name, outcome(scoring.NEUTRAL, store.start_trial(name, "h", MODEL, True, None, mode))) for name in selected]
        for name, result in done:
            after(name, result)
        return done

    monkeypatch.setattr(cli, "_try_skills", try_skills)
    queue("s1")
    assert runner() == 0
    assert modes == [ANSWER_ONLY] and [r["state"] for r in rows()] == ["done"]


def test_the_runners_judge_total_counts_the_drafted_fix_too(runner, monkeypatch, capsys):
    """The fix drafted and tried after a harmful verdict uses the runner's judge; the total it prints
    in ``auto.log`` counts those calls, not only the trial's comparisons."""
    monkeypatch.setattr(cli, "_auto_tasks", lambda *_: [store.Task(1, "csv-totals", "Total it", None, "mined", None)])
    llm = SimpleNamespace(complete_structured=lambda **_: SimpleNamespace(
        parsed={"better": "B", "reason": "r", "skill_md": "---\nname: csv-totals\n---\n"}, text="", model="judge-model",
        usage=SimpleNamespace(total_tokens=10, cost_usd=None)))

    def try_skills(_source, _library, selected, _opts, _budget, judge, *_args, after, **_kwargs):
        judgment = judge.compare("Total it", Attempt("4.65", ""), Attempt("465", ""))  # two calls
        result = replace(outcome(scoring.HURTS, store.start_trial("csv-totals", "h", MODEL, auto=True)),
                         cause="it divided the total by 100", judge_tokens=judgment.tokens)
        after("csv-totals", result)
        return [("csv-totals", result)]

    def draft_and_try(_source, _skill, _model, _opts, _budget, judge, _say, auto):
        judge.draft("---\nname: csv-totals\n---\n", "it divided the total by 100", None, {})  # one more
        return {"id": 1, "verdict": "denied", "reason": "it still hurts"}

    monkeypatch.setattr(cli, "_try_skills", try_skills)
    monkeypatch.setattr(cli.appeal, "draft_and_try", draft_and_try)
    queue("s1")
    assert runner(llm) == 0
    assert "The judge (skill_court_judge) used 30 tokens" in capsys.readouterr().out


def finished(skill, skill_hash, model):
    store.finish_trial(store.start_trial(skill, skill_hash, model, auto=True), model, scoring.NEUTRAL, "{}")


def test_a_skill_used_on_a_model_with_no_verdict_on_its_version_is_queued_for_that_model(monkeypatch, chosen):
    version = {"csv-totals": "v1", "json-format": "v1", "hub-skill": "v1", "written-before-install": "v1"}
    monkeypatch.setattr(auto.skills, "find", lambda name: SimpleNamespace(
        name=name, agent_authored=name != "hub-skill", content_hash=lambda: version[name]))

    def load(skill, session, model):
        quarantine.on_llm_request(session_id=session, model=model, provider="anthropic")
        auto.on_skill_lifecycle(action="loaded", skill_name=skill, session_id=session)
        return [(r["skill"], r["session_id"], r["model"]) for r in rows()]

    for skill in ("csv-totals", "json-format", "hub-skill"):
        finished(skill, "v1", MODEL)
    assert load("csv-totals", "h-1", HAIKU) == [("csv-totals", "h-1", HAIKU)]
    assert load("csv-totals", "h-1", HAIKU) == [("csv-totals", "h-1", HAIKU)]  # once per session
    assert load("csv-totals", "s-1", MODEL) == [("csv-totals", "h-1", HAIKU)]  # it has a verdict there
    assert load("hub-skill", "h-2", HAIKU) == [("csv-totals", "h-1", HAIKU)]  # not written by the agent
    # An agent-written skill no trial has seen (written before Skill Court was installed) is tried the
    # first time it is used on a model.
    assert ("written-before-install", "s-2", MODEL) in load("written-before-install", "s-2", MODEL)
    store.finish_queued("csv-totals", HAIKU, None, "done")
    # A trial the budget cut short is no verdict: the next Haiku session queues it again.
    store.finish_trial(store.start_trial("csv-totals", "v1", HAIKU, auto=True), HAIKU, scoring.INCONCLUSIVE,
                       json.dumps({"stopped": trial.BUDGET}))
    assert ("csv-totals", "h-3", HAIKU) in load("csv-totals", "h-3", HAIKU)
    # Dedupe: this version has a verdict on Haiku now (an inconclusive one for want of tasks counts
    # too), so another Haiku session queues nothing.
    store.finish_trial(store.start_trial("csv-totals", "v1", HAIKU, auto=True), HAIKU, scoring.INCONCLUSIVE,
                       json.dumps({"stopped": trial.JUDGE_FAILED}))  # a failing judge's trial counts: the user retries
    assert ("csv-totals", "h-4", HAIKU) not in load("csv-totals", "h-4", HAIKU)
    version["csv-totals"] = "v2"  # until the agent changes it
    assert ("csv-totals", "h-5", HAIKU) in load("csv-totals", "h-5", HAIKU)


def test_a_trial_the_budget_cut_short_waits_for_tomorrow_and_says_so(runner, monkeypatch):
    monkeypatch.setattr(cli, "_auto_tasks", lambda *_: [store.Task(1, "csv-totals", "Total it", None, "mined", None)])

    def try_skills(_source, _library, selected, *_args, after, **_kwargs):
        result = replace(outcome(scoring.INCONCLUSIVE, store.start_trial("csv-totals", "v1", MODEL, auto=True),
                                 sources={}), stopped=trial.BUDGET)
        after("csv-totals", result)
        return [("csv-totals", result)]

    monkeypatch.setattr(cli, "_try_skills", try_skills)
    queue("s1")
    assert runner() == 0
    [row] = rows()
    assert (row["state"], row["note"]) == ("pending", "waiting for tomorrow's budget")
    [sent] = store.notices(10)
    assert "today's budget was reached" in sent["text"] and "runs again tomorrow" in sent["text"]
    assert "Add tasks" not in sent["text"]


def test_the_runner_tries_changes_then_the_most_used_rechecks_within_the_budget(runner, monkeypatch):
    library = {name: SimpleNamespace(name=name, content_hash=lambda: "v1")
               for name in ("csv-totals", "json-format", "team-changelog")}
    monkeypatch.setattr(cli.skills, "library", lambda: library)
    monkeypatch.setattr(cli, "_auto_tasks", lambda name, *_: [store.Task(n, name, f"Do it {n}", None, "mined", None)
                                                              for n in (1, 2, 3)])

    def no_scan(_wanted):
        raise AssertionError("ordering re-checks must not scan session history")

    monkeypatch.setattr(cli.usage, "scan", no_scan)
    ran = []

    def try_skills(_source, _library, selected, opts, budget, *_args, mode, after, **_kwargs):
        [name] = selected
        ran.append((name, opts.model, opts.provider))
        budget.spent += 0.6  # two trials spend the day's $1.00
        result = outcome(scoring.NEUTRAL, store.start_trial(name, "v1", opts.model or MODEL, True, None, mode))
        after(name, result)
        return [(name, result)]

    monkeypatch.setattr(cli, "_try_skills", try_skills)
    queue("s1")
    # Loaded on Haiku in one session and in three: the most used goes first.
    for skill, sessions in (("json-format", 1), ("team-changelog", 3)):
        for n in range(sessions):
            store.queue_recheck(skill, f"h-{skill}-{n}", HAIKU, "anthropic")
            store.end_session(f"h-{skill}-{n}")
    auto.launch_ready(ON)
    assert runner() == 0
    assert ran == [("csv-totals", None, None), ("team-changelog", HAIKU, "anthropic")]
    assert {r["skill"]: r["state"] for r in rows()} == {"csv-totals": "done", "team-changelog": "done",
                                                        "json-format": "pending"}  # tomorrow's budget
    texts = [n["text"] for n in store.notices(10)]
    assert any(f"you used `team-changelog` on {MODEL}; this version was never tested on {MODEL}" in t for t in texts)


def test_the_first_run_notice_is_posted_once_in_its_own_chat_and_to_the_owner(monkeypatch):
    monkeypatch.setattr(auto.mining, "session_origin",
                        lambda session: {"tg-1": ("telegram", "42", "dm-42")}.get(session, (None, None, None)))
    [text] = turn(session_id="tg-1", platform="telegram", sender="42")
    assert text.startswith("Skill Court is installed") and "`hermes court auto on`" in text
    assert "$1.00 of your provider" in text and store.auto_settings().enabled is False
    assert turn(session_id="tg-2", platform="telegram", sender="7") == []
    # A Telegram sender's message started it: the owner is told too, on their own clients, once.
    assert turn(session_id="cli-9", platform="cli") == [text]
    assert turn(session_id="cli-9", platform="cli") == []
    assert turn(session_id="tg-1", platform="telegram", sender="42") == []
    assert not store.first_run_due()


def test_the_owners_first_run_notice_is_queued_once(monkeypatch):
    monkeypatch.setattr(auto.mining, "session_origin", lambda _session: ("local", None, None))
    [text] = turn(session_id="cli-1", platform="cli")
    assert text.startswith("Skill Court is installed")
    assert turn(session_id="cli-2", platform="cli") == [] and len(store.notices(10)) == 1


def test_no_first_run_notice_once_the_user_has_chosen():
    assert court("auto", "on", "--budget", "2") == 0
    assert turn() == [] and not store.first_run_due()


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


def test_once_todays_judge_tokens_are_used_up_the_rest_of_the_batch_waits(runner, monkeypatch):
    library = {name: SimpleNamespace(name=name, content_hash=lambda: "v1") for name in ("csv-totals", "json-format")}
    monkeypatch.setattr(cli.skills, "library", lambda: library)
    monkeypatch.setattr(cli, "_auto_tasks", lambda name, *_: [store.Task(1, name, "Do it", None, "mined", None)])
    ran = []

    def try_skills(_source, _library, selected, *_args, after, **_kwargs):
        [name] = selected
        ran.append(name)
        store.record_judge_call("judge", "m", ON.daily_judge_tokens, None, auto=True)  # the day's judge tokens
        result = outcome(scoring.NEUTRAL, store.start_trial(name, "v1", MODEL, auto=True))
        after(name, result)
        return [(name, result)]

    monkeypatch.setattr(cli, "_try_skills", try_skills)
    for skill, session in (("csv-totals", "s1"), ("json-format", "s2")):
        store.queue_change(skill, session)
        store.end_session(session)
    auto.launch_ready(ON)
    assert runner() == 0 and ran == ["csv-totals"]
    assert {r["skill"]: (r["state"], r["note"]) for r in rows()}["json-format"] == (
        "pending", "waiting for tomorrow's judge tokens")


def test_a_recheck_with_too_few_tasks_records_no_trial_and_waits_for_more_use(runner, monkeypatch):
    monkeypatch.setattr(cli, "_auto_tasks", lambda name, *_: [store.Task(1, name, "Do it", None, "mined", None)])
    monkeypatch.setattr(cli, "_try_skills", lambda *_args, **_kwargs: pytest.fail("no trial for one task"))
    store.queue_recheck("csv-totals", "h-1", HAIKU, "anthropic")
    store.end_session("h-1")
    auto.launch_ready(ON)
    assert runner() == 0
    [row] = rows()
    assert row["state"] == "done" and row["note"].startswith("waiting for more tasks: 1 of 3")
    assert store.latest_trials() == [] and store.notices(10) == []
