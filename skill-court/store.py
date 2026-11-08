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
# How the reason of a quarantine a trial set starts: ``Skill Court trial #<id>: <why>``.
COURT_REASON = "Skill Court trial #"

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
    # 4: who a notice is for, retry backoff for automatic trials, and the judge's daily token cap.
    (
        # The session whose change led to the notice, its platform (``local`` for the profile
        # owner's own clients) and its sender; a NULL sender means the profile owner.
        "ALTER TABLE notices ADD COLUMN session_id TEXT",
        "ALTER TABLE notices ADD COLUMN platform TEXT",
        "ALTER TABLE notices ADD COLUMN sender TEXT",
        # When a background runner was last started for the row.
        "ALTER TABLE auto_queue ADD COLUMN last_attempt_at TEXT",
        # NULL: the default cap.
        "ALTER TABLE auto_settings ADD COLUMN daily_judge_tokens INTEGER",
        # Every call to the judge (comparisons, causes, drafts), with what the host reported it used.
        """CREATE TABLE judge_calls (
            id       INTEGER PRIMARY KEY,
            at       TEXT NOT NULL,
            auto     INTEGER NOT NULL,
            purpose  TEXT NOT NULL,
            model    TEXT,
            tokens   INTEGER NOT NULL,
            cost_usd REAL
        )""",
    ),
    # 5: the chat a notice's session ran in, so a verdict from a direct chat never reaches a group.
    (
        "ALTER TABLE notices ADD COLUMN chat_id TEXT",
    ),
    # 6: answer-only trials, re-checks on the model a skill was used on, and the first-run notice.
    (
        # ``sandbox``: replays in Docker sandboxes, checks run there. ``answer-only``: no sandbox, only
        # the skills, todo and web tools, every pair judged on the final answers.
        "ALTER TABLE trials ADD COLUMN mode TEXT NOT NULL DEFAULT 'sandbox'",
        # The queue gains the model its trial runs on: '' for a change (the profile's model), or the
        # model a session loaded the skill on (a re-check), with that session's provider. The primary
        # key changes, so the table is rebuilt with its rows.
        """CREATE TABLE auto_queue_6 (
            skill           TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            model           TEXT NOT NULL DEFAULT '',
            provider        TEXT,
            state           TEXT NOT NULL CHECK (state IN ('pending', 'queued', 'running', 'done')),
            changed_at      TEXT NOT NULL,
            ended_at        TEXT,
            trial_id        INTEGER REFERENCES trials (id) ON DELETE SET NULL,
            note            TEXT,
            last_attempt_at TEXT,
            PRIMARY KEY (skill, session_id, model)
        )""",
        "INSERT INTO auto_queue_6 (skill, session_id, state, changed_at, ended_at, trial_id, note, last_attempt_at)"
        " SELECT skill, session_id, state, changed_at, ended_at, trial_id, note, last_attempt_at FROM auto_queue",
        "DROP TABLE auto_queue",
        "ALTER TABLE auto_queue_6 RENAME TO auto_queue",
        # The notice that told the owner automatic trials are on by default: queued once per profile.
        """CREATE TABLE first_run (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            notice_id  INTEGER NOT NULL REFERENCES notices (id),
            created_at TEXT NOT NULL
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


def release_court_quarantine(skill: str, model: str) -> bool:
    """Lift ``skill``'s quarantine for ``model`` if a trial set it; one set by hand stays."""
    with closing(_connect()) as conn, conn:
        return conn.execute(
            "DELETE FROM quarantine WHERE skill = ? AND model = ? AND substr(reason, 1, ?) = ?",
            (skill, model, len(COURT_REASON), COURT_REASON),
        ).rowcount == 1


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

def start_trial(skill: str, skill_hash: str, model: str, auto: bool = False, appeal_id: int | None = None,
                mode: str = "sandbox") -> int:
    """``mode``: how the replays run (``arms.SANDBOX`` or ``arms.ANSWER_ONLY``)."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO trials (skill, skill_hash, model, started_at, auto, appeal_id, mode)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (skill, skill_hash, model, _now(), int(auto), appeal_id, mode),
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


def has_verdict(skill: str, skill_hash: str, model: str) -> bool:
    """Whether a finished trial judged this version of ``skill`` (its content hash) on ``model``. An
    inconclusive trial that today's budget or judge tokens cut short is not one; it runs again."""
    with closing(_connect()) as conn:
        return bool(conn.execute(
            "SELECT EXISTS (SELECT 1 FROM trials WHERE skill = ? AND skill_hash = ? AND model = ?"
            " AND finished_at IS NOT NULL"
            " AND (verdict != 'inconclusive' OR COALESCE(json_extract(summary, '$.stopped'), '') != 'budget'))",
            (skill, skill_hash, model)).fetchone()[0])


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


def latest_appeal(skill: str, model: str | None = None, applied: bool = False) -> dict | None:
    """The newest appeal of ``skill`` (for ``model``, when given; only an applied one with ``applied``)."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM appeals WHERE skill = ? AND (? IS NULL OR model = ?)"
            " AND (NOT ? OR applied_at IS NOT NULL) ORDER BY id DESC LIMIT 1",
            (skill, model, model, applied),
        ).fetchone()
    return dict(row) if row else None


def latest_appeals() -> list[dict]:
    """The newest appeal per (skill, model)."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT a.* FROM appeals a WHERE a.id = (SELECT MAX(id) FROM appeals b"
            " WHERE b.skill = a.skill AND b.model = a.model) ORDER BY a.skill, a.model")]


