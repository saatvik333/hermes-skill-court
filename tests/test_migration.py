import sqlite3

from skill_court import store

# The 1.0 schema exactly as 1.0.x created it.
V1 = """
CREATE TABLE quarantine (skill TEXT NOT NULL, model TEXT NOT NULL, reason TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (skill, model));
CREATE TABLE tasks (id INTEGER PRIMARY KEY, skill TEXT NOT NULL, prompt TEXT NOT NULL, check_cmd TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('mined', 'manual')), source_session TEXT,
    created_at TEXT NOT NULL, UNIQUE (skill, prompt));
CREATE TABLE trials (id INTEGER PRIMARY KEY, skill TEXT NOT NULL, skill_hash TEXT NOT NULL,
    model TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, verdict TEXT, summary TEXT);
CREATE TABLE runs (id INTEGER PRIMARY KEY, trial_id INTEGER NOT NULL REFERENCES trials (id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL, arm TEXT NOT NULL CHECK (arm IN ('with', 'without')), rep INTEGER NOT NULL,
    session_id TEXT, model TEXT, exit_code INTEGER, completed INTEGER, passed INTEGER, api_calls INTEGER,
    tokens INTEGER, cost_usd REAL, seconds REAL, error TEXT);
INSERT INTO quarantine VALUES ('sales-csv-totals', 'claude-sonnet-5-5', 'trial #3', '2026-11-08T00:00:00+00:00');
INSERT INTO tasks VALUES (1, 'sales-csv-totals', 'Total the export', 'test -f /workspace/total.txt', 'manual',
    NULL, '2026-11-08T00:00:00+00:00');
INSERT INTO trials VALUES (3, 'sales-csv-totals', 'h', 'claude-sonnet-5-5', '2026-11-08T00:00:00+00:00',
    '2026-11-08T00:01:00+00:00', 'hurts', '{"tasks": 3}');
INSERT INTO runs (trial_id, task_id, arm, rep, cost_usd) VALUES (3, 1, 'with', 0, 0.02);
PRAGMA user_version = 1;
"""


def test_a_1_0_database_is_upgraded_in_place_with_its_data(hermes_home):
    path = hermes_home / "plugin-data" / store.PLUGIN / "data.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(V1)

    assert store.hidden_for("claude-sonnet-5-5") == {"sales-csv-totals": "trial #3"}
    assert [t.prompt for t in store.tasks_for("sales-csv-totals")] == ["Total the export"]
    [old] = store.latest_trials()
    assert (old["id"], old["verdict"], old["auto"], old["cause"]) == (3, "hurts", 0, None)
    assert [r["cost_usd"] for r in store.runs_for(3)] == [0.02]

    store.set_cause(3, "It divides by 100.", "- amounts are in cents", {"task": "Total the export"})
    store.record_judgment(3, {"task_id": 1, "rep": 0, "first": "without", "second": "without", "winner": "without"})
    assert store.latest_trials()[0]["cause_quote"] == "- amounts are in cents"
    assert [j["winner"] for j in store.judgments_for(3)] == ["without"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_a_1_1_database_keeps_its_settings_queue_and_notices(hermes_home):
    path = hermes_home / "plugin-data" / store.PLUGIN / "data.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:  # the schema the three steps released with 1.1 create
        for step in store._MIGRATIONS[:3]:
            for statement in step:
                conn.execute(statement)
        conn.executescript("""
            INSERT INTO auto_settings VALUES (1, 1, 5.0, 10.0);
            INSERT INTO auto_queue (skill, session_id, state, changed_at) VALUES ('s', 'sess', 'queued', '2026-11-08T00:00:00+00:00');
            INSERT INTO notices (text, created_at) VALUES ('an old verdict', '2026-11-08T00:00:00+00:00');
            PRAGMA user_version = 3;
        """)

    assert store.auto_settings() == store.AutoSettings(True, 5.0, 10.0, store.AUTO_DEFAULTS.daily_judge_tokens)
    assert [(r["skill"], r["state"], r["last_attempt_at"]) for r in store.auto_queue_rows(5)] == [("s", "queued", None)]
    # A notice from before origins were recorded goes to the owner's own clients only.
    assert store.claim_notices("tg-1", "telegram", "42", local=False) == []
    assert [text for _id, text in store.claim_notices("cli-1", "cli", "", local=True)] == ["an old verdict"]
    assert store.auto_judge_tokens_today() == 0



def test_a_new_database_gets_the_whole_schema():
    store.queue_change("s", "sess")
    assert store.auto_settings() == store.AUTO_DEFAULTS
    assert store.start_trial("s", "h", "m", auto=True) == 1
