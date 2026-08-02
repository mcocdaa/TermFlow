import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AuditEvent
from termflow_control_plane.persistence.repositories import (
    InstanceOwnershipError,
    RepositoryBundle,
)


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    await database.initialize()
    try:
        yield RepositoryBundle(database.session_factory)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_enrollment_is_consumed_once(repositories: RepositoryBundle) -> None:
    raw = "x" * 43
    enrollment = await repositories.enrollments.create(
        hash_token(raw),
        datetime.now(UTC) + timedelta(minutes=10),
        display_name="跑步工作站",
    )
    consumed = await repositories.enrollments.consume(hash_token(raw))
    assert consumed is not None
    assert consumed.id == enrollment.id
    assert consumed.display_name == "跑步工作站"
    assert await repositories.enrollments.consume(hash_token(raw)) is None


@pytest.mark.asyncio
async def test_concurrent_enrollment_consumption_has_exactly_one_winner(
    repositories: RepositoryBundle,
) -> None:
    raw = "concurrent-" + "z" * 43
    enrollment = await repositories.enrollments.create(
        hash_token(raw),
        datetime.now(UTC) + timedelta(minutes=10),
        display_name="并发工作站",
    )

    results = await asyncio.gather(
        repositories.enrollments.consume(hash_token(raw)),
        repositories.enrollments.consume(hash_token(raw)),
    )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].id == enrollment.id
    assert winners[0].display_name == "并发工作站"
    assert results.count(None) == 1


@pytest.mark.asyncio
async def test_expired_enrollment_cannot_be_consumed(repositories: RepositoryBundle) -> None:
    raw = "y" * 43
    await repositories.enrollments.create(
        hash_token(raw), datetime.now(UTC) - timedelta(seconds=1)
    )
    assert await repositories.enrollments.consume(hash_token(raw)) is None


@pytest.mark.asyncio
async def test_instance_registration_rotates_only_for_owner(
    repositories: RepositoryBundle,
) -> None:
    first_installation = await repositories.installations.create(hash_token("first"))
    second_installation = await repositories.installations.create(hash_token("second"))
    instance_id = uuid4()
    await repositories.instances.register_or_rotate(
        instance_id,
        first_installation.id,
        "alpha",
        hash_token("instance-one"),
    )
    rotated = await repositories.instances.register_or_rotate(
        instance_id,
        first_installation.id,
        "renamed",
        hash_token("instance-two"),
    )
    assert rotated.name == "renamed"
    assert rotated.token_hash == hash_token("instance-two")
    with pytest.raises(InstanceOwnershipError):
        await repositories.instances.register_or_rotate(
            instance_id,
            second_installation.id,
            "stolen",
            hash_token("bad"),
        )


@pytest.mark.asyncio
async def test_installation_metadata_and_last_seen_are_persisted(
    repositories: RepositoryBundle,
) -> None:
    installation = await repositories.installations.create(
        hash_token("computer"),
        hostname="devbox",
        display_name="devbox",
        platform="Linux",
        client_version="0.1.0",
    )
    assert installation.hostname == "devbox"
    assert installation.display_name == "devbox"
    assert installation.last_seen_at is None

    instance = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        "alpha",
        hash_token("instance"),
    )
    refreshed = await repositories.installations.get(installation.id)
    assert instance.last_seen_at is not None
    assert refreshed is not None
    assert refreshed.last_seen_at is not None


@pytest.mark.asyncio
async def test_initialize_idempotently_upgrades_a_v1_sqlite_database(tmp_path) -> None:
    path = tmp_path / "v1.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE enrollment_tokens (
              id CHAR(32) PRIMARY KEY,
              token_hash VARCHAR(64) NOT NULL,
              expires_at DATETIME NOT NULL,
              used_at DATETIME,
              created_at DATETIME NOT NULL
            );
            CREATE TABLE installations (
              id CHAR(32) PRIMARY KEY,
              token_hash VARCHAR(64) NOT NULL,
              created_at DATETIME NOT NULL,
              revoked_at DATETIME
            );
            CREATE TABLE instances (
              id CHAR(32) PRIMARY KEY,
              installation_id CHAR(32) NOT NULL,
              name VARCHAR(128) NOT NULL,
              token_hash VARCHAR(64) NOT NULL,
              created_at DATETIME NOT NULL,
              revoked_at DATETIME
            );
            """
        )

    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    await database.initialize()
    try:
        async with database.engine.connect() as connection:
            enrollment_rows = await connection.execute(
                text("PRAGMA table_info(enrollment_tokens)")
            )
            installation_rows = await connection.execute(text("PRAGMA table_info(installations)"))
            instance_rows = await connection.execute(text("PRAGMA table_info(instances)"))
        enrollment_columns = {row[1] for row in enrollment_rows}
        installation_columns = {row[1] for row in installation_rows}
        instance_columns = {row[1] for row in instance_rows}
        assert "display_name" in enrollment_columns
        assert {
            "hostname",
            "display_name",
            "platform",
            "client_version",
            "last_seen_at",
        } <= installation_columns
        assert "last_seen_at" in instance_columns
        async with database.engine.connect() as connection:
            enrollment_indexes = await connection.execute(
                text("PRAGMA index_list(enrollment_tokens)")
            )
            installation_indexes = await connection.execute(
                text("PRAGMA index_list(installations)")
            )
            instance_indexes = await connection.execute(text("PRAGMA index_list(instances)"))
        assert "ix_enrollment_tokens_token_hash" in {row[1] for row in enrollment_indexes}
        assert "ix_installations_token_hash" in {row[1] for row in installation_indexes}
        assert "ix_instances_token_hash" in {row[1] for row in instance_indexes}
    finally:
        await database.dispose()


def test_audit_schema_has_metadata_only_columns() -> None:
    names = {column.name for column in inspect(AuditEvent).columns}
    assert names == {
        "id",
        "operation",
        "instance_id",
        "pane_id",
        "input_bytes",
        "result",
        "error_code",
        "created_at",
    }
    assert names.isdisjoint({"text", "input", "output", "payload", "content", "data"})
