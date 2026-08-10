"""Acceptance tests for the SQLite durability contract (plan §17, §20).

Crash-safety claims must name the SQLite durability level (`synchronous=FULL`),
busy/transaction locking, WAL mode + checkpoint policy, and `integrity_check`
recovery. Process restart tests alone do not prove power-loss durability.
"""

import sqlite3

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
    assert connection.execute("PRAGMA busy_timeout").fetchone() == (
        DEFAULT_BUSY_TIMEOUT_MS,
    )
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
