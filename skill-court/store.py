"""Skill Court state in the active profile's plugin-data database.

The connection is opened per call: one gateway serves several profiles, and ``plugin_db``
resolves the profile's home on every call.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from plugins.plugin_storage import plugin_db

PLUGIN = "skill-court"
ANY_MODEL = "*"
SCHEMA_VERSION = 1

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS quarantine (
    skill      TEXT NOT NULL,
    model      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (skill, model)
);
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY,
    skill          TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    check_cmd      TEXT,
    origin         TEXT NOT NULL CHECK (origin IN ('mined', 'manual')),
    source_session TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (skill, prompt)
);
CREATE TABLE IF NOT EXISTS trials (
    id          INTEGER PRIMARY KEY,
    skill       TEXT NOT NULL,
    skill_hash  TEXT NOT NULL,
    model       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    verdict     TEXT,
    summary     TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    trial_id    INTEGER NOT NULL REFERENCES trials (id) ON DELETE CASCADE,
    task_id     INTEGER NOT NULL,
    arm         TEXT NOT NULL CHECK (arm IN ('with', 'without')),
    rep         INTEGER NOT NULL,
    session_id  TEXT,
    model       TEXT,
    exit_code   INTEGER,
    completed   INTEGER,
    passed      INTEGER,
    api_calls   INTEGER,
    tokens      INTEGER,
    cost_usd    REAL,
    seconds     REAL,
    error       TEXT
);
PRAGMA user_version = {SCHEMA_VERSION};
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = plugin_db(PLUGIN)
    # One cheap pragma instead of re-running the DDL on every hook call; a recreated file
    # (plugin data deleted while Hermes runs) reads 0 and gets its tables back.
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
    return conn


# --- quarantine -------------------------------------------------------------------------------

def quarantine(skill: str, model: str, reason: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO quarantine (skill, model, reason, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (skill, model) DO UPDATE SET reason = excluded.reason,"
            " created_at = excluded.created_at",
            (skill, model, reason, _now()),
        )


def release(skill: str, model: str | None) -> int:
    """Lift a quarantine for one model, or for every model when ``model`` is None."""
    with closing(_connect()) as conn, conn:
        if model is None:
            cur = conn.execute("DELETE FROM quarantine WHERE skill = ?", (skill,))
        else:
            cur = conn.execute("DELETE FROM quarantine WHERE skill = ? AND model = ?", (skill, model))
        return cur.rowcount


def hidden_for(model: str) -> dict[str, str]:
    """``{skill: reason}`` quarantined for ``model`` (rows for that model or for every model)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT skill, reason FROM quarantine WHERE model IN (?, ?) ORDER BY model = ?",
            (model, ANY_MODEL, model),
        ).fetchall()
    # Model-specific rows sort last, so their reason wins over a model-wide row.
    return dict(rows)


def quarantine_entries() -> list[tuple[str, str, str, str]]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT skill, model, reason, created_at FROM quarantine ORDER BY skill, model"
        ).fetchall()


# --- tasks ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    id: int
    skill: str
    prompt: str
    check_cmd: str | None
    origin: str
    source_session: str | None


def add_task(skill: str, prompt: str, check_cmd: str | None, origin: str, source_session: str | None = None) -> bool:
    """Insert a task; False when the skill already has a task with this prompt."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tasks (skill, prompt, check_cmd, origin, source_session, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (skill, prompt, check_cmd, origin, source_session, _now()),
        )
        return cur.rowcount == 1


def tasks_for(skill: str) -> list[Task]:
    """Manual tasks (they carry checks) first, then mined ones, newest first within each."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, skill, prompt, check_cmd, origin, source_session FROM tasks WHERE skill = ?"
            " ORDER BY origin = 'mined', id DESC",
            (skill,),
        ).fetchall()
    return [Task(*row) for row in rows]


def remove_task(task_id: int) -> bool:
    with closing(_connect()) as conn, conn:
        return conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount == 1


# --- trials and runs --------------------------------------------------------------------------

def start_trial(skill: str, skill_hash: str, model: str) -> int:
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO trials (skill, skill_hash, model, started_at) VALUES (?, ?, ?, ?)",
            (skill, skill_hash, model, _now()),
        )
        return int(cur.lastrowid)


def finish_trial(trial_id: int, model: str, verdict: str, summary: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE trials SET finished_at = ?, model = ?, verdict = ?, summary = ? WHERE id = ?",
            (_now(), model, verdict, summary, trial_id),
        )


RUN_FIELDS = ("task_id", "arm", "rep", "session_id", "model", "exit_code", "completed", "passed",
              "api_calls", "tokens", "cost_usd", "seconds", "error")


def record_run(trial_id: int, values: dict) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO runs (trial_id, {', '.join(RUN_FIELDS)})"
            f" VALUES (?, {', '.join('?' for _ in RUN_FIELDS)})",
            (trial_id, *(values.get(f) for f in RUN_FIELDS)),
        )


def runs_for(trial_id: int) -> list[dict]:
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM runs WHERE trial_id = ? ORDER BY id", (trial_id,))]


def latest_trials(skills: Iterable[str] | None = None) -> list[dict]:
    """The newest finished trial per (skill, model)."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT t.* FROM trials t WHERE t.finished_at IS NOT NULL AND t.id = ("
            " SELECT MAX(id) FROM trials u WHERE u.skill = t.skill AND u.model = t.model"
            " AND u.finished_at IS NOT NULL) ORDER BY t.skill, t.model"
        ).fetchall()
    wanted = set(skills) if skills is not None else None
    return [dict(r) for r in rows if wanted is None or r["skill"] in wanted]
