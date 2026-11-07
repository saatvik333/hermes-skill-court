"""Quarantine store in the active profile's plugin-data database.

The connection is opened per call: one gateway serves several profiles, and ``plugin_db``
resolves the profile's home on every call.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from plugins.plugin_storage import plugin_db

PLUGIN = "skill-court"
ANY_MODEL = "*"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quarantine (
    skill      TEXT NOT NULL,
    model      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (skill, model)
);
"""


def _connect() -> sqlite3.Connection:
    conn = plugin_db(PLUGIN)
    conn.executescript(_SCHEMA)
    return conn


def quarantine(skill: str, model: str, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO quarantine (skill, model, reason, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (skill, model) DO UPDATE SET reason = excluded.reason,"
            " created_at = excluded.created_at",
            (skill, model, reason, now),
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


def entries() -> list[tuple[str, str, str, str]]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT skill, model, reason, created_at FROM quarantine ORDER BY skill, model"
        ).fetchall()
