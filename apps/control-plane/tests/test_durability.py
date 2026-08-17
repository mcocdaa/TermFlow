"""Acceptance tests for the SQLite durability contract (plan §17, §20).

Crash-safety claims must name the SQLite durability level (`synchronous=FULL`),
busy/transaction locking, WAL mode + checkpoint policy, and `integrity_check`
recovery. Process restart tests alone do not prove power-loss durability, so
this module exercises the real failure shapes feasible in CI:

- `kill -9` of a writer mid-transaction: committed frames survive, the
  uncommitted transaction is rolled back by WAL recovery (no torn rows), and
  the reopened database passes `integrity_check`.
- Busy-writer/backoff: a second writer waits on the configured `busy_timeout`
  and succeeds once the lock holder commits — no unchecked
  `database is locked` failure.
- WAL checkpoint: `wal_checkpoint(TRUNCATE)` empties the WAL file while the
  committed rows and `integrity_check` stay intact.
- `synchronous=FULL` is applied on a live connection, which names the exact
  power-loss durability level (a committed transaction has its data flushed
  before `COMMIT` returns). Full power-loss (disk-cache loss) cannot be
  simulated in CI; the contract names the level instead of inferring it from
  restart tests.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import aiosqlite
import pytest
from termflow_control_plane.persistence.durability import (
    DEFAULT_BUSY_TIMEOUT_MS,
    apply_durability_pragmas,
    durability_pragma_sql,
    run_integrity_check,
    run_integrity_check_async,
)

# Byte offset within the corrupted SQLite file that reliably corrupts a b-tree
# free-block pointer so that `PRAGMA integrity_check` reports failure rows.
CORRUPT_OFFSET = 8193


def _corrupt_database(path) -> None:
    payload = bytearray(path.read_bytes())
    payload[CORRUPT_OFFSET] ^= 0xFF
    path.write_bytes(bytes(payload))


def test_durability_pragma_sql_contains_required_statements() -> None:
    statements = durability_pragma_sql()
    assert "PRAGMA synchronous=FULL" in statements
    assert "PRAGMA journal_mode=WAL" in statements
    assert f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}" in statements
    assert "PRAGMA foreign_keys=ON" in statements


def test_durability_pragma_sql_rejects_negative_busy_timeout() -> None:
    with pytest.raises(ValueError):
        durability_pragma_sql(busy_timeout_ms=-1)


def test_apply_durability_pragmas_sets_expected_settings(tmp_path) -> None:
    path = tmp_path / "pragma.db"
    connection = sqlite3.connect(path)
    apply_durability_pragmas(connection)
    assert connection.execute("PRAGMA synchronous").fetchone() == (2,)  # FULL
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert connection.execute("PRAGMA busy_timeout").fetchone() == (DEFAULT_BUSY_TIMEOUT_MS,)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()


def test_integrity_check_healthy_database_returns_ok(tmp_path) -> None:
    path = tmp_path / "healthy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    connection.execute("INSERT INTO t (body) VALUES ('secret')")
    connection.commit()
    result = run_integrity_check(connection)
    connection.close()
    assert result.ok is True


def test_integrity_check_detects_corruption(tmp_path) -> None:
    path = tmp_path / "corrupt.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    connection.executemany(
        "INSERT INTO t (body) VALUES (?)",
        [(f"payload-{index}" + "x" * 500,) for index in range(200)],
    )
    connection.commit()
    connection.close()
    _corrupt_database(path)

    connection = sqlite3.connect(path)
    result = run_integrity_check(connection)
    connection.close()
    assert result.ok is False
    assert result.messages


def test_integrity_check_treats_malformed_schema_as_failure(tmp_path) -> None:
    """A schema corruption that raises DatabaseError is still a failed check."""
    path = tmp_path / "malformed.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    connection.execute("INSERT INTO t (body) VALUES ('secret')")
    connection.commit()
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute("UPDATE sqlite_master SET sql='x' WHERE name='t'")
    connection.commit()
    connection.close()

    connection = sqlite3.connect(path)
    result = run_integrity_check(connection)
    connection.close()
    assert result.ok is False
    assert result.messages


async def test_async_integrity_check_healthy_and_corrupt(tmp_path) -> None:
    path = tmp_path / "async.db"
    connection = await aiosqlite.connect(path)
    await connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    await connection.executemany(
        "INSERT INTO t (body) VALUES (?)",
        [(f"payload-{index}" + "x" * 500,) for index in range(200)],
    )
    await connection.commit()
    await connection.close()

    connection = await aiosqlite.connect(path)
    result = await run_integrity_check_async(connection)
    await connection.close()
    assert result.ok is True

    _corrupt_database(path)
    connection = await aiosqlite.connect(path)
    result = await run_integrity_check_async(connection)
    await connection.close()
    assert result.ok is False


#: Child-process writer run under ``sys.executable -c``: commit 10 rows, then
#: start one uncommitted insert, signal readiness, and wait to be ``SIGKILL``ed.
_CRASH_WRITER_SOURCE = """
import sqlite3
import sys
import time

