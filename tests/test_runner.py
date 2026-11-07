from skill_court.runner import RunResult, apply_session_row, session_containers

SESSION = "local-" + "ab12" * 16


def row(**fields):
    base = {"model": "claude-sonnet-5-5", "api_call_count": 3, "input_tokens": 4, "output_tokens": 236,
            "cache_read_tokens": 47107, "cache_write_tokens": 134, "estimated_cost_usd": 0.0074}
    return {**base, **fields}


def test_session_totals_count_cached_input():
    result = RunResult()
    assert apply_session_row(row(), result) is True
    assert result.tokens == 4 + 236 + 47107 + 134
    assert (result.api_calls, result.cost_usd, result.model) == (3, 0.0074, "claude-sonnet-5-5")


def test_a_session_the_model_never_answered_has_no_outcome():
    result = RunResult()
    failed = row(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
                 estimated_cost_usd=None, api_call_count=0)
    assert apply_session_row(failed, result) is False
    assert result.tokens is None and result.cost_usd is None


def test_container_matches_routed_and_single_profile_labels():
    profile = "court-1a2b3c4d-without"
    routed = f"profile_{profile}_{SESSION}"[:63]
    assert session_containers(profile, SESSION, [("c1", routed), ("c2", "other")]) == ["c1"]
    assert session_containers(profile, SESSION, [("c3", SESSION[:63])]) == ["c3"]


def test_label_cut_to_63_characters_keeps_runs_apart():
    profile = "court-1a2b3c4d-without"
    other = "local-" + "cd34" * 16
    labelled = [("mine", f"profile_{profile}_{SESSION}"[:63]), ("theirs", f"profile_{profile}_{other}"[:63])]
    assert session_containers(profile, SESSION, labelled) == ["mine"]


def test_ambiguous_labels_are_all_reported():
    profile = "p"
    label = f"profile_{profile}_{SESSION}"[:63]
    assert session_containers(profile, SESSION, [("a", label), ("b", label)]) == ["a", "b"]
