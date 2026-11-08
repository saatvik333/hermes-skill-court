"""A trial: replay a skill's tasks with and without the skill, then score the pairs.

A task with a check is scored by its check; a task without one by the blind pairwise judge
(``judge.py``), which compares the two runs' answers and files as soon as both have finished. An
answer-only trial has no sandbox for a check to run in, so the judge scores every pair.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from typing import Callable

from plugins.plugin_storage import plugin_data_dir

from . import runner, scoring, store
from .arms import ANSWER_ONLY, ARMS, SANDBOX, Arms
from .judge import Attempt, Judge, Judgment, excerpt, successes
from .skills import Skill


@dataclass(frozen=True)
class Options:
    tasks: int = 6
    reps: int = 1
    parallel: int = 4
    timeout: float = 900
    model: str | None = None
    provider: str | None = None
    apply: bool = False


class Budget:
    """Spend reported by the provider. A run whose model answered but reported no price makes the
    total unknown; then no new runs start unless ``allow_unpriced``."""

    def __init__(self, limit_usd: float, allow_unpriced: bool) -> None:
        self.limit = limit_usd
        self.allow_unpriced = allow_unpriced
        self.spent = 0.0
        self.unpriced = 0
        # Pairs that never started because the budget stopped new runs.
        self.skipped = 0

    def add(self, result: runner.RunResult) -> None:
        if result.cost_usd is not None:
            self.spent += result.cost_usd
        elif result.tokens:
            self.unpriced += 1

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit or (self.unpriced > 0 and not self.allow_unpriced)

    def why_stopped(self) -> str:
        if self.spent >= self.limit:
            return f"budget of ${self.limit:.2f} reached"
        return "the provider reported no price for a run, so the budget cannot be enforced (pass --allow-unpriced to continue)"


@dataclass(frozen=True)
class Outcome:
    trial_id: int
    model: str
    score: scoring.Score
    quarantined: bool
    released: bool  # an automatic trial lifted the quarantine an earlier trial set for this model
    sources: dict[str, int]  # pairs with an outcome in both arms, by where it came from: check or judge
    failed_with: int  # tasks solved without the skill (every rep) that failed with it (every rep)
    solved_without: int
    cause: str | None
    quote: str | None  # the SKILL.md line the cause names, verified to occur there
    judge_tokens: int
    mode: str = SANDBOX  # how the replays ran: arms.SANDBOX or arms.ANSWER_ONLY
    # Why an inconclusive trial got too few outcomes when its tasks were not the cause (``BUDGET`` …).
    stopped: str | None = None


# What cut an inconclusive trial short. ``budget``: today's replay budget or judge tokens ran out,
# so the automatic trial runs again tomorrow; ``no price``: a run reported no price, so the budget
# could not apply; ``judge``: the judge failed. Such a trial is no verdict on its version.
BUDGET, NO_PRICE, JUDGE_FAILED = "budget", "no price", "judge"


def describe_sources(sources: dict[str, int], mode: str = SANDBOX) -> str:
    """Where a trial's pair outcomes came from, e.g. ``2 checked, 1 judged``, or ``3 judged,
    answer-only`` for a trial without sandboxes."""
    parts = [f"{sources[key]} {label}" for key, label in (("check", "checked"), ("judge", "judged")) if sources.get(key)]
    text = ", ".join(parts) or "no outcomes"
    return f"{text}, answer-only" if mode == ANSWER_ONLY else text


def _success(result: runner.RunResult, has_check: bool) -> bool | None:
    return result.passed if has_check else result.completed


def _describe(result: runner.RunResult, has_check: bool) -> str:
    outcome = _success(result, has_check)
    if outcome is None:
        state = "no outcome"
    elif has_check:
        state = "pass" if outcome else "FAIL"
    else:
        state = "done" if outcome else "incomplete"
    tokens = f"{result.tokens / 1000:.0f}k tokens" if result.tokens else "? tokens"
    cost = f"${result.cost_usd:.3f}" if result.cost_usd is not None else "$?"
    line = f"{state:<10} {result.api_calls or 0:>3} calls  {tokens:>11}  {cost}  {result.seconds or 0:.0f}s"
    return f"{line}  ({result.error})" if result.error else line


Replay = Callable[[store.Task, str], runner.RunResult]
Record = Callable[[store.Task, int, str, runner.RunResult], None]
# ``(with, without)`` success of one pair: (task, rep, with-skill run, without-skill run).
PairOutcome = Callable[[store.Task, int, runner.RunResult, runner.RunResult], tuple[bool | None, bool | None]]


def check_outcome(task: store.Task, _rep: int, with_run: runner.RunResult,
                  without_run: runner.RunResult) -> tuple[bool | None, bool | None]:
    """Success from the task's check, or from the run completing when it has none."""
    has_check = task.check_cmd is not None
    return _success(with_run, has_check), _success(without_run, has_check)


# Tasks replayed before deciding whether the rest are needed (see ``scoring.settled_early``).
FIRST_WAVE = 4


