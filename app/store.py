"""SQLite-backed resumable upload sessions.

A session is a single row: the accumulated bytes and every piece of
metadata (declared total, current offset, status, frozen verification
response) live together, so each state change — create, append, complete —
is a single-row write inside one IMMEDIATE transaction. That provides the
two guarantees the upload protocol is built on:

* data and metadata are written atomically — a crash never persists one
  without the other;
* concurrent requests on the same upload_id are decided by transaction
  commit order — SQLite serializes IMMEDIATE transactions on the database
  write lock, so exactly one of any racing mutations wins and every loser
  observes the committed offset or status and gets a deterministic answer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    upload_id     TEXT PRIMARY KEY,
    total_length  INTEGER NOT NULL,
    offset        INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'receiving'
                  CHECK (status IN ('receiving', 'completed')),
    data          BLOB NOT NULL,
    response_json TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

_TOUCH = "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


@dataclass(frozen=True)
class PutOk:
    """The chunk was accepted: appended, created, or an identical replay.

    ``offset`` is always the committed tail after the call, so a replayed
    chunk reports the same next offset a fresh append would have.
    """

    offset: int
    total_length: int
    created: bool
    #: False when the chunk was a byte-identical replay of stored data.
    appended: bool


@dataclass(frozen=True)
class PutConflict:
    """The chunk was rejected and nothing was written.

    ``offset``/``total_length``/``status`` describe the committed session
    state observed inside the transaction (a missing session reports
    offset 0), which is exactly what the client needs to resume correctly.
    """

    reason: str
    offset: int
    total_length: int | None
    status: str  # 'receiving' | 'completed' | 'missing'


@dataclass(frozen=True)
class LengthRequired:
    """The first chunk of a new session did not declare Upload-Length."""


@dataclass(frozen=True)
class CompleteOutcome:
    """Result of attempting to finalize a session.

    kind: 'completed' (this call froze the response), 'already' (a previous
    complete committed; the frozen response is returned unchanged),
    'incomplete' (bytes still missing), 'missing' (unknown upload_id).
    """

    kind: str
    offset: int = 0
    total_length: int | None = None
    response_json: str | None = None


class UploadStore:
    """One SQLite file; every mutation is a single IMMEDIATE transaction."""

    def __init__(self, path: str) -> None:
        self._path = path
        with self._session() as conn:
            conn.execute(_SCHEMA)

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        # Autocommit mode: transactions are opened explicitly so the write
        # lock is held from BEGIN IMMEDIATE until COMMIT/ROLLBACK.
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # nothing active to roll back

    def put_chunk(
        self,
        upload_id: str,
        offset: int,
        total_length: int | None,
        body: bytes,
    ) -> PutOk | PutConflict | LengthRequired:
        """Append ``body`` at ``offset``, or accept a byte-identical replay.

        ``total_length`` is the Upload-Length header value (None when the
        client omitted it); on an existing session it must match the
        declared total.
        """
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT total_length, offset, status, data "
                    "FROM uploads WHERE upload_id = ?",
                    (upload_id,),
                ).fetchone()
                if row is None:
                    outcome: PutOk | PutConflict | LengthRequired = self._create(
                        conn, upload_id, offset, total_length, body
                    )
                else:
                    outcome = self._append_or_replay(
                        conn, upload_id, offset, total_length, body, row
                    )
                if isinstance(outcome, PutOk):
                    conn.execute("COMMIT")
                else:
                    conn.execute("ROLLBACK")
                return outcome
            except Exception:
                self._rollback(conn)
                raise

    @staticmethod
    def _create(
        conn: sqlite3.Connection,
        upload_id: str,
        offset: int,
        total_length: int | None,
        body: bytes,
    ) -> PutOk | PutConflict | LengthRequired:
        if offset != 0:
            return PutConflict(
                "the first chunk must start at offset 0", 0, None, "missing"
            )
        if total_length is None:
            return LengthRequired()
        if len(body) > total_length:
            return PutConflict(
                "chunk exceeds the declared Upload-Length", 0, total_length, "missing"
            )
        conn.execute(
            "INSERT INTO uploads (upload_id, total_length, offset, data) "
            "VALUES (?, ?, ?, ?)",
            (upload_id, total_length, len(body), body),
        )
        return PutOk(
            offset=len(body), total_length=total_length, created=True, appended=True
        )

    @staticmethod
    def _append_or_replay(
        conn: sqlite3.Connection,
        upload_id: str,
        offset: int,
        total_length: int | None,
        body: bytes,
        row: tuple[int, int, str, bytes],
    ) -> PutOk | PutConflict:
        total, current, status, data = row
        if status == "completed":
            return PutConflict(
                "upload is already completed", current, total, status
            )
        if total_length is not None and total_length != total:
            return PutConflict(
                "Upload-Length does not match the declared total",
                current,
                total,
                status,
            )
        if offset > current:
            return PutConflict(
                "chunks must append at the current offset", current, total, status
            )
        end = offset + len(body)
        if end > total:
            return PutConflict(
                "chunk exceeds the declared Upload-Length", current, total, status
            )
        if offset == current:
            # Concatenate in Python, not with SQL's || operator: || yields
            # TEXT even for BLOB operands, which would corrupt the bytes.
            conn.execute(
                f"UPDATE uploads SET data = ?, offset = ?, {_TOUCH} "
                "WHERE upload_id = ?",
                (bytes(data) + body, end, upload_id),
            )
            return PutOk(offset=end, total_length=total, created=False, appended=True)
        # offset < current: a retry window. It only succeeds when the whole
        # chunk lies inside the stored range and matches it byte for byte.
        if end <= current and bytes(data[offset:end]) == body:
            return PutOk(
                offset=current, total_length=total, created=False, appended=False
            )
        if end > current:
            return PutConflict(
                "chunk overlaps the current offset", current, total, status
            )
        return PutConflict(
            "chunk bytes conflict with the stored data", current, total, status
        )

    def complete(
        self, upload_id: str, finalize: Callable[[bytes], str]
    ) -> CompleteOutcome:
        """Freeze the session once every declared byte has been received.

        ``finalize`` turns the assembled bytes into the response body to
        persist; it runs inside the same transaction as the status flip,
        so the transition and the frozen response commit atomically. A
        session that is already completed returns its frozen response
        unchanged.
        """
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT total_length, offset, status, data, response_json "
                    "FROM uploads WHERE upload_id = ?",
                    (upload_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return CompleteOutcome(kind="missing")
                total, current, status, data, response_json = row
                if status == "completed":
                    conn.execute("ROLLBACK")
                    return CompleteOutcome("already", current, total, response_json)
                if current != total:
                    conn.execute("ROLLBACK")
                    return CompleteOutcome("incomplete", current, total)
                frozen = finalize(bytes(data))
                conn.execute(
                    "UPDATE uploads SET status = 'completed', response_json = ?, "
                    f"{_TOUCH} WHERE upload_id = ?",
                    (frozen, upload_id),
                )
                conn.execute("COMMIT")
                return CompleteOutcome("completed", current, total, frozen)
            except Exception:
                self._rollback(conn)
                raise
