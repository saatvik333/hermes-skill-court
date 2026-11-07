"""Skill Court state in the active profile's plugin-data database.

The connection is opened per call: one gateway serves several profiles, and ``plugin_db``
resolves the profile's home on every call. The schema is versioned with ``PRAGMA user_version``;
each step in ``_MIGRATIONS`` upgrades one version and keeps the data already stored.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from plugins.plugin_storage import plugin_db

PLUGIN = "skill-court"
ANY_MODEL = "*"
# The plugin-data file a running trial holds an exclusive lock on, so one profile runs one at a time.
TRIAL_LOCK = "trial.lock"

_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 1: quarantine, tasks, trials and their runs.
    (
        """CREATE TABLE quarantine (
            skill      TEXT NOT NULL,
            model      TEXT NOT NULL,
            reason     TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (skill, model)
        )""",
        """CREATE TABLE tasks (
            id             INTEGER PRIMARY KEY,
            skill          TEXT NOT NULL,
            prompt         TEXT NOT NULL,
            check_cmd      TEXT,
            origin         TEXT NOT NULL CHECK (origin IN ('mined', 'manual')),
            source_session TEXT,
            created_at     TEXT NOT NULL,
            UNIQUE (skill, prompt)
        )""",
        """CREATE TABLE trials (
            id          INTEGER PRIMARY KEY,
            skill       TEXT NOT NULL,
            skill_hash  TEXT NOT NULL,
            model       TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            verdict     TEXT,
            summary     TEXT
        )""",
        """CREATE TABLE runs (
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
        )""",
    ),
    # 2: judged outcomes, the cause of a harmful verdict, and automatic trials.
    (
        "ALTER TABLE trials ADD COLUMN auto INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE trials ADD COLUMN cause TEXT",
        "ALTER TABLE trials ADD COLUMN cause_quote TEXT",
        # One row per check-less pair: the arm the judge preferred with the with-skill result shown
        # as A (``first``) and as B (``second``), and the winner both orders agree on.
        """CREATE TABLE judgments (
            id       INTEGER PRIMARY KEY,
            trial_id INTEGER NOT NULL REFERENCES trials (id) ON DELETE CASCADE,
            task_id  INTEGER NOT NULL,
            rep      INTEGER NOT NULL,
            first    TEXT CHECK (first IN ('with', 'without', 'tie')),
            second   TEXT CHECK (second IN ('with', 'without', 'tie')),
            winner   TEXT CHECK (winner IN ('with', 'without', 'tie')),
            reasons  TEXT,
            model    TEXT,
            tokens   INTEGER,
            error    TEXT
        )""",
        """CREATE TABLE auto_settings (
            id               INTEGER PRIMARY KEY CHECK (id = 1),
            enabled          INTEGER NOT NULL,
            daily_budget_usd REAL NOT NULL,
            quiet_minutes    REAL NOT NULL
        )""",
        # A skill the agent changed in a session, waiting for (or done with) its automatic trial.
        """CREATE TABLE auto_queue (
            skill      TEXT NOT NULL,
            session_id TEXT NOT NULL,
            state      TEXT NOT NULL CHECK (state IN ('pending', 'queued', 'running', 'done')),
            changed_at TEXT NOT NULL,
            ended_at   TEXT,
            trial_id   INTEGER REFERENCES trials (id) ON DELETE SET NULL,
            note       TEXT,
            PRIMARY KEY (skill, session_id)
        )""",
        """CREATE TABLE notices (
            id           INTEGER PRIMARY KEY,
            trial_id     INTEGER REFERENCES trials (id) ON DELETE CASCADE,
            text         TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            delivered_at TEXT
        )""",
    ),
    # 3: appeals: a drafted fix of a harmful skill, its own trial, and what applying it changed.
    (
        # The task, the failed run's excerpt and the other run's answer behind a cause, as JSON.
        "ALTER TABLE trials ADD COLUMN evidence TEXT",
        # Set on the trial of an appeal's draft; such a trial is the skill's current verdict only
        # while the draft is applied.
        "ALTER TABLE trials ADD COLUMN appeal_id INTEGER",
        # ``original``: SKILL.md's bytes when drafted, which undo writes back; ``base_hash`` and
        # ``draft_hash``: the skill's content hash before and with the draft; ``trial_id``: the
        # draft's trial; ``lifted``: the quarantine reason that applying the draft removed.
        """CREATE TABLE appeals (
            id                INTEGER PRIMARY KEY,
            skill             TEXT NOT NULL,
            model             TEXT NOT NULL,
            appealed_trial_id INTEGER REFERENCES trials (id) ON DELETE SET NULL,
            base_hash         TEXT NOT NULL,
            original          BLOB NOT NULL,
            draft             TEXT NOT NULL,
            draft_hash        TEXT NOT NULL,
            diff              TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            trial_id          INTEGER REFERENCES trials (id) ON DELETE SET NULL,
            verdict           TEXT CHECK (verdict IN ('granted', 'denied')),
            reason            TEXT,
            applied_at        TEXT,
            lifted            TEXT
        )""",
    ),
)
SCHEMA_VERSION = len(_MIGRATIONS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    # The write lock is taken before the version is read again, so two processes opening an old
    # database at once apply each step once. A recreated file (plugin data deleted while Hermes
    # runs) reads 0 and gets every table back.
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for step in _MIGRATIONS[version:]:
            for statement in step:
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _connect() -> sqlite3.Connection:
    conn = plugin_db(PLUGIN)
    # One cheap pragma instead of re-running the DDL on every hook call.
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        _migrate(conn)
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

def start_trial(skill: str, skill_hash: str, model: str, auto: bool = False, appeal_id: int | None = None) -> int:
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO trials (skill, skill_hash, model, started_at, auto, appeal_id) VALUES (?, ?, ?, ?, ?, ?)",
            (skill, skill_hash, model, _now(), int(auto), appeal_id),
        )
        return int(cur.lastrowid)


def finish_trial(trial_id: int, model: str, verdict: str, summary: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE trials SET finished_at = ?, model = ?, verdict = ?, summary = ? WHERE id = ?",
            (_now(), model, verdict, summary, trial_id),
        )


def set_cause(trial_id: int, cause: str, quote: str | None, evidence: dict) -> None:
    """Why a harmful verdict happened; ``quote`` is a line of the skill's SKILL.md, verified to occur
    there; ``evidence`` is what the cause was read from (task, failed run's excerpt, other answer)."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE trials SET cause = ?, cause_quote = ?, evidence = ? WHERE id = ?",
                     (cause, quote, json.dumps(evidence), trial_id))


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


