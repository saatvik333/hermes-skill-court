"""A trial: replay a skill's tasks with and without the skill, then score the pairs."""

from __future__ import annotations

import json
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from typing import Callable

from plugins.plugin_storage import plugin_data_dir

from . import runner, scoring, store
from .arms import ARMS, Arms
from .skills import Skill


@dataclass(frozen=True)
class Options:
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


def run(skill: Skill, tasks: list[store.Task], arms: Arms, model: str, opts: Options, budget: Budget,
        say: Callable[[str], None]) -> Outcome:
    trial_id = store.start_trial(skill.name, skill.content_hash(), model)
    workdir = plugin_data_dir(store.PLUGIN) / "runs"
    workdir.mkdir(parents=True, exist_ok=True)
    queue = deque((task, rep) for rep in range(opts.reps) for task in tasks)
    results: dict[tuple[int, int], dict[str, runner.RunResult]] = {}
    total = len(queue) * len(ARMS)
    finished = 0
    slots = max(len(ARMS), opts.parallel)

    with ThreadPoolExecutor(max_workers=slots) as pool:
        in_flight: dict[Future, tuple[store.Task, int, str]] = {}
        while queue or in_flight:
            # Both arms of a pair start together, so time-of-day effects hit them equally.
            while queue and len(in_flight) + len(ARMS) <= slots and not budget.exhausted:
                task, rep = queue.popleft()
                for arm in ARMS:
                    future = pool.submit(runner.replay, arms.profiles[arm], task.prompt, task.check_cmd,
                                         workdir=workdir, timeout=opts.timeout, model=opts.model,
                                         provider=opts.provider)
                    in_flight[future] = (task, rep, arm)
            if not in_flight:
                say(f"  {budget.why_stopped()}; {len(queue)} pair(s) not run")
                break
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                task, rep, arm = in_flight.pop(future)
                result = future.result()
                budget.add(result)
                store.record_run(trial_id, {"task_id": task.id, "arm": arm, "rep": rep, **result.as_row()})
                results.setdefault((task.id, rep), {})[arm] = result
                finished += 1
                say(f"  [{finished}/{total}] task {task.id} {arm:<7} {_describe(result, task.check_cmd is not None)}")

    checks = {task.id: task.check_cmd is not None for task in tasks}
    pairs = []
    for (task_id, _rep), by_arm in results.items():
        if len(by_arm) != len(ARMS):
            continue
        with_run, without_run = by_arm["with"], by_arm["without"]
        pairs.append(scoring.Pair(
            task_id,
            scoring.Arm(_success(with_run, checks[task_id]), with_run.tokens),
            scoring.Arm(_success(without_run, checks[task_id]), without_run.tokens),
        ))
    outcome = scoring.score(pairs)
    observed = Counter(r.model for by_arm in results.values() for r in by_arm.values() if r.model)
    model = observed.most_common(1)[0][0] if observed else model
    store.finish_trial(trial_id, model, outcome.verdict, json.dumps(asdict(outcome)))

    quarantined = False
    if opts.apply and outcome.verdict == scoring.HURTS:
        store.quarantine(skill.name, model, f"Skill Court trial #{trial_id}: {outcome.reason}")
        quarantined = True
    return Outcome(trial_id, model, outcome, quarantined)
