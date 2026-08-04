import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from termflow_control_plane.auth.pkce import create_s256_challenge
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import OAuthAuthorization
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret


async def _repositories(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'device-flow.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    client = await repositories.native_clients.create(
        display_name="Device test client",
        public_jwk="{}",
        key_thumbprint="device-test-thumbprint",
        platform="test",
        scopes=("terminal.read",),
    )
    return database, repositories, client


@pytest.mark.asyncio
async def test_device_authorization_lifecycle_and_digest_only_storage(tmp_path) -> None:
    database, repositories, client = await _repositories(tmp_path)
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    verifier = "v" * 43
    try:
        created = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge(verifier),
            epoch=1,
            expires_at=now + timedelta(minutes=15),
            interval=5,
            now=now,
        )
        assert created.user_code != created.device_code
        assert created.status == "pending"
        assert created.interval == 5
        assert await repositories.oauth_authorizations.find_by_device_code(
            created.device_code, epoch=1, now=now
        ) is not None
        assert await repositories.oauth_authorizations.find_by_user_code(
            created.user_code, epoch=1, now=now
        ) is not None

        await repositories.oauth_authorizations.mark_approved(created.id, epoch=1, now=now)
        exchanged = await repositories.oauth_authorizations.exchange_device_code(
            created.device_code,
            verifier,
            epoch=1,
            now=now,
        )
        assert exchanged is not None
        assert exchanged.id == created.id
        assert await repositories.oauth_authorizations.exchange_device_code(
            created.device_code,
            verifier,
            epoch=1,
            now=now,
        ) is None

        async with database.session_factory() as session:
            row = await session.scalar(
                select(OAuthAuthorization).where(OAuthAuthorization.id == created.id)
            )
        assert row is not None
        assert row.device_code_digest == digest_secret(created.device_code)
        assert row.user_code_digest == digest_secret(created.user_code)
        assert created.device_code not in repr(row)
        assert created.user_code not in repr(row)
        assert row.device_exchanged_at.replace(tzinfo=UTC) == now
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_device_authorization_expiry_deny_and_wrong_code_fail(tmp_path) -> None:
    database, repositories, client = await _repositories(tmp_path)
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    verifier = "w" * 43
    try:
        expired = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge(verifier),
            epoch=1,
            expires_at=now + timedelta(seconds=1),
            now=now,
        )
        assert await repositories.oauth_authorizations.find_by_device_code(
            expired.device_code, epoch=1, now=now + timedelta(seconds=2)
        ) is None
        assert await repositories.oauth_authorizations.mark_approved(
            expired.id, epoch=1, now=now + timedelta(seconds=2)
        ) is None

        denied = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge(verifier),
            epoch=1,
            expires_at=now + timedelta(minutes=15),
            now=now,
        )
        assert await repositories.oauth_authorizations.mark_denied(
            denied.id, epoch=1, now=now
        ) is not None
        assert await repositories.oauth_authorizations.mark_approved(
            denied.id, epoch=1, now=now
        ) is None
        assert await repositories.oauth_authorizations.exchange_device_code(
            denied.device_code,
            verifier,
            epoch=1,
            now=now,
        ) is None

        wrong = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge(verifier),
            epoch=1,
            expires_at=now + timedelta(minutes=15),
            now=now,
        )
        await repositories.oauth_authorizations.mark_approved(wrong.id, epoch=1, now=now)
        assert await repositories.oauth_authorizations.exchange_device_code(
            "not-the-device-code",
            verifier,
            epoch=1,
            now=now,
        ) is None
        assert await repositories.oauth_authorizations.exchange_device_code(
            wrong.device_code,
            "x" * 43,
            epoch=1,
            now=now,
        ) is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_device_authorization_exchange_is_atomic_under_concurrency(tmp_path) -> None:
    database, repositories, client = await _repositories(tmp_path)
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    verifier = "c" * 43
    try:
        created = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge(verifier),
            epoch=1,
            expires_at=now + timedelta(minutes=15),
            now=now,
        )
        await repositories.oauth_authorizations.mark_approved(created.id, epoch=1, now=now)
        results = await asyncio.gather(
            repositories.oauth_authorizations.exchange_device_code(
                created.device_code, verifier, epoch=1, now=now
            ),
            repositories.oauth_authorizations.exchange_device_code(
                created.device_code, verifier, epoch=1, now=now
            ),
        )
        assert len([result for result in results if result is not None]) == 1
    finally:
        await database.dispose()


def test_device_flow_migration_adds_digest_and_lifecycle_columns(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")

    async def initialize() -> set[str]:
        await database.initialize()
        try:
            async with database.engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync: {
                        column["name"]
                        for column in inspect(sync).get_columns("oauth_authorizations")
                    }
                )
        finally:
            await database.dispose()

    columns = asyncio.run(initialize())
    assert {
        "device_code_digest",
        "user_code_digest",
        "device_status",
        "device_interval",
        "device_exchanged_at",
    } <= columns
