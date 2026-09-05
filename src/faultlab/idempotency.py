"""Local idempotency oracle for duplicated synthetic side effects.

A retry that fires after the side effect already landed is how one message
becomes two. This module records every attempt at a side effect in a local
SQLite ledger and answers, afterwards, whether the run kept its idempotency
invariants.

The ledger has three tables. ``attempts`` holds one row per attempt with a
``commit_point`` of ``0`` (started) or ``1`` (committed), the payload
fingerprint, the stored result, the 1-based ``call`` id and whether the caller
actually received the committed result. ``commits`` holds the canonical
committed row per ``(operation, idempotency_key)`` under a UNIQUE constraint,
first write wins. ``violations`` holds what the oracle noticed while it ran.

Four violation kinds are recorded or derived:

``duplicate_commit``
    ``fn`` ran and a commit was attempted a second time for an
    ``(operation, key)`` that was already committed with the same payload.
``key_reuse_different_payload``
    The same ``(operation, key)`` was used with a different payload
    fingerprint than the one already committed.
``response_lost_after_commit``
    The commit landed but the caller did not receive the stored result,
    because an ``after_commit`` fault dropped or replaced the response.
``started_never_committed``
    An attempt recorded a begin and never reached a commit, because ``fn``
    raised (or the process died) before the commit point.

Assertion semantics
-------------------

``at-most-once`` fails on ``duplicate_commit`` and nothing else.

``no-key-reuse`` fails on ``key_reuse_different_payload`` and nothing else.

``exactly-once`` fails on:

* any ``duplicate_commit``; or
* a ``started_never_committed`` for an ``(operation, key)`` that has no commit
  at all (a started attempt that a later attempt completed is not a failure of
  exactly-once, only of that attempt); or
* a ``response_lost_after_commit`` that was never recovered, meaning no later
  attempt on the same ``(operation, key)`` delivered the committed result back
  to a caller; or
* zero commits for an ``operation`` that was named explicitly, reported as the
  assertion-only kind ``no_commit``.

So an idempotent client that loses the response to a ``drop`` fault and then
retries with the same key passes exactly-once: the retry replays the stored
result, the response is recovered, and ``fn`` never runs twice. A naive client
that retries by re-running the side effect fails, because its second commit is
a ``duplicate_commit``.

Nothing here reads the wall clock, sleeps, or stores a time.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .clock import FakeClock
from .report import dumps, jsonable

COMMIT_STARTED = 0
COMMIT_COMMITTED = 1

STATUS_STARTED = "started"
STATUS_COMMITTED = "committed"
STATUS_REPLAYED = "replayed"
STATUS_REJECTED = "rejected"

DUPLICATE_COMMIT = "duplicate_commit"
KEY_REUSE_DIFFERENT_PAYLOAD = "key_reuse_different_payload"
RESPONSE_LOST_AFTER_COMMIT = "response_lost_after_commit"
STARTED_NEVER_COMMITTED = "started_never_committed"
NO_COMMIT = "no_commit"

VIOLATION_KINDS = (
    DUPLICATE_COMMIT,
    KEY_REUSE_DIFFERENT_PAYLOAD,
    RESPONSE_LOST_AFTER_COMMIT,
    STARTED_NEVER_COMMITTED,
)

FAULT_MODES = ("drop", "replace")

#: What a ``replace`` fault hands back to the caller instead of the real result.
REPLACED_RESPONSE = {"_faultlab_replaced": True}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    "call" INTEGER NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    commit_point INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'started'
);
CREATE TABLE IF NOT EXISTS commits (
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    "call" INTEGER NOT NULL,
    commit_point INTEGER NOT NULL DEFAULT 1,
    UNIQUE (operation, idempotency_key)
);
CREATE TABLE IF NOT EXISTS violations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    operation TEXT NOT NULL,
    "call" INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_fingerprint TEXT,
    committed_fingerprint TEXT,
    attempt_seq INTEGER NOT NULL DEFAULT 0
);
"""

_TABLES = ("attempts", "commits", "violations")


class LedgerError(Exception):
    """Raised when a ledger file is missing or is not a faultlab ledger."""


class ResponseLost(TimeoutError):
    """The side effect committed but the caller never saw the result."""


class KeyReuseError(Exception):
    """An idempotency key was reused with a different payload."""

    def __init__(self, operation: str, key: str, message: str) -> None:
        self.operation = operation
        self.idempotency_key = key
        super().__init__(message)


def fingerprint(payload: Any) -> str:
    """sha256 hex of the canonical JSON encoding of ``payload``.

    Dict key order does not matter: the canonical encoding sorts keys.
    """
    return hashlib.sha256(dumps(jsonable(payload)).encode("utf-8")).hexdigest()