def adjudicate(tasks: list[store.Task], replay: Replay, opts: Options, budget: Budget, record: Record,
               say: Callable[[str], None], outcome: PairOutcome = check_outcome,
               efficiency: bool = True) -> tuple[scoring.Score, Counter]:
    """Replay ``tasks`` in both arms and score the pairs. ``replay(task, arm)`` runs one arm once;
    ``record`` sees every result; ``outcome`` turns each finished pair into its two successes;
    ``efficiency``: whether token use can decide the verdict (``scoring.score``).
    The first ``FIRST_WAVE`` tasks run first; the rest run only when those leave something to settle.
    Returns the score and how often each model answered."""
    chosen = tasks[: opts.tasks]
    by_id = {task.id: task for task in chosen}
    results: dict[tuple[int, int], dict[str, runner.RunResult]] = {}
    pairs: dict[tuple[int, int], scoring.Pair] = {}
    total = len(chosen) * opts.reps * len(ARMS)
    finished = 0
    slots = max(len(ARMS), opts.parallel)

    def run_wave(wave: list[store.Task]) -> None:
        nonlocal finished
        queue = deque((task, rep) for rep in range(opts.reps) for task in wave)
        with ThreadPoolExecutor(max_workers=slots) as pool:
            in_flight: dict[Future, tuple[store.Task, int, str]] = {}
            while queue or in_flight:
                # Both arms of a pair start together, so time-of-day effects hit them equally.
                while queue and len(in_flight) + len(ARMS) <= slots and not budget.exhausted:
                    task, rep = queue.popleft()
                    for arm in ARMS:
                        in_flight[pool.submit(replay, task, arm)] = (task, rep, arm)
                if not in_flight:
                    budget.skipped += len(queue)
                    say(f"  {budget.why_stopped()}; {len(queue)} pair(s) not run")
                    break
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    task, rep, arm = in_flight.pop(future)
                    result = future.result()
                    budget.add(result)
                    record(task, rep, arm, result)
                    results.setdefault((task.id, rep), {})[arm] = result
                    finished += 1
                    say(f"  [{finished}/{total}] task {task.id} {arm:<7} {_describe(result, task.check_cmd is not None)}")
        for key, by_arm in results.items():
            if key in pairs or len(by_arm) != len(ARMS):
                continue
            with_run, without_run = by_arm["with"], by_arm["without"]
            with_ok, without_ok = outcome(by_id[key[0]], key[1], with_run, without_run)
            pairs[key] = scoring.Pair(key[0], scoring.Arm(with_ok, with_run.tokens),
                                      scoring.Arm(without_ok, without_run.tokens))

    first, rest = chosen[:FIRST_WAVE], chosen[FIRST_WAVE:]
    run_wave(first)
    if rest and scoring.settled_early(list(pairs.values()), efficiency=efficiency):
        margin = " and token use stayed within the efficiency margin" if efficiency else ""
        say(f"  stopped after {len(first)} task(s): every pair ended the same way in both arms{margin}, so the "
            f"other {len(rest)} were not run")
    elif rest:
        run_wave(rest)  # past the budget it only reports the pairs it could not run
    observed = Counter(r.model for by_arm in results.values() for r in by_arm.values() if r.model)
    return scoring.score(list(pairs.values()), efficiency=efficiency), observed


def _regressions(decided: dict[tuple[int, int], tuple[bool, bool]]) -> tuple[int, int]:
    """``(failed with the skill, solved without it)``, counting a task solved without the skill when
    every rep solved it, and failed with it when every rep failed."""
    by_task: dict[int, list[tuple[bool, bool]]] = defaultdict(list)
    for (task_id, _rep), pair in decided.items():
        by_task[task_id].append(pair)
    solved = [pairs for pairs in by_task.values() if all(without for _with, without in pairs)]
    return sum(1 for pairs in solved if not any(with_ok for with_ok, _ in pairs)), len(solved)