db_path, ready_path = sys.argv[1], sys.argv[2]
connection = sqlite3.connect(db_path)
connection.execute("PRAGMA synchronous=FULL")
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA busy_timeout=5000")
connection.execute("PRAGMA foreign_keys=ON")
for index in range(10):
    connection.execute("INSERT INTO events(payload) VALUES (?)", (f"committed-{index}",))
connection.commit()
connection.execute("INSERT INTO events(payload) VALUES (?)", ("torn-row",))
with open(ready_path, "w") as handle:
    handle.write("ready")
deadline = time.monotonic() + 120.0
while time.monotonic() < deadline:
    time.sleep(0.1)
connection.close()
"""


def test_sigkill_mid_transaction_keeps_commits_and_rolls_back_uncommitted(tmp_path) -> None:
    """kill -9 of a writer mid-transaction leaves a WAL-consistent database.

    Committed rows survive, the uncommitted row is rolled back by WAL recovery
    (no torn rows), and ``integrity_check`` passes on reopen.
    """
    db_path = tmp_path / "crash.db"
    ready_path = tmp_path / "ready"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
    connection.close()

    process = subprocess.Popen(
        [sys.executable, "-c", _CRASH_WRITER_SOURCE, str(db_path), str(ready_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30.0
        while not ready_path.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"crash writer exited early ({process.returncode}): "
                    f"stdout={stdout!r} stderr={stderr!r}"
                )
            time.sleep(0.02)
        assert ready_path.exists(), "crash writer never reached the uncommitted write"
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    assert process.returncode == -signal.SIGKILL

    reopened = sqlite3.connect(db_path)
    try:
        payloads = [row[0] for row in reopened.execute("SELECT payload FROM events ORDER BY id")]
        assert payloads == [f"committed-{index}" for index in range(10)]
        assert "torn-row" not in payloads
        assert run_integrity_check(reopened).ok is True
    finally:
        reopened.close()


def test_busy_writer_waits_for_busy_timeout_then_succeeds(tmp_path) -> None:
    """A second writer backs off on the busy handler and commits after release.

    The lock holder keeps an exclusive transaction for ~0.6s; the waiting
    writer must block (not fail with ``database is locked``) and commit once
    the holder releases.
    """
    db_path = tmp_path / "busy.db"
    holder = sqlite3.connect(db_path, check_same_thread=False)
    apply_durability_pragmas(holder)
    holder.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO events(payload) VALUES ('holder')")

    started = threading.Event()
    outcome: dict[str, float | str] = {}

    def writer() -> None:
        connection = sqlite3.connect(db_path, check_same_thread=False)
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            started.set()
            begin = time.monotonic()
            connection.execute("INSERT INTO events(payload) VALUES ('waiter')")
            connection.commit()
            outcome["elapsed"] = time.monotonic() - begin
        except Exception as exc:  # pragma: no cover - failure path asserted below
            outcome["error"] = repr(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=writer)
    thread.start()
    assert started.wait(timeout=10), "writer thread never started"
    time.sleep(0.6)  # hold the write lock while the waiter blocks
    holder.commit()
    holder.close()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["elapsed"] >= 0.4  # it actually waited on the busy handler
    assert outcome["elapsed"] < 5.0  # and finished well inside the 5000ms timeout

    reader = sqlite3.connect(db_path)
    try:
        payloads = {row[0] for row in reader.execute("SELECT payload FROM events")}
        assert payloads == {"holder", "waiter"}
        assert run_integrity_check(reader).ok is True
    finally:
        reader.close()


def test_wal_checkpoint_truncate_empties_wal_and_preserves_rows(tmp_path) -> None:
    """A full checkpoint empties the WAL file while rows and integrity hold."""
    db_path = tmp_path / "checkpoint.db"
    connection = sqlite3.connect(db_path)
    apply_durability_pragmas(connection)
    connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO events(payload) VALUES (?)",
        [(f"row-{index}",) for index in range(100)],
    )
    connection.commit()

    wal_path = Path(f"{db_path}-wal")
    assert wal_path.exists()
    assert wal_path.stat().st_size > 0

    busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert busy == 0
    assert wal_path.stat().st_size == 0

    assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (100,)
    assert run_integrity_check(connection).ok is True
    connection.close()