def _encode(value: Any) -> str:
    return dumps(jsonable(value))


def _decode(text: str | None) -> Any:
    return None if text is None else json.loads(text)


@dataclass(frozen=True)
class AttemptRecord:
    """One recorded attempt at a side effect."""

    seq: int
    call: int
    operation: str
    idempotency_key: str
    payload_fingerprint: str
    commit_point: int
    result: Any
    delivered: bool
    status: str


@dataclass(frozen=True)
class CommitRecord:
    """The canonical committed row for an ``(operation, idempotency_key)``."""

    operation: str
    idempotency_key: str
    payload_fingerprint: str
    result: Any
    call: int
    commit_point: int = COMMIT_COMMITTED


@dataclass(frozen=True)
class CommitOutcome:
    """What happened when an attempt tried to reach the commit point."""

    first: bool
    committed_fingerprint: str
    committed_result: Any
    committed_call: int


class Ledger:
    """An atomic SQLite ledger of synthetic side effects.

    One connection per thread, all against the same file, so concurrent
    attempts serialise inside SQLite rather than in Python. Writes that must
    be atomic use ``BEGIN IMMEDIATE``.
    """

    def __init__(self, path: str | os.PathLike[str], *, create: bool = True) -> None:
        self.path = os.fspath(path)
        self._local = threading.local()
        self._guard = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        if not create and not os.path.isfile(self.path):
            raise LedgerError(f"ledger not found: {self.path}")
        if create:
            self._conn().executescript(_SCHEMA)
        else:
            self._require_schema()

    @classmethod
    def open_existing(cls, path: str | os.PathLike[str]) -> "Ledger":
        """Open a ledger that must already exist. Raises LedgerError if not."""
        return cls(path, create=False)

    # -- connections ---------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                # check_same_thread=False so close() can reap a worker thread's
                # connection; each thread still gets its own, they are never shared.
                conn = sqlite3.connect(
                    self.path,
                    isolation_level=None,
                    timeout=30.0,
                    check_same_thread=False,
                )
            except sqlite3.Error as exc:  # pragma: no cover - unreadable path
                raise LedgerError(f"cannot open ledger {self.path}: {exc}") from None
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._guard:
                self._connections.append(conn)
        return conn

    def _require_schema(self) -> None:
        try:
            rows = self._conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise LedgerError(f"not a faultlab ledger: {self.path} ({exc})") from None
        names = {row["name"] for row in rows}
        missing = [table for table in _TABLES if table not in names]
        if missing:
            raise LedgerError(
                f"not a faultlab ledger: {self.path} (missing table(s): {', '.join(missing)})"
            )

    def close(self) -> None:
        """Close every connection this ledger opened."""
        with self._guard:
            connections, self._connections = self._connections, []
        for conn in connections:
            conn.close()
        self._local = threading.local()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writes --------------------------------------------------------

    def begin(self, operation: str, key: str, payload_fingerprint: str, call: int) -> int:
        """Record a started attempt. Returns its ledger sequence number."""
        cursor = self._conn().execute(
            'INSERT INTO attempts ("call", operation, idempotency_key, payload_fingerprint,'
            " commit_point, status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(call),
                operation,
                key,
                payload_fingerprint,
                COMMIT_STARTED,
                STATUS_STARTED,
            ),
        )
        return int(cursor.lastrowid or 0)

    def commit(
        self,
        seq: int,
        operation: str,
        key: str,
        payload_fingerprint: str,
        result: Any,
        call: int,
    ) -> CommitOutcome:
        """Move an attempt to the commit point, atomically.

        The first commit for an ``(operation, key)`` becomes the canonical row;
        later ones leave it untouched and come back with ``first=False``.
        """
        encoded = _encode(result)
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                'SELECT payload_fingerprint, result, "call" FROM commits'
                " WHERE operation = ? AND idempotency_key = ?",
                (operation, key),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO commits (operation, idempotency_key, payload_fingerprint,"
                    ' result, "call", commit_point) VALUES (?, ?, ?, ?, ?, ?)',
                    (
                        operation,
                        key,
                        payload_fingerprint,
                        encoded,
                        int(call),
                        COMMIT_COMMITTED,
                    ),
                )
                outcome = CommitOutcome(True, payload_fingerprint, result, int(call))
            else:
                outcome = CommitOutcome(
                    False,
                    str(row["payload_fingerprint"]),
                    _decode(row["result"]),
                    int(row["call"]),
                )
            conn.execute(
                "UPDATE attempts SET commit_point = ?, result = ?, status = ? WHERE seq = ?",
                (COMMIT_COMMITTED, encoded, STATUS_COMMITTED, int(seq)),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return outcome

    def set_status(self, seq: int, status: str) -> None:
        self._conn().execute(
            "UPDATE attempts SET status = ? WHERE seq = ?", (status, int(seq))
        )

    def mark_delivered(self, seq: int) -> None:
        """Record that the caller actually received the committed result."""
        self._conn().execute(
            "UPDATE attempts SET delivered = 1 WHERE seq = ?", (int(seq),)
        )

    def record_violation(
        self,
        kind: str,
        operation: str,
        call: int,
        key: str,
        message: str,
        *,
        payload_fingerprint: str | None = None,
        committed_fingerprint: str | None = None,
        attempt_seq: int = 0,
    ) -> None:
        self._conn().execute(
            'INSERT INTO violations (kind, operation, "call", idempotency_key, message,'
            " payload_fingerprint, committed_fingerprint, attempt_seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                operation,
                int(call),
                key,
                message,
                payload_fingerprint,
                committed_fingerprint,
                int(attempt_seq),
            ),
        )

    # -- reads ---------------------------------------------------------

    def commit_for(self, operation: str, key: str) -> CommitRecord | None:
        row = self._conn().execute(
            'SELECT operation, idempotency_key, payload_fingerprint, result, "call",'
            " commit_point FROM commits WHERE operation = ? AND idempotency_key = ?",
            (operation, key),
        ).fetchone()
        return None if row is None else _commit_record(row)

    def commits(self) -> list[CommitRecord]:
        rows = self._conn().execute(
            'SELECT operation, idempotency_key, payload_fingerprint, result, "call",'
            " commit_point FROM commits ORDER BY operation, idempotency_key"
        ).fetchall()
        return [_commit_record(row) for row in rows]

    def attempts(self) -> list[AttemptRecord]:
        rows = self._conn().execute(
            'SELECT seq, "call", operation, idempotency_key, payload_fingerprint,'
            " commit_point, result, delivered, status FROM attempts ORDER BY seq"
        ).fetchall()
        return [
            AttemptRecord(
                seq=int(row["seq"]),
                call=int(row["call"]),
                operation=str(row["operation"]),
                idempotency_key=str(row["idempotency_key"]),
                payload_fingerprint=str(row["payload_fingerprint"]),
                commit_point=int(row["commit_point"]),
                result=_decode(row["result"]),
                delivered=bool(row["delivered"]),
                status=str(row["status"]),
            )
            for row in rows
        ]

    def recorded_violations(self) -> list[tuple[dict[str, Any], int]]:
        """Recorded violations paired with the ledger seq of their attempt."""
        rows = self._conn().execute(
            'SELECT kind, operation, "call", idempotency_key, message, payload_fingerprint,'
            " committed_fingerprint, attempt_seq FROM violations ORDER BY seq"
        ).fetchall()
        out: list[tuple[dict[str, Any], int]] = []
        for row in rows:
            item: dict[str, Any] = {
                "kind": str(row["kind"]),
                "operation": str(row["operation"]),
                "call": int(row["call"]),
                "idempotency_key": str(row["idempotency_key"]),
                "message": str(row["message"]),
            }
            if row["payload_fingerprint"] is not None:
                item["payload_fingerprint"] = str(row["payload_fingerprint"])
            if row["committed_fingerprint"] is not None:
                item["committed_fingerprint"] = str(row["committed_fingerprint"])
            out.append((item, int(row["attempt_seq"])))
        return out


