import json

from skill_court.runner import RunResult, _read_usage


def usage(tmp_path, **fields):
    path = tmp_path / "usage.json"
    base = {"session_id": "local-abc", "model": "m", "api_calls": 3, "total_tokens": 1000,
            "estimated_cost_usd": 0.01, "completed": True, "failed": False, "turn_exit_reason": "text_response"}
    path.write_text(json.dumps({**base, **fields}))
    return path


def test_provider_failure_has_no_outcome(tmp_path):
    result = RunResult()
    responded = _read_usage(usage(tmp_path, completed=False, failed=True, turn_exit_reason=None,
                                  total_tokens=None), result)
    assert responded is False
    assert result.completed is None
    assert result.session_id == "local-abc"


def test_iteration_budget_counts_as_not_completed(tmp_path):
    result = RunResult()
    responded = _read_usage(usage(tmp_path, completed=False, failed=True,
                                  turn_exit_reason="max_iterations_reached(60/60)"), result)
    assert responded is True
    assert result.completed is False


def test_completed_run(tmp_path):
    result = RunResult()
    assert _read_usage(usage(tmp_path), result) is True
    assert result.completed is True and result.tokens == 1000 and result.api_calls == 3


def test_auxiliary_totals_are_preferred(tmp_path):
    result = RunResult()
    _read_usage(usage(tmp_path, total_including_auxiliary={"total_tokens": 1500, "api_calls": 4,
                                                           "estimated_cost_usd": 0.02}), result)
    assert (result.tokens, result.api_calls, result.cost_usd) == (1500, 4, 0.02)


def test_missing_report_has_no_outcome(tmp_path):
    result = RunResult()
    assert _read_usage(tmp_path / "absent.json", result) is False
    assert result.completed is None and result.session_id is None