JUDGMENT_FIELDS = ("task_id", "rep", "first", "second", "winner", "reasons", "model", "tokens", "error")


def record_judgment(trial_id: int, values: dict) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            f"INSERT INTO judgments (trial_id, {', '.join(JUDGMENT_FIELDS)})"
            f" VALUES (?, {', '.join('?' for _ in JUDGMENT_FIELDS)})",
            (trial_id, *(values.get(f) for f in JUDGMENT_FIELDS)),
        )


def judgments_for(trial_id: int) -> list[dict]:
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM judgments WHERE trial_id = ? ORDER BY id", (trial_id,))]


def latest_trials(skills: Iterable[str] | None = None) -> list[dict]:
    """The newest finished trial per (skill, model). A draft's trial counts only while that draft is
    the applied skill."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT t.* FROM trials t WHERE t.id = (SELECT MAX(u.id) FROM trials u"
            " WHERE u.skill = t.skill AND u.model = t.model AND u.finished_at IS NOT NULL"
            " AND (u.appeal_id IS NULL OR u.appeal_id IN (SELECT id FROM appeals WHERE applied_at IS NOT NULL)))"
            " ORDER BY t.skill, t.model"
        ).fetchall()
    wanted = set(skills) if skills is not None else None
    return [dict(r) for r in rows if wanted is None or r["skill"] in wanted]


# --- appeals ----------------------------------------------------------------------------------

def add_appeal(skill: str, model: str, appealed_trial_id: int, base_hash: str, original: bytes,
               draft: str, draft_hash: str, diff: str) -> int:
    with closing(_connect()) as conn, conn:
        return int(conn.execute(
            "INSERT INTO appeals (skill, model, appealed_trial_id, base_hash, original, draft, draft_hash, diff,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (skill, model, appealed_trial_id, base_hash, original, draft, draft_hash, diff, _now()),
        ).lastrowid)


def decide_appeal(appeal_id: int, trial_id: int, verdict: str, reason: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE appeals SET trial_id = ?, verdict = ?, reason = ? WHERE id = ?",
                     (trial_id, verdict, reason, appeal_id))


def latest_appeal(skill: str, model: str | None = None) -> dict | None:
    """The newest appeal of ``skill`` (for ``model``, when given)."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM appeals WHERE skill = ? AND (? IS NULL OR model = ?) ORDER BY id DESC LIMIT 1",
            (skill, model, model),
        ).fetchone()
    return dict(row) if row else None


def latest_appeals() -> list[dict]:
    """The newest appeal per (skill, model)."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT a.* FROM appeals a WHERE a.id = (SELECT MAX(id) FROM appeals b"
            " WHERE b.skill = a.skill AND b.model = a.model) ORDER BY a.skill, a.model")]


def set_applied(appeal_id: int, applied: bool, lifted: str | None = None) -> None:
    """Record that the draft was written to the live skill (``lifted``: the quarantine reason that
    applying it removed), or that undo put the original back."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE appeals SET applied_at = ?, lifted = ? WHERE id = ?",
                     (_now() if applied else None, lifted, appeal_id))


# --- automatic trials -------------------------------------------------------------------------

@dataclass(frozen=True)
class AutoSettings:
    enabled: bool
    daily_budget_usd: float
    # A change counts as finished this long after the agent last touched the skill, when the
    # session that made it has not ended by then.
    quiet_minutes: float


AUTO_DEFAULTS = AutoSettings(enabled=False, daily_budget_usd=2.0, quiet_minutes=15.0)