def _commit_record(row: sqlite3.Row) -> CommitRecord:
    return CommitRecord(
        operation=str(row["operation"]),
        idempotency_key=str(row["idempotency_key"]),
        payload_fingerprint=str(row["payload_fingerprint"]),
        result=_decode(row["result"]),
        call=int(row["call"]),
        commit_point=int(row["commit_point"]),
    )


def _sort_key(violation: dict[str, Any]) -> tuple[str, int, str, str]:
    return (
        str(violation["operation"]),
        int(violation["call"]),
        str(violation["kind"]),
        str(violation.get("idempotency_key", "")),
    )


def describe_violation(violation: dict[str, Any]) -> str:
    """One line naming the kind, the operation and the call."""
    return (
        f"{violation['kind']} operation={violation['operation']} "
        f"call={violation['call']} key={violation.get('idempotency_key', '')}: "
        f"{violation['message']}"
    )


@dataclass
class SideEffectSink:
    """A counted synthetic side effect: appending to a list.

    ``len(sink)`` is how many times the effect actually ran, which is the whole
    point of the oracle.
    """

    name: str = "outbox"
    sent: list[Any] = field(default_factory=list)

    def send(self, payload: Any) -> dict[str, Any]:
        self.sent.append(jsonable(payload))
        return {"delivery": len(self.sent), "sink": self.name, "payload": jsonable(payload)}

    def __len__(self) -> int:
        return len(self.sent)


