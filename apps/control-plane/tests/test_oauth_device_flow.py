import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
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
        assert (
            await repositories.oauth_authorizations.find_by_device_code(
                created.device_code, epoch=1, now=now
            )
            is not None
        )
        assert (
            await repositories.oauth_authorizations.find_by_user_code(
                created.user_code, epoch=1, now=now
            )
            is not None
        )

        await repositories.oauth_authorizations.mark_approved(created.id, epoch=1, now=now)
        exchanged = await repositories.oauth_authorizations.exchange_device_code(
            created.device_code,
            verifier,
            epoch=1,
            now=now,
        )
        assert exchanged is not None
        assert exchanged.id == created.id
        assert (
            await repositories.oauth_authorizations.exchange_device_code(
                created.device_code,
                verifier,
                epoch=1,
                now=now,
            )
            is None
        )

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
async def test_device_authorization_expiry_deny_and_atomic_exchange(tmp_path, monkeypatch) -> None:
    database, repositories, client = await _repositories(tmp_path)
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    verifier = "w" * 43
    try:
        # Expired authorizations are unfindable and cannot be approved.
        expired = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge(verifier),
            epoch=1,
            expires_at=now + timedelta(seconds=1),
            now=now,
        )
        assert (
            await repositories.oauth_authorizations.find_by_device_code(
                expired.device_code, epoch=1, now=now + timedelta(seconds=2)
            )
            is None
        )
        assert (
            await repositories.oauth_authorizations.mark_approved(
                expired.id, epoch=1, now=now + timedelta(seconds=2)
            )
            is None
        )

        # Denied authorizations can never be approved or exchanged.
        denied = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge(verifier),
            epoch=1,
            expires_at=now + timedelta(minutes=15),
            now=now,
        )
        assert (
            await repositories.oauth_authorizations.mark_denied(denied.id, epoch=1, now=now)
            is not None
        )
        assert (
            await repositories.oauth_authorizations.mark_approved(denied.id, epoch=1, now=now)
            is None
        )
        assert (
            await repositories.oauth_authorizations.exchange_device_code(
                denied.device_code,
                verifier,
                epoch=1,
                now=now,
            )
            is None
        )

        # Wrong device code or wrong PKCE verifier cannot exchange.
        wrong = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge(verifier),
            epoch=1,
            expires_at=now + timedelta(minutes=15),
            now=now,
        )
        await repositories.oauth_authorizations.mark_approved(wrong.id, epoch=1, now=now)
        assert (
            await repositories.oauth_authorizations.exchange_device_code(
                "not-the-device-code",
                verifier,
                epoch=1,
                now=now,
            )
            is None
        )
        assert (
            await repositories.oauth_authorizations.exchange_device_code(
                wrong.device_code,
                "x" * 43,
                epoch=1,
                now=now,
            )
            is None
        )

        # Exchange is atomic under concurrency: exactly one concurrent call
        # wins the one-time claim.
        atomic = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge("c" * 43),
            epoch=1,
            expires_at=now + timedelta(minutes=15),
            now=now,
        )
        await repositories.oauth_authorizations.mark_approved(atomic.id, epoch=1, now=now)
        results = await asyncio.gather(
            repositories.oauth_authorizations.exchange_device_code(
                atomic.device_code, "c" * 43, epoch=1, now=now
            ),
            repositories.oauth_authorizations.exchange_device_code(
                atomic.device_code, "c" * 43, epoch=1, now=now
            ),
        )
        assert len([result for result in results if result is not None]) == 1

        # A token-issue failure rolls back the one-time claim: the
        # authorization stays approved and exchangeable.
        rollback = await repositories.oauth_authorizations.create_device_authorization(
            client_id=client.id,
            scopes=("terminal.read",),
            pkce_challenge=create_s256_challenge("t" * 43),
            epoch=1,
            expires_at=now + timedelta(minutes=15),
            now=now,
        )
        await repositories.oauth_authorizations.mark_approved(rollback.id, epoch=1, now=now)

        async def fail_insert(*args, **kwargs):
            return None

        monkeypatch.setattr(
            "termflow_control_plane.persistence.repositories._insert_auth_token",
            fail_insert,
        )
        exchanged = await repositories.oauth_authorizations.exchange_device_code_with_tokens(
            rollback.device_code,
            "t" * 43,
            epoch=1,
            raw_access_token="a" * 43,
            raw_refresh_token="r" * 49,
            key_thumbprint=client.key_thumbprint,
            access_expires_at=now + timedelta(minutes=10),
            refresh_expires_at=now + timedelta(days=1),
            now=now,
        )
        assert exchanged is None
        row = await repositories.oauth_authorizations.get_device_authorization(
            rollback.device_code,
            epoch=1,
        )
        assert row is not None
        assert row.device_status == "approved"
        assert row.device_exchanged_at is None
        assert row.consumed_at is None
    finally:
        await database.dispose()
