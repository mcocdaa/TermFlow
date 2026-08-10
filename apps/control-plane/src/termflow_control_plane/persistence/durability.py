"""SQLite durability contract for the Agent Broker control plane (§17, §20).

Crash-safety claims must name the exact SQLite durability level rather than
relying on process-restart tests alone, which do not prove power-loss durability.

Contract:

- **Power-loss durability:** ``PRAGMA synchronous=FULL`` — a committed
  transaction has its data flushed to disk before ``COMMIT`` returns. No
  alternative power-loss window is accepted in the default contract; any
  deployment choosing a weaker level must document that explicit loss window.
- **Busy/transaction locking:** a non-zero ``busy_timeout`` (single-writer
  behavior, no unchecked ``database is locked`` failures) plus
  ``foreign_keys=ON`` for referential enforcement.
- **WAL mode + checkpoint/backup policy:** ``journal_mode=WAL`` for concurrent
  readers with a single writer; checkpoints/backups are part of the retention
  and recovery policy, and row deletion is logical rather than guaranteed
  physical erasure (see §16.1 backups/WAL window).
- **Recovery:** ``PRAGMA integrity_check`` runs on recovery; a non-``ok``
  result (or a malformed-database error) is a failed check that must be handled
  before the database is trusted again.

``apps/control-plane/src/termflow_control_plane/persistence/database.py``
already applies ``journal_mode=WAL`` and ``foreign_keys=ON`` during
initialization; this module keeps the full contract executable and standalone
(asserted by ``apps/control-plane/tests/test_durability.py``) without changing
runtime behavior.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

import aiosqlite

#: Single-writer busy timeout in milliseconds.
DEFAULT_BUSY_TIMEOUT_MS = 5000


def durability_pragma_sql(*, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> tuple[str, ...]:
    """Return the ordered PRAGMA statements that apply the durability contract.

    ``synchronous=FULL`` and ``journal_mode=WAL`` are non-negotiable parts of the
    contract; ``busy_timeout`` and ``foreign_keys`` are configurable in policy
    but always present.
    """
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be non-negative")
    return (
        "PRAGMA synchronous=FULL",
        "PRAGMA journal_mode=WAL",
        f"PRAGMA busy_timeout={busy_timeout_ms}",
        "PRAGMA foreign_keys=ON",
    )


class ExecutesPragma(Protocol):
    """Minimal protocol shared by ``sqlite3.Connection`` and aiosqlite connections."""

    def execute(self, sql: str, parameters: object = ...) -> object: ...


def apply_durability_pragmas(
    connection: ExecutesPragma,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> None:
    """Apply the durability PRAGMAs to a sqlite3 or aiosqlite connection."""
    for statement in durability_pragma_sql(busy_timeout_ms=busy_timeout_ms):
        connection.execute(statement)


@dataclass(frozen=True)
class IntegrityCheckResult:
    """Outcome of ``PRAGMA integrity_check``.

    ``ok`` is ``True`` only when every returned row is ``ok``; a malformed
    database (``sqlite3.DatabaseError``) is also a failed check.
    """

    ok: bool
    messages: tuple[str, ...]


def _parse_integrity_check_rows(rows: Iterable[sqlite3.Row]) -> IntegrityCheckResult:
    messages = tuple(str(row[0]) for row in rows)
    return IntegrityCheckResult(ok=all(message == "ok" for message in messages), messages=messages)


def run_integrity_check(connection: sqlite3.Connection) -> IntegrityCheckResult:
    """Run ``PRAGMA integrity_check`` against a synchronous sqlite3 connection."""
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return IntegrityCheckResult(ok=False, messages=(str(exc),))
    return _parse_integrity_check_rows(rows)


async def run_integrity_check_async(connection: aiosqlite.Connection) -> IntegrityCheckResult:
    """Run ``PRAGMA integrity_check`` against an aiosqlite connection."""
    try:
        cursor = await connection.execute("PRAGMA integrity_check")
        rows = await cursor.fetchall()
        await cursor.close()
    except sqlite3.DatabaseError as exc:
        return IntegrityCheckResult(ok=False, messages=(str(exc),))
    return _parse_integrity_check_rows(rows)
