import json

from skill_court import usage


def call(call_id, tool, **args):
    return {"id": call_id, "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}


def assistant(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def result(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


SESSION = [
    {"role": "user", "content": "List the files"},
    assistant(call("a", "terminal", command="ls")),
    result("a", json.dumps({"output": "", "exit_code": 2})),  # a failure before the skill loads
    assistant(call("b", "skill_manage", action="patch", name="csv-totals")),  # a change before it loads
    result("b", json.dumps({"success": True})),
    {"role": "user", "content": "Total this week's sales export"},
    assistant(call("c", "skill_view", name="demo/csv-totals"), call("d", "skill_view", name="json-format")),
    result("c", json.dumps({"success": True, "content": "..."})),
    result("d", json.dumps({"success": False, "error": "Skill 'json-format' is quarantined"})),
    assistant(call("e", "terminal", command="python total.py")),
    result("e", "Error executing tool 'terminal': timed out after 60.0s"),
    {"role": "user", "content": "Fix the skill so it does not assume cents"},
    assistant(call("f", "skill_manage", operations=[{"action": "patch", "name": "csv-totals"},
                                                   {"action": "write_file", "name": "csv-totals"}])),
    result("f", json.dumps({"success": True})),
    assistant(call("g", "skill_view", name="csv-totals", file_path="references/units.md")),  # a linked file
    result("g", json.dumps({"success": True})),
]


def test_usage_counts_loads_tool_failures_around_them_and_changes_after_them():
    found = usage.aggregate([("claude-sonnet-5-5", SESSION)])
    # A quarantined skill_view loaded nothing.
    assert list(found) == ["csv-totals"]
    assert found["csv-totals"]["claude-sonnet-5-5"] == usage.Usage(
        sessions=1, loads=1, calls_after=5, failed_after=2, calls_before=2, failed_before=1, changes=2)


def test_usage_adds_up_per_model_and_says_it_is_not_a_verdict():
    other = [{"role": "user", "content": "Total March"}, assistant(call("x", "skill_view", name="csv-totals")),
             result("x", json.dumps({"success": True}))]
    found = usage.aggregate([("claude-sonnet-5-5", SESSION), ("claude-haiku-5-5", other), (None, other),
                             ("claude-haiku-5-5", other)], frozenset({"csv-totals"}))
    by_model = found["csv-totals"]
    assert {model: u.sessions for model, u in by_model.items()} == {
        "claude-sonnet-5-5": 1, "claude-haiku-5-5": 2, usage.UNKNOWN_MODEL: 1}
    assert usage.describe(by_model) == (
        "loaded in 4 turns of 4 sessions (2 on claude-haiku-5-5, 1 on an unrecorded model, 1 on claude-sonnet-5-5); "
        "2 of 8 tool calls failed from the turn it loaded in on, 1 of 2 before it; changed 2 times after it loaded")
    assert "not a verdict" in usage.LABEL