class IdempotencyOracle:
    """Runs synthetic side effects against a ledger and reports violations.

    ``execute`` is the idempotent wrapper: it looks the key up first and
    replays a matching commit instead of running ``fn`` again. ``effect``
    always runs ``fn``, which is what a naive retry does, and is how a
    ``duplicate_commit`` gets recorded.
    """

    def __init__(
        self,
        ledger: Ledger,
        clock: FakeClock | None = None,
        *,
        tick_ms: int = 0,
    ) -> None:
        self.ledger = ledger
        self.clock = clock
        self.tick_ms = int(tick_ms)
        self._guard = threading.Lock()
        self._next_call_id = 1
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}

    # -- bookkeeping ---------------------------------------------------

    def _allocate_call(self, call: int | None) -> int:
        with self._guard:
            if self.clock is not None and self.tick_ms:
                self.clock.advance_ms(self.tick_ms)
            if call is not None:
                return int(call)
            allocated = self._next_call_id
            self._next_call_id += 1
            return allocated

    def _key_lock(self, operation: str, key: str) -> threading.Lock:
        with self._guard:
            return self._key_locks.setdefault((operation, key), threading.Lock())

    def _apply_fault(
        self,
        fault: str | None,
        seq: int,
        operation: str,
        key: str,
        call: int,
        result: Any,
    ) -> Any:
        if fault is None:
            self.ledger.mark_delivered(seq)
            return result
        if fault not in FAULT_MODES:
            raise ValueError(
                f"unknown fault {fault!r}; allowed: " + ", ".join(FAULT_MODES)
            )
        lost = "dropped" if fault == "drop" else "replaced"
        self.ledger.record_violation(
            RESPONSE_LOST_AFTER_COMMIT,
            operation,
            call,
            key,
            f"the side effect committed but the response was {lost}",
            attempt_seq=seq,
        )
        if fault == "drop":
            raise ResponseLost("response dropped after commit")
        return dict(REPLACED_RESPONSE)

    # -- running side effects ------------------------------------------

    def effect(
        self,
        operation: str,
        key: str,
        payload: Any,
        fn: Callable[[Any], Any],
        *,
        call: int | None = None,
        fault: str | None = None,
    ) -> Any:
        """Always run ``fn`` and try to commit. This is the unsafe path.

        A second commit for an already-committed ``(operation, key)`` records a
        ``duplicate_commit`` (same payload) or a
        ``key_reuse_different_payload`` (different payload). The canonical
        committed row keeps the first result either way.
        """
        call_id = self._allocate_call(call)
        payload_fp = fingerprint(payload)
        seq = self.ledger.begin(operation, key, payload_fp, call_id)
        result = fn(payload)
        outcome = self.ledger.commit(
            seq, operation, key, payload_fp, result, call_id
        )
        if not outcome.first:
            if outcome.committed_fingerprint != payload_fp:
                self.ledger.record_violation(
                    KEY_REUSE_DIFFERENT_PAYLOAD,
                    operation,
                    call_id,
                    key,
                    "idempotency key already committed with a different payload"
                    f" (committed on call {outcome.committed_call})",
                    payload_fingerprint=payload_fp,
                    committed_fingerprint=outcome.committed_fingerprint,
                    attempt_seq=seq,
                )
            else:
                self.ledger.record_violation(
                    DUPLICATE_COMMIT,
                    operation,
                    call_id,
                    key,
                    "side effect committed again for a key already committed on call"
                    f" {outcome.committed_call}",
                    payload_fingerprint=payload_fp,
                    attempt_seq=seq,
                )
        return self._apply_fault(fault, seq, operation, key, call_id, result)

    def execute(
        self,
        operation: str,
        key: str,
        payload: Any,
        fn: Callable[[Any], Any],
        *,
        call: int | None = None,
        fault: str | None = None,
    ) -> Any:
        """Idempotent wrapper: look the key up, then maybe run ``fn``.

        A matching commit replays the stored result without calling ``fn``. A
        commit under the same key with a different payload records
        ``key_reuse_different_payload`` and raises ``KeyReuseError`` without
        calling ``fn``. Concurrent attempts on one ``(operation, key)`` are
        serialised by an in-process lock, so only one of them runs ``fn``.
        """
        call_id = self._allocate_call(call)
        payload_fp = fingerprint(payload)
        with self._key_lock(operation, key):
            seq = self.ledger.begin(operation, key, payload_fp, call_id)
            existing = self.ledger.commit_for(operation, key)
            if existing is not None:
                if existing.payload_fingerprint != payload_fp:
                    self.ledger.set_status(seq, STATUS_REJECTED)
                    message = (
                        "idempotency key already committed with a different payload"
                        f" (committed on call {existing.call}); fn was not called"
                    )
                    self.ledger.record_violation(
                        KEY_REUSE_DIFFERENT_PAYLOAD,
                        operation,
                        call_id,
                        key,
                        message,
                        payload_fingerprint=payload_fp,
                        committed_fingerprint=existing.payload_fingerprint,
                        attempt_seq=seq,
                    )
                    raise KeyReuseError(operation, key, message)
                self.ledger.set_status(seq, STATUS_REPLAYED)
                self.ledger.mark_delivered(seq)
                return existing.result

            result = fn(payload)
            outcome = self.ledger.commit(
                seq, operation, key, payload_fp, result, call_id
            )
            if not outcome.first:  # pragma: no cover - the key lock prevents this
                self.ledger.record_violation(
                    DUPLICATE_COMMIT,
                    operation,
                    call_id,
                    key,
                    "side effect committed again for a key already committed on call"
                    f" {outcome.committed_call}",
                    payload_fingerprint=payload_fp,
                    attempt_seq=seq,
                )
            return self._apply_fault(fault, seq, operation, key, call_id, result)

    # -- violations ----------------------------------------------------

    def violations(self, operation: str | None = None) -> list[dict[str, Any]]:
        """Every violation in the ledger, sorted by (operation, call, kind)."""
        return [item for item, _seq in self._all(operation)]

    def _all(self, operation: str | None = None) -> list[tuple[dict[str, Any], int]]:
        found = list(self.ledger.recorded_violations())
        for attempt in self.ledger.attempts():
            if attempt.commit_point == COMMIT_STARTED and attempt.status == STATUS_STARTED:
                found.append(
                    (
                        {
                            "kind": STARTED_NEVER_COMMITTED,
                            "operation": attempt.operation,
                            "call": attempt.call,
                            "idempotency_key": attempt.idempotency_key,
                            "message": "attempt recorded a begin and never reached the commit point",
                            "payload_fingerprint": attempt.payload_fingerprint,
                        },
                        attempt.seq,
                    )
                )
        if operation is not None:
            found = [pair for pair in found if pair[0]["operation"] == operation]
        found.sort(key=lambda pair: (_sort_key(pair[0]), pair[1]))
        return found

    # -- assertions ----------------------------------------------------

    def assert_at_most_once(self, operation: str | None = None) -> list[dict[str, Any]]:
        """Violations that break at-most-once. Empty means it passed."""
        return [
            item
            for item, _seq in self._all(operation)
            if item["kind"] == DUPLICATE_COMMIT
        ]

    def assert_no_key_reuse(self, operation: str | None = None) -> list[dict[str, Any]]:
        """Violations that break no-key-reuse. Empty means it passed."""
        return [
            item
            for item, _seq in self._all(operation)
            if item["kind"] == KEY_REUSE_DIFFERENT_PAYLOAD
        ]

    def assert_exactly_once(self, operation: str | None = None) -> list[dict[str, Any]]:
        """Violations that break exactly-once. Empty means it passed.

        See the module docstring for the precise definition.
        """
        commits = {(row.operation, row.idempotency_key) for row in self.ledger.commits()}
        delivered: dict[tuple[str, str], list[int]] = {}
        for attempt in self.ledger.attempts():
            if attempt.delivered:
                delivered.setdefault(
                    (attempt.operation, attempt.idempotency_key), []
                ).append(attempt.seq)

        failing: list[dict[str, Any]] = []
        for item, seq in self._all(operation):
            kind = item["kind"]
            identity = (item["operation"], item.get("idempotency_key", ""))
            if kind == DUPLICATE_COMMIT:
                failing.append(item)
            elif kind == STARTED_NEVER_COMMITTED and identity not in commits:
                failing.append(item)
            elif kind == RESPONSE_LOST_AFTER_COMMIT:
                recovered = any(later > seq for later in delivered.get(identity, ()))
                if not recovered:
                    failing.append(item)

        if operation is not None and not any(op == operation for op, _key in commits):
            failing.append(
                {
                    "kind": NO_COMMIT,
                    "operation": operation,
                    "call": 0,
                    "idempotency_key": "",
                    "message": f"operation {operation!r} recorded no commit at all",
                }
            )
        failing.sort(key=_sort_key)
        return failing
