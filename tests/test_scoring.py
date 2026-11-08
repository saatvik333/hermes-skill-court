from itertools import count

from skill_court.scoring import HELPS, HURTS, INCONCLUSIVE, NEUTRAL, Arm, Pair, score, settled_early

_task_ids = count(1)


def pair(with_ok, without_ok, with_tokens=1000, without_tokens=1000, task=None):
    """One pair; each call is a new task unless ``task`` is given."""
    return Pair(task if task is not None else next(_task_ids), Arm(with_ok, with_tokens), Arm(without_ok, without_tokens))


def test_skill_that_breaks_tasks_hurts():
    s = score([pair(False, True) for _ in range(4)] + [pair(True, True)])
    assert s.verdict == HURTS
    assert (s.tasks, s.success_with, s.success_without) == (5, 0.2, 1.0)
    assert s.success_ci[1] < 0


def test_skill_that_fixes_tasks_helps():
    assert score([pair(True, False) for _ in range(3)]).verdict == HELPS


def test_reps_of_one_task_are_not_independent_evidence():
    s = score([pair(False, True, task=7) for _ in range(5)])
    assert s.verdict == INCONCLUSIVE
    assert (s.tasks, s.pairs) == (1, 5)


def test_reps_are_averaged_within_a_task():
    pairs = [pair(False, True, task=1), pair(True, True, task=1),
             pair(False, True, task=2), pair(False, True, task=2),
             pair(False, True, task=3), pair(False, True, task=3)]
    s = score(pairs)
    assert s.tasks == 3
    assert abs(s.success_delta - (-0.5 - 1 - 1) / 3) < 1e-9
    assert s.verdict == HURTS


def test_same_success_but_much_more_expensive_hurts():
    s = score([pair(True, True, 2000, 1000) for _ in range(3)] + [pair(False, False, 1800, 1000) for _ in range(2)])
    assert s.verdict == HURTS
    assert 0.8 < s.token_change < 1.0


def test_same_success_and_much_cheaper_helps():
    assert score([pair(True, True, 400, 1000) for _ in range(3)]).verdict == HELPS


def test_the_cheaper_threshold_mirrors_the_costlier_one():
    # 22% fewer tokens clears 1/1.25 (20% fewer), the mirror of costing 25% more.
    assert score([pair(True, True, 780, 1000) for _ in range(3)]).verdict == HELPS
    assert score([pair(True, True, 1220, 1000) for _ in range(3)]).verdict == NEUTRAL


def test_without_efficiency_only_success_decides_and_the_token_change_is_still_shown():
    # An answer-only trial: the run with the skill spends a call loading it, the answers tie.
    costly = [pair(True, True, 2000, 1000) for _ in range(4)]
    cheap = [pair(True, True, 400, 1000) for _ in range(3)]
    assert (score(costly).verdict, score(cheap).verdict) == (HURTS, HELPS)
    s = score(costly, efficiency=False)
    assert (s.verdict, s.reason, round(s.token_change, 2)) == (NEUTRAL, "no measurable difference in success", 1.0)
    assert score(cheap, efficiency=False).verdict == NEUTRAL
    assert score([pair(False, True) for _ in range(3)], efficiency=False).verdict == HURTS
    # The first wave settles once every pair ended the same way, however many tokens the skill cost.
    assert not settled_early(costly) and settled_early(costly, efficiency=False)
    assert not settled_early([pair(False, True)] + costly, efficiency=False)


def test_noise_is_neutral():
    s = score([pair(True, False), pair(False, True), pair(True, True, 1100, 1000), pair(True, True, 900, 1000)])
    assert s.verdict == NEUTRAL
    assert s.success_ci[0] < 0 < s.success_ci[1]


def test_too_few_tasks_is_inconclusive_even_when_one_sided():
    assert score([pair(False, True), pair(False, True)]).verdict == INCONCLUSIVE


def test_pairs_without_an_outcome_do_not_count():
    s = score([pair(None, True) for _ in range(5)] + [pair(False, True), pair(False, True)])
    assert s.verdict == INCONCLUSIVE
    assert (s.tasks, s.pairs) == (2, 2)


def test_more_tokens_with_a_success_gain_is_not_an_efficiency_hurt():
    s = score([pair(True, False, 3000, 1000), pair(True, True, 3000, 1000), pair(True, True, 3000, 1000)])
    assert s.verdict != HURTS


def test_token_change_ignores_pairs_without_an_outcome():
    s = score([pair(True, True) for _ in range(3)] + [pair(None, True, 9000, 1000)])
    assert s.verdict == NEUTRAL
    assert abs(s.token_change) < 1e-9


def test_intervals_are_reproducible():
    pairs = [pair(True, False), pair(False, True), pair(True, True, 1500, 1000), pair(False, False, 700, 1000)]
    assert score(pairs) == score(pairs)


def test_efficiency_verdict_reports_a_success_drop_it_did_not_need():
    s = score([pair(False, True, 1600, 1000), pair(False, True, 1500, 1000), pair(True, True, 1550, 1000)])
    assert s.verdict == HURTS
    assert s.reason.startswith("success went from 100% without the skill to 33% with it")
    same = score([pair(True, True, 2000, 1000) for _ in range(3)])
    assert same.reason.startswith("success was the same")
