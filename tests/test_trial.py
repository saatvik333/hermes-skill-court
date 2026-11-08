from types import SimpleNamespace

import pytest

from skill_court import scoring, store, trial
from skill_court.arms import ANSWER_ONLY, SANDBOX, Arms
from skill_court.judge import Judge
from skill_court.runner import NO_SANDBOX, RunResult
from skill_court.skills import Skill

MODEL = "claude-sonnet-5-5"


class ChangedSkill(Skill):
    def content_hash(self) -> str:
        return "the agent's latest version"


@pytest.fixture
def skill(hermes_home, monkeypatch):
    path = hermes_home / "skills" / "csv-totals"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: csv-totals\n---\n- Amounts are in dollars unless the file says cents.\n")
    for n in (1, 2, 3):
        store.add_task("csv-totals", f"Total export {n}", "test -f /workspace/total.txt", "manual")
    # Every replay passes its check on the same tokens: the changed skill no longer hurts.
    monkeypatch.setattr(trial.runner, "replay", lambda *_args, **_kwargs: RunResult(
        model=MODEL, completed=True, passed=True, tokens=1_000, cost_usd=0.01))
    return ChangedSkill("csv-totals", path, "agent")


def run(skill, *, auto, apply=True):
    return trial.run(skill, store.tasks_for(skill.name), Arms("default", {"with": "w", "without": "wo"}, SANDBOX), MODEL,
                     trial.Options(apply=apply), trial.Budget(5.0, False), lambda _line: None, Judge(None), auto=auto)


class Ties:
    """``ctx.llm`` answering every comparison with a tie."""

    def __init__(self):
        self.prompts = []

    def complete_structured(self, *, input, **_):
        self.prompts.append(input[0]["text"])
        return SimpleNamespace(parsed={"better": "tie", "reason": "same total"}, model="judge-model", text="",
                               usage=SimpleNamespace(total_tokens=10))


def test_an_answer_only_trial_judges_every_pair_and_records_its_mode(skill, monkeypatch):
    replays = []

    def replay(profile, prompt, check_cmd, **kwargs):
        replays.append((check_cmd, kwargs["sandbox"]))
        return RunResult(model=MODEL, completed=True, answer="465", workspace=NO_SANDBOX, tokens=1_000, cost_usd=0.01)

    monkeypatch.setattr(trial.runner, "replay", replay)
    llm = Ties()
    outcome = trial.run(skill, store.tasks_for(skill.name), Arms("default", {"with": "w", "without": "wo"}, ANSWER_ONLY),
                        MODEL, trial.Options(), trial.Budget(5.0, False), lambda _line: None, Judge(llm))
    # The tasks have checks, but no sandbox to run them in: every pair went to the judge, in both orders.
    assert replays == [(None, False)] * 6
    assert len(store.judgments_for(outcome.trial_id)) == 3 and len(llm.prompts) == 6
    assert all(NO_SANDBOX in prompt for prompt in llm.prompts)
    assert (outcome.sources, outcome.mode, outcome.score.verdict) == ({"judge": 3}, ANSWER_ONLY, scoring.NEUTRAL)
    assert store.latest_trials()[0]["mode"] == ANSWER_ONLY
    assert trial.describe_sources(outcome.sources, outcome.mode) == "3 judged, answer-only"


def test_an_automatic_answer_only_trial_never_hides_a_skill_for_the_tokens_loading_it_costs(skill, monkeypatch):
    # The run with the skill spends a call loading it (twice the tokens) and gives the same answer.
    monkeypatch.setattr(trial.runner, "replay", lambda profile, *_args, **_kwargs: RunResult(
        model=MODEL, completed=True, answer="465", workspace=NO_SANDBOX, tokens=2_000 if profile == "w" else 1_000,
        cost_usd=0.01))
    outcome = trial.run(skill, store.tasks_for(skill.name), Arms("default", {"with": "w", "without": "wo"}, ANSWER_ONLY),
                        MODEL, trial.Options(apply=True), trial.Budget(5.0, False), lambda _line: None, Judge(Ties()),
                        auto=True)
    assert (outcome.score.verdict, outcome.quarantined, round(outcome.score.token_change, 2)) == (
        scoring.NEUTRAL, False, 1.0)
    assert store.hidden_for(MODEL) == {}


