"""Verdict-quality benchmark: Skill Court's own trial scheduler and scoring, fed simulated replays.

Every simulated skill has a known true effect, so each verdict can be graded. The replays come from
a fixed-seed model of real trials: tasks of mixed difficulty, skills that touch only some tasks,
noisy token use, and runs that end with no outcome. The trial code under test is the plugin's real
``trial.adjudicate`` and ``scoring.score``; only ``replay`` is simulated.

Output: ``METRIC`` lines. Exit 1 when the safety gate fails (harmless skills quarantined too often).
"""

from __future__ import annotations

import math
import os
import random
import sqlite3
import sys
import tempfile
import time
import types
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SEED = 20261108
SKILLS = int(os.environ.get("COURT_SIM_SKILLS", "3000"))
POOL = 8  # tasks registered per skill
NO_OUTCOME = 0.03  # chance a run ends with no outcome (provider error, timeout)
REPLAY_COST = 0.02  # loss per replay: 50 replays cost as much as one missed-harm-as-neutral
FALSE_QUARANTINE_GATE = 2.0  # percent of harmless skills quarantined; above this the run fails

HURTS, HELPS, NEUTRAL, INCONCLUSIVE = "hurts", "helps", "neutral", "inconclusive"
GRAY_NEG, GRAY_POS = "gray-", "gray+"

# loss[truth][verdict]: quarantining a harmless skill is the worst outcome, then calling harm help.
LOSS = {
    HURTS: {HURTS: 0, INCONCLUSIVE: 2, NEUTRAL: 4, HELPS: 6},
    HELPS: {HELPS: 0, INCONCLUSIVE: 1, NEUTRAL: 1, HURTS: 10},
    NEUTRAL: {NEUTRAL: 0, INCONCLUSIVE: 0.5, HELPS: 2, HURTS: 10},
    GRAY_NEG: {HURTS: 0, INCONCLUSIVE: 0.5, NEUTRAL: 0.5, HELPS: 3},
    GRAY_POS: {HELPS: 0, INCONCLUSIVE: 0.5, NEUTRAL: 0.5, HURTS: 5},
}

BASELINES = [(0.97, 0.35), (0.85, 0.25), (0.6, 0.2), (0.3, 0.15), (0.05, 0.05)]
KINDS = [("neutral", 0.40), ("harm-strong", 0.12), ("harm-weak", 0.10), ("help-strong", 0.12),
         ("help-weak", 0.10), ("bloat", 0.08), ("lean", 0.08)]


def _load_plugin(home: Path):
    """Import the plugin as package ``skill_court`` with the two Hermes modules it needs stubbed."""
    package = types.ModuleType("skill_court")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "skill-court")]
    sys.modules["skill_court"] = package
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    constants.get_default_hermes_root = lambda: home
    sys.modules["hermes_constants"] = constants

    def data_dir(name: str) -> Path:
        path = home / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = data_dir
    storage.plugin_db = lambda name, filename="data.db": sqlite3.connect(data_dir(name) / filename)
    sys.modules["plugins"] = types.ModuleType("plugins")
    sys.modules["plugins.plugin_storage"] = storage
    from skill_court import runner, store, trial
    return runner, store, trial


@dataclass
class Truth:
    label: str
    delta: float  # mean success change over the task pool
    token_mult: float


def _pick(rng: random.Random, table: list[tuple]) -> object:
    x, acc = rng.random(), 0.0
    for value, weight in table:
        acc += weight
        if x < acc:
            return value
    return table[-1][0]


def _scenario(rng: random.Random) -> tuple[list[float], list[float], list[float], float, Truth]:
    kind = _pick(rng, KINDS)
    p0 = [_pick(rng, BASELINES) for _ in range(POOL)]
    reach = rng.choice([1.0, 0.7])
    p1 = []
    for p in p0:
        hit = rng.random() < reach
        if kind == "harm-strong" and hit:
            p1.append(p * 0.1)
        elif kind == "harm-weak" and hit:
            p1.append(max(0.0, p - 0.3))
        elif kind == "help-strong" and hit:
            p1.append(p + (1 - p) * 0.9)
        elif kind == "help-weak" and hit:
            p1.append(p + (1 - p) * 0.4)
        else:
            p1.append(p)
    mult = {"bloat": 1.6, "lean": 0.6}.get(kind, 1.0) * math.exp(rng.gauss(0, 0.05))
    base_tokens = [math.exp(rng.gauss(math.log(20000), 0.6)) for _ in range(POOL)]
    delta = sum(b - a for a, b in zip(p0, p1)) / POOL
    if delta <= -0.2 or (delta <= 0 and mult >= 1.4):
        label = HURTS
    elif delta >= 0.2 or (delta >= 0 and mult <= 0.7):
        label = HELPS
    elif abs(delta) < 0.08 and 0.85 <= mult <= 1.18:
        label = NEUTRAL
    else:
        label = GRAY_NEG if (delta < 0 or (abs(delta) < 0.08 and mult > 1)) else GRAY_POS
    return p0, p1, base_tokens, mult, Truth(label, delta, mult)


