from skill_court.scoring import HELPS, HURTS, INCONCLUSIVE, NEUTRAL, Arm, Pair, score


def pair(with_ok, without_ok, with_tokens=1000, without_tokens=1000):
    return Pair(Arm(with_ok, with_tokens), Arm(without_ok, without_tokens))


def test_skill_that_breaks_tasks_hurts():
    s = score([pair(False, True)] * 4 + [pair(True, True)])
    assert s.verdict == HURTS
    assert s.success_with == 0.2 and s.success_without == 1.0
    assert s.success_ci[1] < 0


def test_skill_that_fixes_tasks_helps():
    assert score([pair(True, False)] * 5).verdict == HELPS


def test_same_success_but_much_more_expensive_hurts():
    s = score([pair(True, True, 2000, 1000)] * 3 + [pair(False, False, 1800, 1000)] * 2)
    assert s.verdict == HURTS
    assert 0.8 < s.token_change < 1.0


def test_same_success_and_much_cheaper_helps():
    assert score([pair(True, True, 400, 1000)] * 5).verdict == HELPS


def test_noise_is_neutral():
    s = score([pair(True, False), pair(False, True), pair(True, True, 1100, 1000), pair(True, True, 900, 1000)])
    assert s.verdict == NEUTRAL
    assert s.success_ci[0] < 0 < s.success_ci[1]


def test_too_few_pairs_is_inconclusive_even_when_one_sided():
    assert score([pair(False, True)] * 2).verdict == INCONCLUSIVE


def test_pairs_without_an_outcome_do_not_count():
    s = score([pair(None, True)] * 5 + [pair(False, True)] * 2)
    assert s.verdict == INCONCLUSIVE
    assert s.pairs == 7


def test_more_tokens_with_a_success_gain_is_not_an_efficiency_hurt():
    s = score([pair(True, False, 3000, 1000), pair(True, True, 3000, 1000), pair(True, True, 3000, 1000)])
    assert s.verdict != HURTS


def test_intervals_are_reproducible():
    pairs = [pair(True, False), pair(False, True), pair(True, True, 1500, 1000), pair(False, False, 700, 1000)]
    assert score(pairs) == score(pairs)


def test_token_change_ignores_pairs_without_an_outcome():
    s = score([pair(True, True, 1000, 1000)] * 3 + [pair(None, True, 9000, 1000)])
    assert s.verdict == NEUTRAL
    assert abs(s.token_change) < 1e-9
