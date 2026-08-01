from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import inspect
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
        hash_token(raw), datetime.now(UTC) + timedelta(minutes=10)
    )
    assert await repositories.enrollments.consume(hash_token(raw)) == enrollment.id
    assert await repositories.enrollments.consume(hash_token(raw)) is None


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