def _judge(index: int) -> tuple[str, str, int]:
    """One simulated skill on trial: (truth label, verdict, replays used)."""
    runner, store, trial = _MODULES
    rng = random.Random(f"{SEED}:{index}")
    p0, p1, base_tokens, mult, truth = _scenario(rng)
    tasks = [store.Task(i, "sim", f"task {i}", "check", "manual", None) for i in range(POOL)]
    replays = Counter()

    def replay(task, arm: str):
        k = replays[(task.id, arm)]
        replays[(task.id, arm)] += 1
        draw = random.Random(f"{SEED}:{index}:{task.id}:{arm}:{k}")
        tokens = int(base_tokens[task.id] * (mult if arm == "with" else 1.0) * math.exp(draw.gauss(0, 0.35)))
        if draw.random() < NO_OUTCOME:
            return runner.RunResult(error="simulated provider error", tokens=tokens // 4, cost_usd=0.0)
        p = p1[task.id] if arm == "with" else p0[task.id]
        return runner.RunResult(session_id="s", model="sim", exit_code=0, completed=True,
                                passed=draw.random() < p, api_calls=5, tokens=tokens, cost_usd=0.0, seconds=1)

    budget = trial.Budget(float("inf"), allow_unpriced=True)
    opts = trial.Options(parallel=1)
    score, _ = trial.adjudicate(tasks, replay, opts, budget, lambda *a: None, lambda s: None)
    return truth.label, score.verdict, sum(replays.values())


_MODULES: tuple = ()


def _init_worker(home: str) -> None:
    global _MODULES
    _MODULES = _load_plugin(Path(home))


def main() -> int:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="court-sim-") as home:
        workers = max(1, (os.cpu_count() or 2) - 1)
        with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(home,)) as pool:
            results = list(pool.map(_judge, range(SKILLS), chunksize=64))

    loss = sum(LOSS[t][v] + REPLAY_COST * n for t, v, n in results) / SKILLS
    harmless = [(t, v) for t, v, _ in results if t in (HELPS, NEUTRAL, GRAY_POS)]
    harmful = [(t, v) for t, v, _ in results if t == HURTS]
    helpful = [(t, v) for t, v, _ in results if t == HELPS]
    clear = [(t, v) for t, v, _ in results if t in (HURTS, HELPS, NEUTRAL)]
    false_q = 100 * sum(v == HURTS for _, v in harmless) / len(harmless)
    missed = 100 * sum(v != HURTS for _, v in harmful) / len(harmful)
    found_help = 100 * sum(v == HELPS for _, v in helpful) / len(helpful)
    accuracy = 100 * sum(t == v for t, v in clear) / len(clear)
    inconclusive = 100 * sum(v == INCONCLUSIVE for _, v, _ in results) / SKILLS
    replays = sum(n for *_, n in results) / SKILLS

    print(f"METRIC loss={loss:.4f}")
    print(f"METRIC false_quarantine_pct={false_q:.2f}")
    print(f"METRIC missed_harm_pct={missed:.2f}")
    print(f"METRIC found_help_pct={found_help:.2f}")
    print(f"METRIC accuracy_pct={accuracy:.2f}")
    print(f"METRIC inconclusive_pct={inconclusive:.2f}")
    print(f"METRIC replays_per_skill={replays:.2f}")
    print(f"METRIC sim_seconds={time.monotonic() - started:.1f}")
    truth_mix = Counter(t for t, *_ in results)
    print("truth mix:", dict(sorted(truth_mix.items())), file=sys.stderr)
    if false_q > FALSE_QUARANTINE_GATE:
        print(f"safety gate failed: {false_q:.2f}% of harmless skills quarantined (gate {FALSE_QUARANTINE_GATE}%)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