def auto_settings() -> AutoSettings:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT enabled, daily_budget_usd, quiet_minutes FROM auto_settings").fetchone()
    return AUTO_DEFAULTS if row is None else AutoSettings(bool(row[0]), row[1], row[2])


def set_auto_settings(settings: AutoSettings) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO auto_settings (id, enabled, daily_budget_usd, quiet_minutes) VALUES (1, ?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET enabled = excluded.enabled,"
            " daily_budget_usd = excluded.daily_budget_usd, quiet_minutes = excluded.quiet_minutes",
            (int(settings.enabled), settings.daily_budget_usd, settings.quiet_minutes),
        )


def queue_change(skill: str, session_id: str) -> None:
    """The agent created or changed ``skill`` in ``session_id``: (re)start its wait for a trial.
    A change during a running trial makes that trial's result stale for this row, so it waits again."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO auto_queue (skill, session_id, state, changed_at) VALUES (?, ?, 'pending', ?)"
            " ON CONFLICT (skill, session_id) DO UPDATE SET state = 'pending', changed_at = excluded.changed_at,"
            " ended_at = NULL, trial_id = NULL, note = NULL",
            (skill, session_id, _now()),
        )


def end_session(session_id: str) -> int:
    """Changes made in ``session_id`` are complete; returns how many skills that releases."""
    with closing(_connect()) as conn, conn:
        return conn.execute(
            "UPDATE auto_queue SET ended_at = ? WHERE session_id = ? AND state = 'pending' AND ended_at IS NULL",
            (_now(), session_id),
        ).rowcount


def claim_ready(quiet_before: str) -> list[str]:
    """Mark pending changes whose session ended, or that have been quiet since ``quiet_before``
    (UTC ISO), as queued for the background runner; returns their skills."""
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            "UPDATE auto_queue SET state = 'queued' WHERE state = 'pending'"
            " AND (ended_at IS NOT NULL OR changed_at <= ?) RETURNING skill",
            (quiet_before,),
        ).fetchall()
    return sorted({skill for (skill,) in rows})


def start_queued() -> dict[str, list[str]]:
    """Claim every queued change for this runner: ``{skill: [sessions that changed it]}``."""
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            "UPDATE auto_queue SET state = 'running' WHERE state = 'queued' RETURNING skill, session_id"
        ).fetchall()
    claimed: dict[str, list[str]] = {}
    for skill, session_id in sorted(rows):
        claimed.setdefault(skill, []).append(session_id)
    return claimed


def finish_queued(skill: str, trial_id: int | None, note: str) -> None:
    """Close the running rows of ``skill``; a row the agent changed again meanwhile stays pending."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE auto_queue SET state = 'done', trial_id = ?, note = ? WHERE skill = ? AND state = 'running'",
            (trial_id, note, skill),
        )


def defer_queued(skill: str, note: str) -> None:
    """Put the running rows of ``skill`` back to wait for the next chance to run."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE auto_queue SET state = 'pending', note = ? WHERE skill = ? AND state = 'running'",
                     (note, skill))


def requeue_orphans() -> int:
    """Queue again the rows a runner claimed and never closed (it was killed). Call only while
    holding the trial lock: then no other runner can own a running row."""
    with closing(_connect()) as conn, conn:
        return conn.execute("UPDATE auto_queue SET state = 'queued' WHERE state = 'running'").rowcount


def unfinished() -> int:
    """Queued or running changes: work a background runner owns, or should."""
    with closing(_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM auto_queue WHERE state IN ('queued', 'running')").fetchone()[0]


def auto_queue_rows() -> list[dict]:
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM auto_queue ORDER BY changed_at DESC, skill")]


def auto_spent_since(since: str) -> float:
    """Provider-reported replay spend of automatic trials started at or after ``since`` (UTC ISO)."""
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(r.cost_usd), 0) FROM runs r JOIN trials t ON t.id = r.trial_id"
            " WHERE t.auto = 1 AND t.started_at >= ?",
            (since,),
        ).fetchone()[0]


def day_start(now: datetime) -> str:
    """The start of ``now``'s local calendar day, in the UTC ISO form timestamps are stored in."""
    start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def auto_spent_today(now: datetime | None = None) -> float:
    return auto_spent_since(day_start(now or datetime.now(timezone.utc)))


def add_notice(trial_id: int | None, text: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO notices (trial_id, text, created_at) VALUES (?, ?, ?)", (trial_id, text, _now()))


def claim_notices() -> list[tuple[int, str]]:
    """Take the undelivered notices, so two sessions starting a turn at once never both post one."""
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            "UPDATE notices SET delivered_at = ? WHERE delivered_at IS NULL RETURNING id, text", (_now(),)
        ).fetchall()
    return sorted(rows)


def unclaim_notice(notice_id: int) -> None:
    """A claimed notice could not be posted; leave it for the next turn."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE notices SET delivered_at = NULL WHERE id = ?", (notice_id,))


def notices() -> list[dict]:
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM notices ORDER BY id DESC")]
