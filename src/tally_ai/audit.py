"""Audit log: every conversation, turn and posting attempt, in a local SQLite file.

Contains customer data; the default location (data/) is gitignored.

Tables
    conversations  one row per message thread: channel, user, outcome, final draft
    events         every agent message and user reply, in order
    postings       every attempt to post to Tally: the exact XML sent and Tally's reply
"""

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id            TEXT PRIMARY KEY,
    channel       TEXT NOT NULL,
    user          TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    message       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    result        TEXT,
    party         TEXT,
    voucher_date  TEXT,
    voucher_number TEXT,
    total         TEXT,
    remote_id     TEXT,
    draft_json    TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    at              TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'agent')),
    text            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    at              TEXT NOT NULL,
    voucher_number  TEXT,
    voucher_date    TEXT,
    party           TEXT,
    total           TEXT,
    remote_id       TEXT,
    request_xml     TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    result_json     TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS events_conversation ON events(conversation_id, id);
CREATE INDEX IF NOT EXISTS postings_conversation ON postings(conversation_id);
CREATE INDEX IF NOT EXISTS conversations_started ON conversations(started_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuditLog:
    """Thread-safe append-only audit store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._db:
            yield self._db

    # ------------------------------------------------------------ writes
    def start(self, conversation_id: str, *, channel: str, user: str | None, message: str) -> None:
        with self._write() as db:
            db.execute(
                "INSERT INTO conversations (id, channel, user, started_at, message) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, channel, user, _now(), message),
            )
            db.execute(
                "INSERT INTO events (conversation_id, at, role, text) VALUES (?, ?, 'user', ?)",
                (conversation_id, _now(), message),
            )

    def event(self, conversation_id: str, role: str, text: str) -> None:
        with self._write() as db:
            db.execute(
                "INSERT INTO events (conversation_id, at, role, text) VALUES (?, ?, ?, ?)",
                (conversation_id, _now(), role, text),
            )

    def posting(
        self,
        conversation_id: str,
        *,
        request_xml: str,
        ok: bool,
        voucher_number: str | None,
        voucher_date: str | None,
        party: str | None,
        total: str | None,
        remote_id: str | None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._write() as db:
            db.execute(
                """INSERT INTO postings (conversation_id, at, voucher_number, voucher_date, party, total,
                   remote_id, request_xml, ok, result_json, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id,
                    _now(),
                    voucher_number,
                    voucher_date,
                    party,
                    total,
                    remote_id,
                    request_xml,
                    int(ok),
                    json.dumps(result) if result is not None else None,
                    error,
                ),
            )

    def finish(
        self,
        conversation_id: str,
        *,
        status: str,
        result: str | None,
        draft: dict[str, Any] | None = None,
        party: str | None = None,
        voucher_date: str | None = None,
        voucher_number: str | None = None,
        total: str | None = None,
        remote_id: str | None = None,
    ) -> None:
        with self._write() as db:
            db.execute(
                """UPDATE conversations SET finished_at = ?, status = ?, result = ?, draft_json = ?,
                   party = ?, voucher_date = ?, voucher_number = ?, total = ?, remote_id = ?
                   WHERE id = ?""",
                (
                    _now(),
                    status,
                    result,
                    json.dumps(draft, default=str) if draft else None,
                    party,
                    voucher_date,
                    voucher_number,
                    total,
                    remote_id,
                    conversation_id,
                ),
            )

    # ------------------------------------------------------------ reads
    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return dict(row) if row else None

    def events(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT role, text, at FROM events WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        )
        return [dict(r) for r in rows]

    def postings(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        if conversation_id is None:
            rows = self._db.execute("SELECT * FROM postings ORDER BY id")
        else:
            rows = self._db.execute(
                "SELECT * FROM postings WHERE conversation_id = ? ORDER BY id", (conversation_id,)
            )
        return [dict(r) for r in rows]
