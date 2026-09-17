"""Resumable chunked-upload sessions persisted in a local SQLite file.

A session is created by the first chunk (``Upload-Offset: 0`` plus a
declared ``Upload-Length``), grows by strict tail appends, and is sealed by
:meth:`UploadStore.complete`, which atomically flips the session from
``receiving`` to ``completed`` and freezes the verification response.

Every operation is a single ``BEGIN IMMEDIATE`` transaction, so concurrent
writers serialize on the database write lock and are judged by transaction
commit order: the first legal change wins, and every racing request is
answered from the post-commit state (the then-current offset or status).
Chunk bytes and session metadata live in the same row, so a commit always
persists — or rejects — both together.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    upload_id     TEXT PRIMARY KEY,
    upload_length INTEGER NOT NULL,
    received      INTEGER NOT NULL,
    status        TEXT NOT NULL,
    data          BLOB NOT NULL,
    response_json TEXT
);
"""

_RECEIVING = "receiving"
_COMPLETED = "completed"


class AppendOutcome(Enum):
    APPENDED = "appended"            # tail extended (or harmless empty append)
    REPLAYED = "replayed"            # byte-identical retry inside the saved range
    CONFLICT = "conflict"            # 409: out of bounds / crosses tail / total changed / content clash
    NOT_FOUND = "not_found"          # no such session and the offset was not zero
    MISSING_LENGTH = "missing_length"  # first chunk without an Upload-Length


@dataclass(frozen=True)
class AppendResult:
    outcome: AppendOutcome
    #: Current tail after the transaction (meaningless for NOT_FOUND).
    received: int = 0
    #: Human-readable reason for CONFLICT.
    reason: str = ""


class CompleteOutcome(Enum):
    COMPLETED = "completed"    # transitioned now; response freshly frozen
    ALREADY = "already"        # was completed before; same frozen response
    INCOMPLETE = "incomplete"  # bytes still missing
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class CompleteResult:
    outcome: CompleteOutcome
    #: Frozen verification response (COMPLETED / ALREADY only).
    response_json: str | None = None
    received: int = 0
    upload_length: int = 0


class UploadStore:
    """SQLite-backed session store; every public method is one transaction."""

    def __init__(self, path: str, *, busy_timeout: float = 30.0) -> None:
        self._path = path
        self._busy_timeout = busy_timeout
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        # A fresh connection per call keeps the store thread-safe; the busy
        # timeout makes a racing BEGIN IMMEDIATE wait for the holder's commit
        # instead of failing with SQLITE_BUSY.
        conn = sqlite3.connect(self._path, timeout=self._busy_timeout)
        conn.isolation_level = None  # autocommit; transactions are explicit
        if not self._initialized:
            # Schema setup happens once, under a lock: PRAGMA/DDL issued on
            # every connection would race with concurrent writers (journal
            # pragmas notably ignore the busy timeout and fail outright).
            with self._init_lock:
                if not self._initialized:
                    conn.executescript(_SCHEMA)
                    self._initialized = True
        return conn

    def _transact(self, fn):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result
        finally:
            conn.close()

    def append(
        self,
        upload_id: str,
        offset: int,
        upload_length: int | None,
        body: bytes,
    ) -> AppendResult:
        """Apply one chunk. ``upload_length`` is None when the header is absent."""

        def txn(conn: sqlite3.Connection) -> AppendResult:
            row = conn.execute(
                "SELECT upload_length, received, status, data"
                " FROM uploads WHERE upload_id = ?",
                (upload_id,),
            ).fetchone()

            if row is None:
                # Only an offset-0 chunk carrying the declared total may
                # open a session; anything else has nothing to conflict with.
                if offset != 0:
                    return AppendResult(AppendOutcome.NOT_FOUND)
                if upload_length is None:
                    return AppendResult(AppendOutcome.MISSING_LENGTH)
                if len(body) > upload_length:
                    return AppendResult(
                        AppendOutcome.CONFLICT, 0,
                        "chunk exceeds the declared Upload-Length",
                    )
                conn.execute(
                    "INSERT INTO uploads (upload_id, upload_length, received, status, data)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (upload_id, upload_length, len(body), _RECEIVING, body),
                )
                return AppendResult(AppendOutcome.APPENDED, len(body))

            stored_length, received, status, data = row

            if status == _COMPLETED:
                return AppendResult(
                    AppendOutcome.CONFLICT, received, "upload is already completed"
                )
            if upload_length is not None and upload_length != stored_length:
                return AppendResult(
                    AppendOutcome.CONFLICT, received,
                    "Upload-Length does not match the declared total",
                )

            end = offset + len(body)
            if end > stored_length:
                return AppendResult(
                    AppendOutcome.CONFLICT, received,
                    "chunk exceeds the declared Upload-Length",
                )
            if offset > received:
                return AppendResult(
                    AppendOutcome.CONFLICT, received,
                    "offset is beyond the current tail",
                )
            if offset < received:
                if end > received:
                    return AppendResult(
                        AppendOutcome.CONFLICT, received,
                        "chunk crosses the current tail",
                    )
                if data[offset:end] != body:
                    return AppendResult(
                        AppendOutcome.CONFLICT, received,
                        "chunk conflicts with the stored bytes",
                    )
                # Whole chunk already stored, byte for byte: idempotent retry.
                return AppendResult(AppendOutcome.REPLAYED, received)

            if body:
                # Concatenate in Python, not with SQL `||`: SQLite types the
                # concat operator's result as TEXT even for two BLOBs, and
                # TEXT round-trips would corrupt non-UTF-8 batch bytes.
                conn.execute(
                    "UPDATE uploads SET data = ?, received = ? WHERE upload_id = ?",
                    (bytes(data) + body, end, upload_id),
                )
            return AppendResult(AppendOutcome.APPENDED, end)

        return self._transact(txn)

    def complete(
        self,
        upload_id: str,
        finalize: Callable[[bytes], str],
    ) -> CompleteResult:
        """Seal a fully received session, freezing ``finalize(assembled bytes)``.

        ``finalize`` runs inside the same transaction as the status flip, so
        the stored response always matches the stored bytes exactly and a
        racing completer observes — and returns — the frozen result.
        """

        def txn(conn: sqlite3.Connection) -> CompleteResult:
            row = conn.execute(
                "SELECT upload_length, received, status, data, response_json"
                " FROM uploads WHERE upload_id = ?",
                (upload_id,),
            ).fetchone()
            if row is None:
                return CompleteResult(CompleteOutcome.NOT_FOUND)

            upload_length, received, status, data, response_json = row
            if status == _COMPLETED:
                return CompleteResult(
                    CompleteOutcome.ALREADY,
                    response_json=response_json,
                    received=received,
                    upload_length=upload_length,
                )
            if received != upload_length:
                return CompleteResult(
                    CompleteOutcome.INCOMPLETE,
                    received=received,
                    upload_length=upload_length,
                )

            frozen = finalize(bytes(data))
            conn.execute(
                "UPDATE uploads SET status = ?, response_json = ? WHERE upload_id = ?",
                (_COMPLETED, frozen, upload_id),
            )
            return CompleteResult(
                CompleteOutcome.COMPLETED,
                response_json=frozen,
                received=received,
                upload_length=upload_length,
            )

        return self._transact(txn)