def test_a_trial_the_budget_cut_short_is_no_verdict_but_one_with_too_few_tasks_is(skill):
    arms_ = Arms("default", {"with": "w", "without": "wo"}, SANDBOX)
    # Two pairs start at once and spend $0.04; the third never starts.
    cut = trial.run(skill, store.tasks_for(skill.name), arms_, MODEL, trial.Options(), trial.Budget(0.01, False),
                    lambda _line: None, Judge(None), auto=True)
    assert (cut.score.verdict, cut.stopped) == (scoring.INCONCLUSIVE, trial.BUDGET)
    assert not store.has_verdict(skill.name, skill.content_hash(), MODEL)
    short = trial.run(skill, store.tasks_for(skill.name)[:1], arms_, MODEL, trial.Options(), trial.Budget(5.0, False),
                      lambda _line: None, Judge(None), auto=True)
    assert (short.score.verdict, short.stopped) == (scoring.INCONCLUSIVE, None)
    assert store.has_verdict(skill.name, skill.content_hash(), MODEL)


def test_a_trial_stops_for_the_judge_cap_only_when_its_own_judge_calls_were_refused(skill):
    arms_ = Arms("default", {"with": "w", "without": "wo"}, SANDBOX)
    judge = Judge(None)
    judge.capped = True  # an earlier trial in the batch used up today's judge tokens
    short = trial.run(skill, store.tasks_for(skill.name)[:1], arms_, MODEL, trial.Options(), trial.Budget(5.0, False),
                      lambda _line: None, judge, auto=True)
    # Checked tasks need no judge: too few tasks is the reason, and that version counts as tried.
    assert (short.score.verdict, short.stopped) == (scoring.INCONCLUSIVE, None)
    assert store.has_verdict(skill.name, skill.content_hash(), MODEL)


def test_a_trial_stopped_for_want_of_a_price_counts_so_it_is_not_retried_unattended(skill, monkeypatch):
    monkeypatch.setattr(trial.runner, "replay", lambda *_args, **_kwargs: RunResult(
        model=MODEL, completed=True, passed=True, tokens=1_000, cost_usd=None))
    unpriced = trial.run(skill, store.tasks_for(skill.name), Arms("default", {"with": "w", "without": "wo"}, SANDBOX),
                         MODEL, trial.Options(), trial.Budget(5.0, False), lambda _line: None, Judge(None), auto=True)
    assert (unpriced.score.verdict, unpriced.stopped) == (scoring.INCONCLUSIVE, trial.NO_PRICE)
    assert store.has_verdict(skill.name, skill.content_hash(), MODEL)


def test_a_changed_skill_that_no_longer_hurts_gets_the_courts_quarantine_lifted(skill):
    store.quarantine(skill.name, MODEL, f"{store.COURT_REASON}4: success fell from 100% to 0%")
    store.quarantine(skill.name, "other-model", f"{store.COURT_REASON}5: success fell")
    outcome = run(skill, auto=True)
    assert outcome.score.verdict == scoring.NEUTRAL and outcome.released
    assert store.hidden_for(MODEL) == {}
    assert skill.name in store.hidden_for("other-model")  # judged only for the model that ran


def test_a_quarantine_set_by_hand_or_a_manual_trial_keeps_the_skill_hidden(skill):
    store.quarantine(skill.name, MODEL, "quarantined manually")
    assert not run(skill, auto=True).released
    assert store.hidden_for(MODEL) == {skill.name: "quarantined manually"}
    store.quarantine(skill.name, MODEL, f"{store.COURT_REASON}4: success fell")
    assert not run(skill, auto=False).released  # a manual trial only ever adds quarantines
    assert not run(skill, auto=True, apply=False).released  # an appeal's draft trial changes none
    assert skill.name in store.hidden_for(MODEL)


def _adjudicate(n_tasks, passes):
    """``passes(task_id, arm)``: whether that run passes; every run uses the same tokens."""
    tasks = [store.Task(i, "s", f"task {i}", "check", "manual", None) for i in range(n_tasks)]
    ran = []

    def replay(task, arm):
        ran.append((task.id, arm))
        return RunResult(model=MODEL, completed=True, passed=passes(task.id, arm), tokens=1_000, cost_usd=0.0)

    score, _ = trial.adjudicate(tasks, replay, trial.Options(tasks=n_tasks), trial.Budget(5.0, False),
                                lambda *_: None, lambda _line: None)
    return score, {task_id for task_id, _arm in ran}


def test_a_first_wave_with_no_difference_settles_the_trial_without_the_rest():
    score, ran = _adjudicate(6, lambda _task, _arm: True)
    assert ran == set(range(trial.FIRST_WAVE)) and score.verdict == scoring.NEUTRAL


def test_any_difference_in_the_first_wave_runs_every_task():
    # The skill breaks every task but task 1, so the first wave differs and the rest are run too.
    score, ran = _adjudicate(6, lambda task, arm: arm == "without" or task == 1)
    assert ran == set(range(6))
    assert score.verdict == scoring.HURTS
