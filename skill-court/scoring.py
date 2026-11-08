"""Paired with/without comparison and the verdict rules. Pure functions; no Hermes imports.

Each pair is one task run once with the skill and once without it. Success is the sandbox check
when the task has one, else the run completing. Repeated runs of one task are not independent
evidence, so they are averaged per task first and every interval is a bootstrap over tasks (fixed
seed, so a report is reproducible). A verdict needs at least ``min_tasks`` tasks with outcomes in
both arms and its whole 95% interval on one side; with three tasks that means every task moved the
same way. With ``efficiency`` off (answer-only trials) only success decides a verdict: a run there is
a few calls, so the extra call that loads the skill is a large share of its tokens and a token
verdict would mostly measure that load; the token change is still reported.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass

HELPS, HURTS, NEUTRAL, INCONCLUSIVE = "helps", "hurts", "neutral", "inconclusive"


@dataclass(frozen=True)
class Arm:
    success: bool | None
    tokens: int | None


@dataclass(frozen=True)
class Pair:
    task: int
    with_skill: Arm
    without_skill: Arm


@dataclass(frozen=True)
class Score:
    tasks: int  # distinct tasks with an outcome in both arms
    pairs: int  # pairs with an outcome in both arms
    success_with: float | None
    success_without: float | None
    success_delta: float | None
    success_ci: tuple[float, float] | None
    token_change: float | None  # relative: +0.4 means the skill made runs use 40% more tokens
    token_ci: tuple[float, float] | None
    verdict: str
    reason: str


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _bootstrap_ci(values: list[float], resamples: int, rng: random.Random) -> tuple[float, float]:
    n = len(values)
    means = sorted(_mean([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def _per_task(values: dict[int, list[float]]) -> list[float]:
    return [_mean(v) for _, v in sorted(values.items())]


def _pct(x: float) -> str:
    return f"{round(x * 100):+d}%"


def score(pairs: list[Pair], *, min_tasks: int = 3, efficiency_margin: float = 0.25, efficiency: bool = True,
          resamples: int = 2000, seed: int = 0) -> Score:
    rng = random.Random(seed)
    judged = [p for p in pairs if p.with_skill.success is not None and p.without_skill.success is not None]
    deltas: dict[int, list[float]] = defaultdict(list)
    with_rates: dict[int, list[float]] = defaultdict(list)
    without_rates: dict[int, list[float]] = defaultdict(list)
    ratios: dict[int, list[float]] = defaultdict(list)
    for p in judged:
        deltas[p.task].append(float(p.with_skill.success) - float(p.without_skill.success))
        with_rates[p.task].append(float(p.with_skill.success))
        without_rates[p.task].append(float(p.without_skill.success))
        if p.with_skill.tokens and p.without_skill.tokens:
            ratios[p.task].append(math.log(p.with_skill.tokens / p.without_skill.tokens))

    task_deltas = _per_task(deltas)
    task_ratios = _per_task(ratios)
    n_tasks = len(task_deltas)
    success_with = _mean(_per_task(with_rates)) if n_tasks else None
    success_without = _mean(_per_task(without_rates)) if n_tasks else None
    success_delta = _mean(task_deltas) if n_tasks else None
    success_ci = _bootstrap_ci(task_deltas, resamples, rng) if n_tasks else None
    token_change = math.exp(_mean(task_ratios)) - 1 if task_ratios else None
    log_ci = _bootstrap_ci(task_ratios, resamples, rng) if task_ratios else None
    token_ci = (math.exp(log_ci[0]) - 1, math.exp(log_ci[1]) - 1) if log_ci else None

    def result(verdict: str, reason: str) -> Score:
        return Score(n_tasks, len(judged), success_with, success_without, success_delta, success_ci,
                     token_change, token_ci, verdict, reason)

    if n_tasks < min_tasks:
        return result(INCONCLUSIVE, f"{n_tasks} task(s) with outcomes in both arms; at least {min_tasks} are needed")
    assert success_ci is not None and success_with is not None and success_without is not None
    assert success_delta is not None
    if success_ci[1] < 0:
        return result(HURTS, f"success fell from {success_without:.0%} without the skill to {success_with:.0%} with it")
    if success_ci[0] > 0:
        return result(HELPS, f"success rose from {success_without:.0%} without the skill to {success_with:.0%} with it")
    if efficiency and token_ci is not None and token_change is not None and len(task_ratios) >= min_tasks:
        change = (f"success went from {success_without:.0%} without the skill to {success_with:.0%} with it"
                  if success_delta else "success was the same")
        if success_delta <= 0 and token_ci[0] > efficiency_margin:
            return result(HURTS, f"{change}, and runs used {_pct(token_change)} tokens with the skill")
        # The mirror of "costs 25% more" is "costs 1/1.25 as much", i.e. 20% less, not 25% less.
        if success_delta >= 0 and token_ci[1] < 1 / (1 + efficiency_margin) - 1:
            return result(HELPS, f"{change}, and runs used {_pct(token_change)} tokens with the skill")
    return result(NEUTRAL, "no measurable difference in success or token use" if efficiency
                  else "no measurable difference in success")


def settled_early(pairs: list[Pair], *, min_tasks: int = 3, efficiency_margin: float = 0.25,
                  efficiency: bool = True) -> bool:
    """Whether the first pairs already show nothing worth more replays: every judged pair ended the
    same way in both arms, on at least ``min_tasks`` tasks, and (with ``efficiency``) the skill's token
    change is inside the efficiency margin (between 1/1.25 and 1.25 times the tokens for the default)."""
    judged = [p for p in pairs if p.with_skill.success is not None and p.without_skill.success is not None]
    if len({p.task for p in judged}) < min_tasks or any(p.with_skill.success != p.without_skill.success for p in judged):
        return False
    if not efficiency:
        return True
    change = score(pairs, min_tasks=min_tasks, efficiency_margin=efficiency_margin).token_change
    return change is not None and abs(math.log1p(change)) < math.log1p(efficiency_margin)
