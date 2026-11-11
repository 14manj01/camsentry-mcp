"""Local memory: what each still showed, what has been delivered, the arming mode and custom checks.

One SQLite file in the user's data directory, shared by the MCP server and the standalone
watcher. It never holds credentials, image URLs or images: only analysis results.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HISTORY_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    camera_id   TEXT NOT NULL,
    time        TEXT NOT NULL,
    report      TEXT NOT NULL,
    reported_at TEXT,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS events_camera_time ON events (camera_id, time);
CREATE INDEX IF NOT EXISTS events_time ON events (time);
CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id  TEXT,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _utc_text(when: datetime) -> str:
    """Timestamps are stored as UTC ISO strings so that they sort correctly as text."""
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Rule:
    """A custom check the user asked for, e.g. "the gate is open". camera_id None means every camera."""

    id: int
    camera_id: str | None
    text: str


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock: MCP tools run on worker threads.
        self._db = sqlite3.connect(path, timeout=15, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
        if os.name != "nt":
            os.chmod(path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _read(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # -- settings ----------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        rows = self._read("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_setting(self, key: str, value: str) -> None:
        with self._write() as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- analysed stills ---------------------------------------------------

    def known_event_ids(self, event_ids: list[str]) -> set[str]:
        if not event_ids:
            return set()
        marks = ",".join("?" * len(event_ids))
        rows = self._read(f"SELECT event_id FROM events WHERE event_id IN ({marks})", tuple(event_ids))
        return {row["event_id"] for row in rows}

    def save_report(self, event_id: str, camera_id: str, when: datetime, report: dict[str, Any]) -> None:
        """Record an analysed still. Analysing it again replaces the result but keeps the delivery marks."""
        with self._write() as db:
            db.execute(
                "INSERT INTO events (event_id, camera_id, time, report) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET report = excluded.report",
                (event_id, camera_id, _utc_text(when), json.dumps(report)),
            )

    def previous_report(self, camera_id: str, before: datetime, within: timedelta) -> dict[str, Any] | None:
        """The latest still from the same camera taken before `before`, if it is recent enough to compare with."""
        rows = self._read(
            # A still the model could not analyse says nothing about what was there, so skip it.
            "SELECT report FROM events WHERE camera_id = ? AND time < ? AND time >= ? "
            "AND json_extract(report, '$.detection') IS NOT NULL ORDER BY time DESC LIMIT 1",
            (camera_id, _utc_text(before), _utc_text(before - within)),
        )
        return json.loads(rows[0]["report"]) if rows else None

    def _take(self, column: str, now: datetime, limit: int, not_before: datetime) -> tuple[list[dict[str, Any]], bool]:
        """Claim up to `limit` reports not yet delivered through `column`, oldest first.

        Claiming and marking happen in one transaction, so two processes never deliver the same
        report. Anything older than `not_before` is marked without being returned: stale news.
        """
        floor, stamp = _utc_text(not_before), _utc_text(now)
        with self._write() as db:
            db.execute(f"UPDATE events SET {column} = ? WHERE {column} IS NULL AND time < ?", (stamp, floor))
            rows = db.execute(
                f"SELECT event_id, report FROM events WHERE {column} IS NULL ORDER BY time LIMIT ?", (limit + 1,)
            ).fetchall()
            batch = rows[:limit]
            db.executemany(f"UPDATE events SET {column} = ? WHERE event_id = ?", [(stamp, row["event_id"]) for row in batch])
        return [json.loads(row["report"]) for row in batch], len(rows) > len(batch)

    def take_unreported(self, now: datetime, limit: int, not_before: datetime) -> tuple[list[dict[str, Any]], bool]:
        """Reports the chat model has not been told about yet, and whether more are waiting."""
        return self._take("reported_at", now, limit, not_before)

    def take_unnotified(self, now: datetime, limit: int, not_before: datetime) -> list[dict[str, Any]]:
        """Reports that have not been considered for a phone notification yet."""
        return self._take("notified_at", now, limit, not_before)[0]

    def release_notification(self, event_id: str) -> None:
        """Undo a claim after a failed send so that the next cycle tries again."""
        with self._write() as db:
            db.execute("UPDATE events SET notified_at = NULL WHERE event_id = ?", (event_id,))

    def reports_since(self, since: datetime, camera_id: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        """Stored reports, oldest first. With a limit, the most recent ones are kept."""
        sql, args = "SELECT report FROM events WHERE time >= ?", [_utc_text(since)]
        if camera_id:
            sql += " AND camera_id = ?"
            args.append(camera_id)
        rows = self._read(sql + " ORDER BY time DESC LIMIT ?", (*args, limit))
        return [json.loads(row["report"]) for row in reversed(rows)]

    def prune(self, now: datetime) -> None:
        with self._write() as db:
            db.execute("DELETE FROM events WHERE time < ?", (_utc_text(now - timedelta(days=HISTORY_DAYS)),))

    def forget(self) -> int:
        """Delete every stored still analysis. Returns how many were removed."""
        with self._write() as db:
            return db.execute("DELETE FROM events").rowcount

    # -- custom checks -----------------------------------------------------

    def add_rule(self, text: str, camera_id: str | None, now: datetime) -> Rule:
        with self._write() as db:
            cursor = db.execute(
                "INSERT INTO rules (camera_id, text, created_at) VALUES (?, ?, ?)", (camera_id, text, _utc_text(now))
            )
            return Rule(int(cursor.lastrowid), camera_id, text)

    def rules(self) -> list[Rule]:
        rows = self._read("SELECT id, camera_id, text FROM rules ORDER BY id")
        return [Rule(row["id"], row["camera_id"], row["text"]) for row in rows]

    def remove_rule(self, rule_id: int) -> bool:
        with self._write() as db:
            return db.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount > 0
