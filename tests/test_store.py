from skill_court import store


def test_model_specific_quarantine_hides_only_that_model():
    store.quarantine("sales-csv-totals", "qwen/qwen3-30b", "hurts on qwen")
    assert store.hidden_for("qwen/qwen3-30b") == {"sales-csv-totals": "hurts on qwen"}
    assert store.hidden_for("claude-sonnet-5-5") == {}


def test_every_model_quarantine_applies_to_all_and_specific_reason_wins():
    store.quarantine("deploy", store.ANY_MODEL, "manual")
    store.quarantine("deploy", "claude-sonnet-5-5", "trial #4")
    assert store.hidden_for("other-model") == {"deploy": "manual"}
    assert store.hidden_for("claude-sonnet-5-5") == {"deploy": "trial #4"}


def test_release_scope():
    store.quarantine("deploy", store.ANY_MODEL, "manual")
    store.quarantine("deploy", "m1", "trial")
    assert store.release("deploy", "m1") == 1
    assert store.hidden_for("m1") == {"deploy": "manual"}
    assert store.release("deploy", None) == 1
    assert store.hidden_for("m1") == {}
    assert store.release("deploy", None) == 0


def test_requarantine_updates_the_reason_instead_of_duplicating():
    store.quarantine("deploy", "m1", "first")
    store.quarantine("deploy", "m1", "second")
    assert [(s, m, r) for s, m, r, _ in store.quarantine_entries()] == [("deploy", "m1", "second")]


def test_tasks_manual_first_and_duplicate_prompts_rejected():
    assert store.add_task("s", "mined prompt one", None, "mined", "sess-1")
    assert store.add_task("s", "manual prompt", "test -f /workspace/x", "manual")
    assert not store.add_task("s", "mined prompt one", None, "mined", "sess-2")
    assert store.add_task("other", "mined prompt one", None, "mined", "sess-1")
    assert [t.origin for t in store.tasks_for("s")] == ["manual", "mined"]


def test_latest_trials_returns_newest_finished_trial_per_skill_and_model():
    old = store.start_trial("s", "h1", "m1")
    store.finish_trial(old, "m1", "hurts", "{}")
    new = store.start_trial("s", "h2", "m1")
    store.finish_trial(new, "m1", "neutral", "{}")
    store.start_trial("s", "h3", "m1")  # still running: not a verdict yet
    other = store.start_trial("s", "h2", "m2")
    store.finish_trial(other, "m2", "helps", "{}")
    assert [(t["model"], t["verdict"], t["skill_hash"]) for t in store.latest_trials()] == [
        ("m1", "neutral", "h2"),
        ("m2", "helps", "h2"),
    ]


def test_runs_are_deleted_with_their_trial(hermes_home):
    trial = store.start_trial("s", "h", "m")
    store.record_run(trial, {"task_id": 1, "arm": "with", "rep": 0, "passed": 1})
    assert len(store.runs_for(trial)) == 1
    conn = store._connect()
    with conn:
        conn.execute("DELETE FROM trials WHERE id = ?", (trial,))
    conn.close()
    assert store.runs_for(trial) == []


def test_schema_returns_after_the_database_file_is_removed(hermes_home):
    store.quarantine("deploy", "m1", "x")
    (hermes_home / "plugin-data" / store.PLUGIN / "data.db").unlink()
    assert store.hidden_for("m1") == {}
    store.quarantine("deploy", "m1", "y")
    assert store.hidden_for("m1") == {"deploy": "y"}