def claim_apply(appeal_id: int) -> bool:
    """Mark the draft as written to the live skill; False when it already is, so two processes
    never both write it."""
    with closing(_connect()) as conn, conn:
        return conn.execute("UPDATE appeals SET applied_at = ?, lifted = NULL WHERE id = ? AND applied_at IS NULL",
                            (_now(), appeal_id)).rowcount == 1


def release_apply(appeal_id: int) -> bool:
    """Mark the draft as no longer written to the live skill; False when it was not."""
    with closing(_connect()) as conn, conn:
        return conn.execute("UPDATE appeals SET applied_at = NULL, lifted = NULL WHERE id = ? AND applied_at IS NOT NULL",
                            (appeal_id,)).rowcount == 1


def set_lifted(appeal_id: int, lifted: str) -> None:
    """The quarantine reason applying the draft removed, which undo restores."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE appeals SET lifted = ? WHERE id = ?", (lifted, appeal_id))


# --- automatic trials -------------------------------------------------------------------------

@dataclass(frozen=True)
class AutoSettings:
    enabled: bool
    daily_budget_usd: float
    # A change counts as finished this long after the agent last touched the skill, when the
    # session that made it has not ended by then.
    quiet_minutes: float
    # Tokens a day the judge may use in automatic trials (comparisons, causes and drafts).
    daily_judge_tokens: int


# A new install tests skills automatically on a $1.00 daily replay budget; ``auto off`` or ``auto on``
# stores a row.
AUTO_DEFAULTS = AutoSettings(enabled=True, daily_budget_usd=1.0, quiet_minutes=15.0, daily_judge_tokens=300_000)


def auto_settings() -> AutoSettings:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT enabled, daily_budget_usd, quiet_minutes, daily_judge_tokens FROM auto_settings").fetchone()
    if row is None:
        return AUTO_DEFAULTS
    return AutoSettings(bool(row[0]), row[1], row[2], AUTO_DEFAULTS.daily_judge_tokens if row[3] is None else row[3])


def set_auto_settings(settings: AutoSettings) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO auto_settings (id, enabled, daily_budget_usd, quiet_minutes, daily_judge_tokens)"
            " VALUES (1, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET enabled = excluded.enabled,"
            " daily_budget_usd = excluded.daily_budget_usd, quiet_minutes = excluded.quiet_minutes,"
            " daily_judge_tokens = excluded.daily_judge_tokens",
            (int(settings.enabled), settings.daily_budget_usd, settings.quiet_minutes, settings.daily_judge_tokens),
        )


def queue_change(skill: str, session_id: str) -> None:
    """The agent created or changed ``skill`` in ``session_id``: (re)start its wait for a trial on the
    profile's model. A change during a running trial makes that trial's result stale for this row, so
    it waits again; a new change is tried as soon as it is ready, whenever an earlier one was last
    attempted."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO auto_queue (skill, session_id, state, changed_at) VALUES (?, ?, 'pending', ?)"
            " ON CONFLICT (skill, session_id, model) DO UPDATE SET state = 'pending',"
            " changed_at = excluded.changed_at, ended_at = NULL, trial_id = NULL, note = NULL, last_attempt_at = NULL",
            (skill, session_id, _now()),
        )


def queue_recheck(skill: str, session_id: str, model: str, provider: str | None) -> bool:
    """``session_id`` loaded ``skill`` on ``model``, which has no verdict on its current version: it
    waits like a change for that session to end or go quiet, then is tried on ``model`` through
    ``provider``. One row per session and model, never reset, so loading the skill again in that
    session queues nothing new; False when the row exists."""
    with closing(_connect()) as conn, conn:
        return conn.execute(
            "INSERT INTO auto_queue (skill, session_id, model, provider, state, changed_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?) ON CONFLICT DO NOTHING",
            (skill, session_id, model, provider, _now()),
        ).rowcount == 1


