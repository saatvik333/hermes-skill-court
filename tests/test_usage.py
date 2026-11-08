import json
import sys
import types

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


def test_a_change_that_failed_or_only_staged_its_write_is_not_counted():
    session = [
        {"role": "user", "content": "Total this week's sales export"},
        assistant(call("a", "skill_view", name="csv-totals")),
        result("a", json.dumps({"success": True, "content": "..."})),
        assistant(call("b", "skill_manage", action="patch", name="csv-totals")),
        result("b", json.dumps({"success": False, "error": "old_string not found in SKILL.md"})),
        assistant(call("c", "skill_manage", action="edit", name="csv-totals")),
        result("c", json.dumps({"success": True, "staged": True, "pending_id": "p1", "gist": "edit csv-totals"})),
        assistant(call("d", "skill_manage", action="patch", name="csv-totals")),
        result("d", json.dumps({"success": True})),
    ]
    assert usage.aggregate([("m", session)])["csv-totals"]["m"].changes == 1


class FakeSessionDB:
    """``hermes_state.SessionDB`` over ``rows``, newest first: each session's live messages, after the
    turns an in-place compaction archived, which only an ``include_compacted`` read returns."""

    rows: list[dict] = []
    read: list[str] = []

    def __init__(self, read_only):
        assert read_only

    def list_sessions_rich(self, *, limit, offset, **_):
        return self.rows[offset:offset + limit]

    def get_messages(self, session_id, include_compacted=False):
        self.read.append(session_id)
        session = next(row for row in self.rows if row["id"] == session_id)
        return (session["archived"] if include_compacted else []) + session["live"]

    def close(self):
        pass


def test_usage_reads_compacted_turns_of_the_most_recent_sessions_only(hermes_home, monkeypatch):
    (hermes_home / "state.db").touch()
    loaded = [{"role": "user", "content": "Total this week's sales export"},
              assistant(call("a", "skill_view", name="csv-totals")), result("a", json.dumps({"success": True}))]
    later = [{"role": "user", "content": "Thanks"}, {"role": "assistant", "content": "You're welcome."}]
    monkeypatch.setattr(FakeSessionDB, "rows", [{"id": f"s{n}", "model": "m", "archived": loaded, "live": later}
                                                for n in range(usage.RECENT_SESSIONS + 1)])
    monkeypatch.setattr(FakeSessionDB, "read", [])
    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=FakeSessionDB))
    # Every session loaded the skill before a compaction archived that turn; the oldest is not read.
    assert usage.scan()["csv-totals"]["m"].sessions == usage.RECENT_SESSIONS
    assert FakeSessionDB.read == [f"s{n}" for n in range(usage.RECENT_SESSIONS)]
    assert f"{usage.RECENT_SESSIONS} most recent sessions" in usage.LABEL
