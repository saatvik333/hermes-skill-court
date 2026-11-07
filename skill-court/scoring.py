"""Paired with/without comparison and the verdict rules. Pure functions; no Hermes imports.

Each pair is the same task run once with the skill and once without it. Success is the sandbox
check when the task has one, else the run completing. Effects are means over pairs with 95%
percentile-bootstrap intervals (fixed seed, so a report is reproducible).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

HELPS, HURTS, NEUTRAL, INCONCLUSIVE = "helps", "hurts", "neutral", "inconclusive"


@dataclass(frozen=True)
class Arm:
    success: bool | None
    tokens: int | None


@dataclass(frozen=True)
class Pair:
    with_skill: Arm
    without_skill: Arm


@dataclass(frozen=True)
class Score:
    pairs: int
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


def _pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def score(pairs: list[Pair], *, min_pairs: int = 3, efficiency_margin: float = 0.25,
          resamples: int = 2000, seed: int = 0) -> Score:
    rng = random.Random(seed)
    judged = [p for p in pairs if p.with_skill.success is not None and p.without_skill.success is not None]
    deltas = [float(p.with_skill.success) - float(p.without_skill.success) for p in judged]
    ratios = [math.log(p.with_skill.tokens / p.without_skill.tokens) for p in pairs
              if p.with_skill.tokens and p.without_skill.tokens]

    success_with = _mean([float(p.with_skill.success) for p in judged]) if judged else None
    success_without = _mean([float(p.without_skill.success) for p in judged]) if judged else None
    success_delta = _mean(deltas) if deltas else None
    success_ci = _bootstrap_ci(deltas, resamples, rng) if deltas else None
    token_change = math.exp(_mean(ratios)) - 1 if ratios else None
    log_ci = _bootstrap_ci(ratios, resamples, rng) if ratios else None
    token_ci = (math.exp(log_ci[0]) - 1, math.exp(log_ci[1]) - 1) if log_ci else None

    def result(verdict: str, reason: str) -> Score:
        return Score(len(pairs), success_with, success_without, success_delta, success_ci,
                     token_change, token_ci, verdict, reason)

    if len(judged) < min_pairs:
        return result(INCONCLUSIVE, f"{len(judged)} complete pair(s); at least {min_pairs} are needed")
    assert success_ci is not None and success_with is not None and success_without is not None
    if success_ci[1] < 0:
        return result(HURTS, f"success fell from {success_without:.0%} without the skill to {success_with:.0%} with it")
    if success_ci[0] > 0:
        return result(HELPS, f"success rose from {success_without:.0%} without the skill to {success_with:.0%} with it")
    if token_ci is not None and len(ratios) >= min_pairs and success_delta is not None and success_delta <= 0:
        if token_ci[0] > efficiency_margin:
            return result(HURTS, f"no gain in success, and runs used {_pct(token_change)} tokens with the skill")
    if token_ci is not None and len(ratios) >= min_pairs and success_delta is not None and success_delta >= 0:
        if token_ci[1] < -efficiency_margin:
            return result(HELPS, f"same success, and runs used {_pct(token_change)} tokens with the skill")
    return result(NEUTRAL, "no measurable difference in success or token use")