def end_session(session_id: str) -> int:
    """Changes made in ``session_id`` are complete; returns how many skills that releases."""
    with closing(_connect()) as conn, conn:
        return conn.execute(
            "UPDATE auto_queue SET ended_at = ? WHERE session_id = ? AND state = 'pending' AND ended_at IS NULL",
            (_now(), session_id),
        ).rowcount


def claim_ready(quiet_before: str, retry_before: str) -> list[str]:
    """Mark pending changes whose session ended, or that have been quiet since ``quiet_before``,
    as queued for the background runner, except those a runner was started for after
    ``retry_before`` (both UTC ISO); returns their skills."""
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            "UPDATE auto_queue SET state = 'queued' WHERE state = 'pending'"
            " AND (ended_at IS NOT NULL OR changed_at <= ?)"
            " AND (last_attempt_at IS NULL OR last_attempt_at <= ?) RETURNING skill",
            (quiet_before, retry_before),
        ).fetchall()
    return sorted({skill for (skill,) in rows})


@dataclass(frozen=True)
class Queued:
    """The queued rows of one skill and model that a runner took: one trial on ``model`` ('' for a
    change, which runs on the profile's model) through ``provider``."""
    skill: str
    model: str
    provider: str | None
    sessions: list[str]  # the sessions that changed or loaded it


def start_queued() -> list[Queued]:
    """Claim every queued row for this runner, one ``Queued`` per skill and model."""
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            "UPDATE auto_queue SET state = 'running' WHERE state = 'queued' RETURNING skill, model, session_id, provider"
        ).fetchall()
    claimed: dict[tuple[str, str], Queued] = {}
    for skill, model, session_id, provider in sorted(rows, key=lambda row: row[:3]):
        found = claimed.setdefault((skill, model), Queued(skill, model, provider, []))
        found.sessions.append(session_id)
    return list(claimed.values())


def finish_queued(skill: str, model: str, trial_id: int | None, note: str) -> int:
    """Close the running rows of ``skill`` on ``model``; a row the agent changed again meanwhile stays
    pending. Returns how many rows it closed."""
    with closing(_connect()) as conn, conn:
        return conn.execute(
            "UPDATE auto_queue SET state = 'done', trial_id = ?, note = ? WHERE skill = ? AND model = ?"
            " AND state = 'running'", (trial_id, note, skill, model),
        ).rowcount


def defer_queued(skill: str, model: str, note: str) -> None:
    """Put the running rows of ``skill`` on ``model`` back to wait for the next chance to run."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE auto_queue SET state = 'pending', note = ? WHERE skill = ? AND model = ?"
                     " AND state = 'running'", (note, skill, model))


def requeue_orphans() -> int:
    """Queue again the rows a runner claimed and never closed (it was killed). Call only while
    holding the trial lock: then no other runner can own a running row."""
    with closing(_connect()) as conn, conn:
        return conn.execute("UPDATE auto_queue SET state = 'queued' WHERE state = 'running'").rowcount


def unfinished(attempted_before: str) -> int:
    """Queued or running changes, work a background runner owns or should, whose last runner was
    started before ``attempted_before`` (UTC ISO), or never."""
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM auto_queue WHERE state IN ('queued', 'running')"
            " AND (last_attempt_at IS NULL OR last_attempt_at <= ?)",
            (attempted_before,),
        ).fetchone()[0]


def mark_attempt(at: str, note: str | None = None) -> None:
    """A background runner was started (or, with ``note``, failed to start) at ``at`` for every
    queued or running change."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE auto_queue SET last_attempt_at = ?, note = COALESCE(?, note)"
                     " WHERE state IN ('queued', 'running')", (at, note))