def run(skill: Skill, tasks: list[store.Task], arms: Arms, model: str, opts: Options, budget: Budget,
        say: Callable[[str], None], judge: Judge, auto: bool = False, appeal_id: int | None = None) -> Outcome:
    """``appeal_id``: this trial tries that appeal's draft (``skill.path`` holds the drafted skill).
    ``arms.mode`` decides how the replays run."""
    if arms.mode == ANSWER_ONLY:
        # No sandbox for a check to run in: every pair goes to the judge, on its final answers.
        tasks = [replace(task, check_cmd=None) for task in tasks]
    trial_id = store.start_trial(skill.name, skill.content_hash(), model, auto, appeal_id, arms.mode)
    workdir = plugin_data_dir(store.PLUGIN) / "runs"
    workdir.mkdir(parents=True, exist_ok=True)
    by_id = {task.id: task for task in tasks}
    runs: dict[tuple[int, int], dict[str, runner.RunResult]] = {}
    judging: dict[tuple[int, int], Future[Judgment]] = {}
    decided: dict[tuple[int, int], tuple[bool, bool]] = {}
    sources: Counter = Counter()
    judge_tokens = 0

    def replay(task: store.Task, arm: str) -> runner.RunResult:
        return runner.replay(arms.profiles[arm], task.prompt, task.check_cmd, workdir=workdir, timeout=opts.timeout,
                             model=opts.model, provider=opts.provider, sandbox=arms.mode == SANDBOX)

    with ThreadPoolExecutor(max_workers=opts.parallel) as judges:
        def record(task: store.Task, rep: int, arm: str, result: runner.RunResult) -> None:
            store.record_run(trial_id, {"task_id": task.id, "arm": arm, "rep": rep, **result.as_row()})
            pair = runs.setdefault((task.id, rep), {})
            pair[arm] = result
            # Judge a check-less pair as soon as both runs have an outcome, while other runs go on.
            if task.check_cmd is None and len(pair) == len(ARMS) and all(r.completed is not None for r in pair.values()):
                judging[(task.id, rep)] = judges.submit(
                    judge.compare, task.prompt, Attempt.of(pair["with"]), Attempt.of(pair["without"]))

        def outcome(task: store.Task, rep: int, with_run: runner.RunResult,
                    without_run: runner.RunResult) -> tuple[bool | None, bool | None]:
            nonlocal judge_tokens
            if task.check_cmd is not None:
                source = "check"
                pair: tuple[bool | None, bool | None] = (with_run.passed, without_run.passed)
            elif (task.id, rep) in judging:
                source = "judge"
                judgment = judging[(task.id, rep)].result()
                judge_tokens += judgment.tokens
                store.record_judgment(trial_id, {"task_id": task.id, "rep": rep, **asdict(judgment)})
                say(f"  task {task.id} judged: {judgment.describe()}")
                if judgment.winner is None:
                    return None, None
                pair = successes(judgment.winner)
            else:
                return None, None  # a run without an outcome: nothing to judge
            if pair[0] is not None and pair[1] is not None:
                sources[source] += 1
                decided[(task.id, rep)] = (pair[0], pair[1])
            return pair

        # An answer-only run is a few calls, so the extra call that loads the skill is a large share of
        # its tokens and a token verdict would mostly measure that load: only success counts there.
        skipped = budget.skipped
        score, observed = adjudicate(tasks, replay, opts, budget, record, say, outcome,
                                     efficiency=arms.mode == SANDBOX)
        cut = budget.skipped > skipped
    model = observed.most_common(1)[0][0] if observed else model

    cause = quote = None
    evidence: dict = {}
    lost = next((key for key, pair in decided.items() if pair == (False, True)), None)
    if score.verdict == scoring.HURTS and lost is not None:
        task, with_run, without_run = by_id[lost[0]], runs[lost]["with"], runs[lost]["without"]
        try:
            messages = runner.session_messages(arms.profiles["with"], with_run.session_id or "")
            evidence = {"task": task.prompt, "excerpt": excerpt(messages, skill.name),
                        "without_answer": without_run.answer or "(no final answer)"}
            found = judge.explain((skill.path / "SKILL.md").read_text(encoding="utf-8"), evidence["task"],
                                  evidence["excerpt"], evidence["without_answer"])
        except Exception as exc:  # the verdict stands without a cause
            say(f"  cause: none found ({type(exc).__name__}: {exc})")
        else:
            judge_tokens += found.tokens
            cause, quote = found.text, found.quote
            say(f"  cause (task {task.id}): {cause}")
            say(f"  skill line: {quote}" if quote else "  skill line: the judge's quote is not in SKILL.md, so none is shown")

    failed_with, solved_without = _regressions(decided)
    stopped = None
    if score.verdict == scoring.INCONCLUSIVE:
        if (cut and budget.spent >= budget.limit) or judge.capped:
            stopped = BUDGET
        elif cut:
            stopped = NO_PRICE
        elif any(j["error"] for j in store.judgments_for(trial_id)):
            stopped = JUDGE_FAILED
    summary = {**asdict(score), "sources": dict(sources), "judge_tokens": judge_tokens, "stopped": stopped}
    store.finish_trial(trial_id, model, score.verdict, json.dumps(summary))
    if cause is not None:
        store.set_cause(trial_id, cause, quote, evidence)

    quarantined = released = False
    if opts.apply and score.verdict == scoring.HURTS:
        store.quarantine(skill.name, model, f"{store.COURT_REASON}{trial_id}: {score.reason}")
        quarantined = True
    elif opts.apply and auto and score.verdict in (scoring.HELPS, scoring.NEUTRAL):
        # An automatic trial tries the skill as it is now, after a change or on a model that never judged
        # this version: whatever an earlier trial hid, this version does not hurt. A quarantine set by hand stays.
        released = store.release_court_quarantine(skill.name, model)
    return Outcome(trial_id, model, score, quarantined, released, dict(sources), failed_with, solved_without,
                   cause, quote, judge_tokens, arms.mode, stopped)