def auto_queue_rows(limit: int) -> list[dict]:
    """The ``limit`` most recently changed rows."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM auto_queue ORDER BY changed_at DESC, skill LIMIT ?",
                                              (limit,))]


def record_judge_call(purpose: str, model: str, tokens: int, cost_usd: float | None, auto: bool) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO judge_calls (at, auto, purpose, model, tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
                     (_now(), int(auto), purpose, model, tokens, cost_usd))


def auto_spent_since(since: str) -> float:
    """Provider-reported spend of automatic trials since ``since`` (UTC ISO): the replays of trials
    started since then, and the judge calls made since then that the host priced."""
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT (SELECT COALESCE(SUM(r.cost_usd), 0) FROM runs r JOIN trials t ON t.id = r.trial_id"
            " WHERE t.auto = 1 AND t.started_at >= ?)"
            " + (SELECT COALESCE(SUM(cost_usd), 0) FROM judge_calls WHERE auto = 1 AND at >= ?)",
            (since, since),
        ).fetchone()[0]


def auto_judge_tokens_since(since: str) -> int:
    """Tokens the judge used in automatic trials since ``since`` (UTC ISO)."""
    with closing(_connect()) as conn:
        return conn.execute("SELECT COALESCE(SUM(tokens), 0) FROM judge_calls WHERE auto = 1 AND at >= ?",
                            (since,)).fetchone()[0]


def day_start(now: datetime) -> str:
    """The start of ``now``'s local calendar day, in the UTC ISO form timestamps are stored in."""
    start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def auto_spent_today(now: datetime | None = None) -> float:
    return auto_spent_since(day_start(now or datetime.now(timezone.utc)))


def auto_judge_tokens_today(now: datetime | None = None) -> int:
    return auto_judge_tokens_since(day_start(now or datetime.now(timezone.utc)))


# The platform of a notice for a session on one of the profile owner's own clients.
LOCAL = "local"


@dataclass(frozen=True)
class Origin:
    """Who a notice is for: the session whose change it is about, and that session's platform
    (``LOCAL`` for the owner's own clients, None when unknown), sender and chat. A None sender means
    the profile owner, the only one a notice is posted to besides a known sender in a known chat."""
    session_id: str | None
    platform: str | None
    sender: str | None
    chat_id: str | None = None


def _insert_notice(conn: sqlite3.Connection, trial_id: int | None, text: str, origin: Origin) -> int:
    return int(conn.execute(
        "INSERT INTO notices (trial_id, text, created_at, session_id, platform, sender, chat_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (trial_id, text, _now(), origin.session_id, origin.platform, origin.sender, origin.chat_id)).lastrowid)


def add_notice(trial_id: int | None, text: str, origin: Origin) -> None:
    with closing(_connect()) as conn, conn:
        _insert_notice(conn, trial_id, text, origin)


# Due while no first-run notice was queued and automatic trials run on their defaults: once
# ``auto on`` or ``auto off`` stored settings, the user has chosen and is not told again.
_FIRST_RUN_DUE = "SELECT NOT EXISTS (SELECT 1 FROM first_run) AND NOT EXISTS (SELECT 1 FROM auto_settings)"


def first_run_due() -> bool:
    with closing(_connect()) as conn:
        return bool(conn.execute(_FIRST_RUN_DUE).fetchone()[0])


def queue_first_run_notice(text: str, audience: list[Origin]) -> bool:
    """Queue the first-run notice, one copy per origin in ``audience``, if it is still due; False
    otherwise. The write lock is taken before the check is repeated, so two sessions starting at once
    queue it once."""
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(_FIRST_RUN_DUE).fetchone()[0]:
            return False
        notice_ids = [_insert_notice(conn, None, text, origin) for origin in audience]
        conn.execute("INSERT INTO first_run (id, notice_id, created_at) VALUES (1, ?, ?)", (notice_ids[0], _now()))
        return True


def has_notices_for(platform: str, sender: str) -> bool:
    """Whether an undelivered notice waits for ``sender`` on ``platform``."""
    with closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM notices WHERE delivered_at IS NULL AND platform = ? AND sender = ? LIMIT 1",
                            (platform, sender)).fetchone() is not None


def claim_notices(session_id: str, platform: str, sender: str, chat_id: str | None,
                  local: bool) -> list[tuple[int, str]]:
    """Take the undelivered notices for a turn of ``session_id`` in ``chat_id`` (``local``: on one
    of the owner's own clients): those about that session, those for the same sender in the same
    chat on the same platform, and on a local client those for the owner. Taking them first means
    two sessions starting a turn at once never both post one."""
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            "UPDATE notices SET delivered_at = ? WHERE delivered_at IS NULL AND ("
            " (? != '' AND session_id = ?)"
            " OR (NOT ? AND ? != '' AND ? IS NOT NULL AND platform = ? AND sender = ? AND chat_id = ?)"
            " OR (? AND sender IS NULL)) RETURNING id, text",
            (_now(), session_id, session_id, local, sender, chat_id, platform, sender, chat_id, local),
        ).fetchall()
    return sorted(rows)


def unclaim_notice(notice_id: int) -> None:
    """A claimed notice could not be posted; leave it for the next turn."""
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE notices SET delivered_at = NULL WHERE id = ?", (notice_id,))


def notices(limit: int) -> list[dict]:
    """The ``limit`` newest notices."""
    with closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM notices ORDER BY id DESC LIMIT ?", (limit,))]
