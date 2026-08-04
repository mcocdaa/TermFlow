import asyncio
import base64
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from termflow_control_plane.auth.secret_box import AesGcmSecretBox
from termflow_control_plane.persistence.database import (
    Database,
    UnrecognizedDatabaseSchema,
    _migration_config,
)
from termflow_control_plane.persistence.models import (
    AuthAuditEvent,
    AuthChallenge,
    AuthenticationState,
    AuthToken,
    NativeClient,
    OAuthAuthorization,
    TotpSetup,
)
from termflow_control_plane.persistence.repositories import (
    AuthenticationStateChanged,
    NativeClientRevoked,
    RepositoryBundle,
    digest_secret,
)


@pytest.fixture
def secret_box() -> AesGcmSecretBox:
    return AesGcmSecretBox(b"k" * 32, key_version=7)


@pytest.mark.asyncio
async def test_fresh_database_is_migrated_and_starts_at_auth_epoch_one(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await database.initialize()
    try:
        async with database.engine.connect() as connection:
            table_names = set(
                await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            )
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "0004"
        assert {
            "authentication_state",
            "totp_setups",
            "auth_challenges",
            "native_clients",
            "oauth_authorizations",
            "auth_tokens",
            "auth_audit_events",
        } <= table_names

        state = await RepositoryBundle(database.session_factory).auth_state.get()
        assert state.epoch == 1
        assert state.totp_ciphertext is None
        assert state.totp_last_accepted_counter is None
    finally:
        await database.dispose()


def test_existing_unified_auth_database_is_upgraded_with_oauth_request_state(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'existing-0002.db'}")
    try:
        with engine.begin() as connection:
            config = _migration_config(connection)
            command.upgrade(config, "0002")
            old_columns = {
                column["name"]
                for column in inspect(connection).get_columns("oauth_authorizations")
            }
            assert "request_state" not in old_columns

            command.upgrade(config, "head")
            new_columns = {
                column["name"]
                for column in inspect(connection).get_columns("oauth_authorizations")
            }
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert "request_state" in new_columns
        assert revision == "0004"
    finally:
        engine.dispose()


def test_aes_gcm_secret_box_uses_random_nonce_versioned_aad_and_safe_repr(
    secret_box: AesGcmSecretBox,
) -> None:
    plaintext = b"JBSWY3DPEHPK3PXP"
    first = secret_box.encrypt(plaintext, purpose="totp-authenticator")
    second = secret_box.encrypt(plaintext, purpose="totp-authenticator")

    assert len(first.nonce) == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert first.key_version == 7
    assert first.aad_version == 1
    assert secret_box.decrypt(first, purpose="totp-authenticator") == plaintext
    with pytest.raises(ValueError):
        secret_box.decrypt(first, purpose="totp-setup")
    assert plaintext.decode() not in repr(secret_box)
    assert plaintext.decode() not in repr(first)
    assert (b"k" * 32).hex() not in repr(secret_box)


@pytest.mark.asyncio
async def test_totp_secret_and_counter_are_persisted_without_plaintext(
    tmp_path,
    secret_box: AesGcmSecretBox,
) -> None:
    path = tmp_path / "totp.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    raw_secret = b"RAW_TOTP_SECRET_MUST_NOT_APPEAR"
    encrypted = secret_box.encrypt(raw_secret, purpose="totp-authenticator")
    try:
        await repositories.auth_state.configure_totp(
            encrypted,
            counter=100,
            expected_epoch=1,
            expected_generation=0,
            enabled=True,
        )
        state = await repositories.auth_state.get()
        assert state.totp_ciphertext == encrypted.ciphertext
        assert state.totp_nonce == encrypted.nonce
        assert state.totp_key_version == encrypted.key_version
        assert state.totp_aad_version == encrypted.aad_version
        assert state.totp_enabled_at is not None
        assert state.totp_last_accepted_counter == 100
        assert state.totp_generation == 1

        results = await asyncio.gather(
            repositories.auth_state.accept_totp_counter(
                101,
                epoch=state.epoch,
                generation=state.totp_generation,
            ),
            repositories.auth_state.accept_totp_counter(
                101,
                epoch=state.epoch,
                generation=state.totp_generation,
            ),
        )
        assert sorted(results) == [False, True]
        assert (
            await repositories.auth_state.accept_totp_counter(
                100,
                epoch=state.epoch,
                generation=state.totp_generation,
            )
            is False
        )
    finally:
        await database.dispose()

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert raw_secret not in path.read_bytes()


@pytest.mark.asyncio
async def test_old_totp_generation_cannot_advance_a_reconfigured_authenticator(
    tmp_path,
    secret_box: AesGcmSecretBox,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'totp-generation.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    try:
        first = secret_box.encrypt(b"first", purpose="totp-authenticator")
        await repositories.auth_state.configure_totp(
            first,
            counter=10,
            expected_epoch=1,
            expected_generation=0,
            enabled=True,
        )
        first_state = await repositories.auth_state.get()

        second = secret_box.encrypt(b"second", purpose="totp-authenticator")
        await repositories.auth_state.configure_totp(
            second,
            counter=20,
            expected_epoch=1,
            expected_generation=first_state.totp_generation,
            enabled=True,
        )
        second_state = await repositories.auth_state.get()
        assert second_state.totp_generation == first_state.totp_generation + 1

        assert (
            await repositories.auth_state.accept_totp_counter(
                21,
                epoch=first_state.epoch,
                generation=first_state.totp_generation,
            )
            is False
        )
        assert await repositories.auth_state.accept_totp_counter(
            21,
            epoch=second_state.epoch,
            generation=second_state.totp_generation,
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_totp_configuration_and_protection_transitions_are_atomic(
    tmp_path,
    secret_box: AesGcmSecretBox,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'totp-cas.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    try:
        encrypted = secret_box.encrypt(b"first", purpose="totp-authenticator")
        assert await repositories.auth_state.configure_totp(
            encrypted,
            counter=10,
            expected_epoch=1,
            expected_generation=0,
            enabled=False,
        )
        configured = await repositories.auth_state.get()
        assert configured.totp_ciphertext == encrypted.ciphertext
        assert configured.totp_enabled_at is None
        assert configured.totp_last_accepted_counter == 10

        assert not await repositories.auth_state.configure_totp(
            encrypted,
            counter=11,
            expected_epoch=1,
            expected_generation=0,
            enabled=False,
        )
        assert not await repositories.auth_state.enable_totp_protection(
            counter=10,
            expected_epoch=configured.epoch,
            expected_generation=configured.totp_generation,
        )
        assert await repositories.auth_state.enable_totp_protection(
            counter=11,
            expected_epoch=configured.epoch,
            expected_generation=configured.totp_generation,
        )
        enabled = await repositories.auth_state.get()
        assert enabled.totp_enabled_at is not None
        assert enabled.totp_last_accepted_counter == 11

        assert not await repositories.auth_state.disable_totp_protection(
            expected_epoch=enabled.epoch,
            expected_generation=enabled.totp_generation - 1,
        )
        assert await repositories.auth_state.disable_totp_protection(
            expected_epoch=enabled.epoch,
            expected_generation=enabled.totp_generation,
        )
        disabled = await repositories.auth_state.get()
        assert disabled.totp_enabled_at is None
        assert disabled.totp_ciphertext == encrypted.ciphertext
        assert disabled.totp_nonce == encrypted.nonce
        assert disabled.totp_last_accepted_counter == 11
        assert disabled.totp_generation == enabled.totp_generation + 1

        assert await repositories.auth_state.reset_and_increment_epoch() == 2
        reset = await repositories.auth_state.get()
        assert reset.totp_ciphertext is None
        assert reset.totp_last_accepted_counter is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_totp_setup_cannot_reenable_after_epoch_reset(
    tmp_path,
    secret_box: AesGcmSecretBox,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stale-setup.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    encrypted = secret_box.encrypt(b"stale", purpose="totp-authenticator")
    try:
        assert await repositories.auth_state.reset_and_increment_epoch() == 2
        assert (
            await repositories.auth_state.configure_totp(
                encrypted,
                counter=1,
                expected_epoch=1,
                expected_generation=1,
                enabled=True,
            )
            is False
        )
        assert (await repositories.auth_state.get()).totp_enabled_at is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_pending_totp_setup_is_encrypted_and_consumed_once(
    tmp_path,
    secret_box: AesGcmSecretBox,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'setup.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    encrypted = secret_box.encrypt(b"pending-secret", purpose="totp-setup")
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    try:
        setup_id = await repositories.totp_setups.create(encrypted, expires_at=expires_at, epoch=1)
        assert await repositories.totp_setups.get_active(setup_id, epoch=1) == encrypted
        results = await asyncio.gather(
            repositories.totp_setups.consume(setup_id, epoch=1),
            repositories.totp_setups.consume(setup_id, epoch=1),
        )
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        assert winners[0] == encrypted
        assert await repositories.totp_setups.get_active(setup_id, epoch=1) is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_auth_challenge_attempts_and_consumption_are_atomic(
    tmp_path,
    secret_box: AesGcmSecretBox,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'challenge.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    context = secret_box.encrypt(b'{"flow":"web-login"}', purpose="auth-challenge")
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    try:
        challenge_id = await repositories.auth_challenges.create(
            "web_login",
            context,
            expires_at=expires_at,
            epoch=1,
        )
        assert (
            await repositories.auth_challenges.get_active(
                challenge_id,
                "web_login",
                epoch=1,
            )
            == context
        )
        assert await repositories.auth_challenges.fail_attempt(challenge_id, maximum=5)

        results = await asyncio.gather(
            repositories.auth_challenges.consume(challenge_id, "web_login", epoch=1),
            repositories.auth_challenges.consume(challenge_id, "web_login", epoch=1),
        )
        winners = [result for result in results if result is not None]
        assert winners == [context]

        async with database.session_factory() as session:
            row = await session.scalar(select(AuthChallenge))
            assert row is not None
            assert row.challenge_digest == digest_secret(str(challenge_id))
        assert str(challenge_id) not in repr(row)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_auth_challenge_is_destroyed_at_the_attempt_limit(
    tmp_path,
    secret_box: AesGcmSecretBox,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'attempt-limit.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    context = secret_box.encrypt(b"context", purpose="auth-challenge")
    try:
        challenge_id = await repositories.auth_challenges.create(
            "web_login",
            context,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            epoch=1,
        )
        assert all(
            [await repositories.auth_challenges.fail_attempt(challenge_id) for _ in range(5)]
        )
        assert await repositories.auth_challenges.fail_attempt(challenge_id) is False
        assert (
            await repositories.auth_challenges.consume(challenge_id, "web_login", epoch=1)
            is None
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_oauth_code_and_refresh_rotation_have_one_winner_and_store_digests_only(
    tmp_path,
) -> None:
    path = tmp_path / "oauth.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    raw_code = "authorization-code-plaintext"
    raw_refresh = "refresh-token-plaintext"
    replacement = "replacement-refresh-plaintext"
    client_id = uuid4()
    now = datetime.now(UTC)
    try:
        await repositories.native_clients.create(
            client_id=client_id,
            display_name="Desktop",
            public_jwk='{"kty":"EC","crv":"P-256"}',
            key_thumbprint="thumbprint",
            platform="linux",
            scopes=("terminal.read", "terminal.write"),
        )
        active_client = await repositories.native_clients.get_active_by_thumbprint("thumbprint")
        assert active_client is not None
        assert active_client.id == client_id
        authorization_id = await repositories.oauth_authorizations.create(
            transaction_secret="transaction-secret",
            client_id=client_id,
            redirect_uri="termflow://oauth/callback",
            request_state="persisted-state-for-transaction",
            scopes=("terminal.read",),
            pkce_challenge="pkce-challenge",
            expires_at=now + timedelta(minutes=5),
            epoch=1,
        )
        pending = await repositories.oauth_authorizations.get_active_transaction(
            "transaction-secret",
            epoch=1,
        )
        assert pending is not None
        assert pending.id == authorization_id
        await repositories.oauth_authorizations.issue_code(authorization_id, raw_code)
        code_results = await asyncio.gather(
            repositories.oauth_authorizations.exchange_code(
                raw_code,
                epoch=1,
                raw_access_token="exchange-access-one",
                raw_refresh_token="exchange-refresh-one",
                key_thumbprint="thumbprint",
                pkce_challenge="pkce-challenge",
                access_expires_at=now + timedelta(minutes=10),
                refresh_expires_at=now + timedelta(days=30),
            ),
            repositories.oauth_authorizations.exchange_code(
                raw_code,
                epoch=1,
                raw_access_token="exchange-access-two",
                raw_refresh_token="exchange-refresh-two",
                key_thumbprint="thumbprint",
                pkce_challenge="pkce-challenge",
                access_expires_at=now + timedelta(minutes=10),
                refresh_expires_at=now + timedelta(days=30),
            ),
        )
        assert sum(result is not None for result in code_results) == 1

        refresh = await repositories.auth_tokens.issue(
            raw_refresh,
            kind="refresh",
            scopes=("terminal.read",),
            key_thumbprint="thumbprint",
            expires_at=now + timedelta(days=30),
            epoch=1,
            client_id=client_id,
        )
        rotate_results = await asyncio.gather(
            repositories.auth_tokens.rotate_refresh(
                raw_refresh,
                replacement,
                expires_at=now + timedelta(days=30),
                epoch=1,
            ),
            repositories.auth_tokens.rotate_refresh(
                raw_refresh,
                "losing-replacement",
                expires_at=now + timedelta(days=30),
                epoch=1,
            ),
        )
        assert sum(result is not None for result in rotate_results) == 1
        assert refresh.token_digest == digest_secret(raw_refresh)
        await repositories.auth_tokens.issue(
            "access-token-plaintext",
            kind="access",
            scopes=("terminal.read",),
            key_thumbprint="thumbprint",
            expires_at=now + timedelta(minutes=10),
            epoch=1,
            client_id=client_id,
        )
        await repositories.auth_tokens.issue(
            "cli-token-plaintext",
            kind="cli",
            scopes=("terminal.read",),
            key_thumbprint=None,
            expires_at=now + timedelta(minutes=15),
            epoch=1,
        )
    finally:
        await database.dispose()

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_bytes = path.read_bytes()
    assert raw_code.encode() not in database_bytes
    assert raw_refresh.encode() not in database_bytes
    assert replacement.encode() not in database_bytes
    assert b"access-token-plaintext" not in database_bytes
    assert b"cli-token-plaintext" not in database_bytes


@pytest.mark.asyncio
async def test_reset_increments_epoch_and_invalidates_auth_artifacts_but_keeps_clients(
    tmp_path,
    secret_box: AesGcmSecretBox,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reset.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    client_id = uuid4()
    encrypted = secret_box.encrypt(b"secret", purpose="totp-authenticator")
    try:
        await repositories.auth_state.configure_totp(
            encrypted,
            counter=42,
            expected_epoch=1,
            expected_generation=0,
            enabled=True,
        )
        pending = secret_box.encrypt(b"pending-reset-secret", purpose="totp-setup")
        await repositories.totp_setups.create(
            pending,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            epoch=1,
        )
        await repositories.native_clients.create(
            client_id=client_id,
            display_name="Phone",
            public_jwk='{"kty":"EC"}',
            key_thumbprint="phone-thumbprint",
            platform="android",
            scopes=("terminal.read",),
        )
        await repositories.auth_tokens.issue(
            "cli-secret",
            kind="cli",
            scopes=("terminal.read",),
            key_thumbprint=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            epoch=1,
        )

        assert await repositories.auth_state.reset_and_increment_epoch() == 2
        state = await repositories.auth_state.get()
        assert state.epoch == 2
        assert state.totp_ciphertext is None
        assert await repositories.auth_tokens.get_active("cli-secret", epoch=2) is None
        with pytest.raises(AuthenticationStateChanged):
            await repositories.auth_tokens.issue(
                "stale-epoch-token",
                kind="cli",
                scopes=("terminal.read",),
                key_thumbprint=None,
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
                epoch=1,
            )
        client = await repositories.native_clients.get(client_id)
        assert client is not None
        assert client.revoked_at is None
        async with database.session_factory() as session:
            assert await session.scalar(select(func.count(TotpSetup.id))) == 0
        audit = await repositories.auth_audit.list_all()
        assert [(event.operation, event.result) for event in audit] == [
            ("auth.reset", "reset")
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_native_client_lifecycle_and_auth_audit_are_separate_from_terminal_audit(
    tmp_path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'client-audit.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    client_id = uuid4()
    try:
        client = await repositories.native_clients.create(
            client_id=client_id,
            display_name="Desktop",
            public_jwk='{"kty":"EC"}',
            key_thumbprint="desktop-thumbprint",
            platform="macos",
            client_version="0.1.0",
            scopes=("terminal.write", "terminal.read"),
        )
        assert client.created_at is not None
        assert client.updated_at is not None
        assert client.last_used_at is None
        assert client.client_version == "0.1.0"
        touched_at = datetime.now(UTC)
        assert await repositories.native_clients.touch(client_id, now=touched_at)
        touched = await repositories.native_clients.get(client_id)
        assert touched is not None
        assert touched.last_used_at is not None
        renamed = await repositories.native_clients.rename(client_id, "Work Mac")
        assert renamed is not None
        assert renamed.display_name == "Work Mac"
        assert await repositories.native_clients.revoke(client_id)

        event = await repositories.auth_audit.record(
            operation="auth.client.revoke",
            result="ok",
            source_digest=digest_secret("127.0.0.1"),
            client_id=client_id,
        )
        assert event.source_digest == digest_secret("127.0.0.1")
        assert len(await repositories.auth_audit.list_all()) == 1
        assert await repositories.audit.list_all() == []
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_revoked_native_client_cannot_receive_a_new_token(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'revoked-client.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    client_id = uuid4()
    try:
        await repositories.native_clients.create(
            client_id=client_id,
            display_name="Revoked",
            public_jwk='{"kty":"EC"}',
            key_thumbprint="revoked-thumbprint",
            platform="linux",
            scopes=("terminal.read",),
        )
        assert await repositories.native_clients.revoke(client_id)
        with pytest.raises(NativeClientRevoked):
            await repositories.auth_tokens.issue(
                "must-not-be-issued",
                kind="access",
                scopes=("terminal.read",),
                key_thumbprint="revoked-thumbprint",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                epoch=1,
                client_id=client_id,
            )
        assert await repositories.auth_tokens.get_active("must-not-be-issued", epoch=1) is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_refresh_replay_revokes_the_entire_token_family(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'refresh-replay.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    now = datetime.now(UTC)
    try:
        await repositories.auth_tokens.issue(
            "refresh-original",
            kind="refresh",
            scopes=("terminal.read",),
            key_thumbprint="thumbprint",
            expires_at=now + timedelta(days=30),
            epoch=1,
        )
        assert (
            await repositories.auth_tokens.rotate_refresh(
                "refresh-original",
                "refresh-replacement",
                expires_at=now + timedelta(days=30),
                epoch=1,
            )
            is not None
        )
        assert (
            await repositories.auth_tokens.rotate_refresh(
                "refresh-original",
                "attacker-replacement",
                expires_at=now + timedelta(days=30),
                epoch=1,
            )
            is None
        )
        assert (
            await repositories.auth_tokens.get_active("refresh-replacement", epoch=1)
            is None
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_authorization_code_expires_sixty_seconds_after_issue(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'code-ttl.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    client_id = uuid4()
    try:
        await repositories.native_clients.create(
            client_id=client_id,
            display_name="Desktop",
            public_jwk='{"kty":"EC"}',
            key_thumbprint="code-thumbprint",
            platform="linux",
            scopes=("terminal.read",),
        )
        authorization_id = await repositories.oauth_authorizations.create(
            transaction_secret="code-ttl-transaction",
            client_id=client_id,
            redirect_uri="termflow://oauth/callback",
            request_state="persisted-state-for-code-ttl",
            scopes=("terminal.read",),
            pkce_challenge="challenge",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            epoch=1,
        )
        assert await repositories.oauth_authorizations.issue_code(
            authorization_id,
            "short-code",
        )
        async with database.session_factory() as session:
            issued_at = await session.scalar(
                select(OAuthAuthorization.code_issued_at).where(
                    OAuthAuthorization.id == authorization_id
                )
            )
        assert issued_at is not None
        assert (
            await repositories.oauth_authorizations.exchange_code(
                "short-code",
                epoch=1,
                raw_access_token="expired-access",
                raw_refresh_token="expired-refresh",
                key_thumbprint="code-thumbprint",
                pkce_challenge="challenge",
                access_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=30),
                now=issued_at.replace(tzinfo=UTC) + timedelta(seconds=61),
            )
            is None
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_code_consumption_and_token_inserts_share_one_transaction(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'atomic-exchange.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    client_id = uuid4()
    now = datetime.now(UTC)
    try:
        await repositories.native_clients.create(
            client_id=client_id,
            display_name="Desktop",
            public_jwk='{"kty":"EC"}',
            key_thumbprint="exchange-thumbprint",
            platform="linux",
            scopes=("terminal.read",),
        )
        authorization_id = await repositories.oauth_authorizations.create(
            transaction_secret="exchange-transaction",
            client_id=client_id,
            redirect_uri="termflow://oauth/callback",
            request_state="persisted-state-for-exchange",
            scopes=("terminal.read",),
            pkce_challenge="challenge",
            expires_at=now + timedelta(minutes=5),
            epoch=1,
        )
        assert await repositories.oauth_authorizations.issue_code(
            authorization_id,
            "exchange-code",
        )
        await repositories.auth_tokens.issue(
            "digest-collision",
            kind="cli",
            scopes=("terminal.read",),
            key_thumbprint=None,
            expires_at=now + timedelta(minutes=15),
            epoch=1,
        )

        assert (
            await repositories.oauth_authorizations.exchange_code(
                "exchange-code",
                epoch=1,
                raw_access_token="wrong-proof-access",
                raw_refresh_token="wrong-proof-refresh",
                key_thumbprint="exchange-thumbprint",
                pkce_challenge="wrong-challenge",
                access_expires_at=now + timedelta(minutes=10),
                refresh_expires_at=now + timedelta(days=30),
            )
            is None
        )

        with pytest.raises(IntegrityError):
            await repositories.oauth_authorizations.exchange_code(
                "exchange-code",
                epoch=1,
                raw_access_token="digest-collision",
                raw_refresh_token="new-refresh",
                key_thumbprint="exchange-thumbprint",
                pkce_challenge="challenge",
                access_expires_at=now + timedelta(minutes=10),
                refresh_expires_at=now + timedelta(days=30),
            )
        exchanged = await repositories.oauth_authorizations.exchange_code(
            "exchange-code",
            epoch=1,
            raw_access_token="valid-access",
            raw_refresh_token="valid-refresh",
            key_thumbprint="exchange-thumbprint",
            pkce_challenge="challenge",
            access_expires_at=now + timedelta(minutes=10),
            refresh_expires_at=now + timedelta(days=30),
        )
        assert exchanged is not None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unversioned_current_database_is_stamped_then_upgraded(tmp_path) -> None:
    path = tmp_path / "current.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE enrollment_tokens (
              id CHAR(32) PRIMARY KEY, token_hash VARCHAR(64) NOT NULL,
              display_name VARCHAR(128), expires_at DATETIME NOT NULL,
              used_at DATETIME, created_at DATETIME NOT NULL
            );
            CREATE UNIQUE INDEX ix_enrollment_tokens_token_hash ON enrollment_tokens(token_hash);
            CREATE TABLE installations (
              id CHAR(32) PRIMARY KEY, token_hash VARCHAR(64) NOT NULL,
              hostname VARCHAR(255), display_name VARCHAR(128), platform VARCHAR(128),
              client_version VARCHAR(64), last_seen_at DATETIME,
              created_at DATETIME NOT NULL, revoked_at DATETIME
            );
            CREATE UNIQUE INDEX ix_installations_token_hash ON installations(token_hash);
            CREATE TABLE instances (
              id CHAR(32) PRIMARY KEY, installation_id CHAR(32) NOT NULL,
              name VARCHAR(128) NOT NULL, token_hash VARCHAR(64) NOT NULL,
              last_seen_at DATETIME, created_at DATETIME NOT NULL, revoked_at DATETIME,
              FOREIGN KEY(installation_id) REFERENCES installations(id)
            );
            CREATE UNIQUE INDEX ix_instances_token_hash ON instances(token_hash);
            CREATE TABLE audit_events (
              id CHAR(32) PRIMARY KEY, operation VARCHAR(64) NOT NULL,
              instance_id CHAR(32), pane_id VARCHAR(32), input_bytes INTEGER,
              result VARCHAR(32) NOT NULL, error_code VARCHAR(64), created_at DATETIME NOT NULL
            );
            """
        )

    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    await database.initialize()
    try:
        async with database.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0004"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unrecognized_unversioned_database_fails_closed(tmp_path) -> None:
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated_secrets (value TEXT NOT NULL)")

    database = Database(f"sqlite+aiosqlite:///{path}")
    with pytest.raises(UnrecognizedDatabaseSchema, match="unrecognized"):
        await database.initialize()
    await database.dispose()

@pytest.mark.asyncio
async def test_unversioned_database_with_unknown_core_columns_fails_closed(tmp_path) -> None:
    path = tmp_path / "unknown-column.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE enrollment_tokens (
              id CHAR(32) PRIMARY KEY, token_hash VARCHAR(64) NOT NULL,
              raw_secret TEXT, expires_at DATETIME NOT NULL,
              used_at DATETIME, created_at DATETIME NOT NULL
            );
            CREATE TABLE installations (
              id CHAR(32) PRIMARY KEY, token_hash VARCHAR(64) NOT NULL,
              created_at DATETIME NOT NULL, revoked_at DATETIME
            );
            CREATE TABLE instances (
              id CHAR(32) PRIMARY KEY, installation_id CHAR(32) NOT NULL,
              name VARCHAR(128) NOT NULL, token_hash VARCHAR(64) NOT NULL,
              created_at DATETIME NOT NULL, revoked_at DATETIME
            );
            """
        )

    database = Database(f"sqlite+aiosqlite:///{path}")
    with pytest.raises(UnrecognizedDatabaseSchema, match="unrecognized"):
        await database.initialize()
    await database.dispose()


@pytest.mark.asyncio
async def test_corrupt_versioned_head_database_fails_closed(tmp_path) -> None:
    path = tmp_path / "corrupt-head.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    async with database.engine.begin() as connection:
        await connection.execute(text("DROP TABLE auth_tokens"))

    with pytest.raises(UnrecognizedDatabaseSchema, match="unrecognized"):
        await database.initialize()
    await database.dispose()


@pytest.mark.asyncio
async def test_unversioned_database_that_violates_v2_uniqueness_fails_closed(tmp_path) -> None:
    path = tmp_path / "duplicate-token.db"
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE enrollment_tokens (
              id CHAR(32) PRIMARY KEY, token_hash VARCHAR(64) NOT NULL,
              expires_at DATETIME NOT NULL, used_at DATETIME, created_at DATETIME NOT NULL
            );
            CREATE TABLE installations (
              id CHAR(32) PRIMARY KEY, token_hash VARCHAR(64) NOT NULL,
              created_at DATETIME NOT NULL, revoked_at DATETIME
            );
            CREATE TABLE instances (
              id CHAR(32) PRIMARY KEY, installation_id CHAR(32) NOT NULL,
              name VARCHAR(128) NOT NULL, token_hash VARCHAR(64) NOT NULL,
              created_at DATETIME NOT NULL, revoked_at DATETIME
            );
            """
        )
        connection.executemany(
            "INSERT INTO enrollment_tokens "
            "(id, token_hash, expires_at, created_at) VALUES (?, 'duplicate', ?, ?)",
            [(uuid4().hex, now, now), (uuid4().hex, now, now)],
        )

    database = Database(f"sqlite+aiosqlite:///{path}")
    with pytest.raises(UnrecognizedDatabaseSchema, match="unrecognized"):
        await database.initialize()
    await database.dispose()

    with sqlite3.connect(path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(enrollment_tokens)")}
    assert "audit_events" not in table_names
    assert "display_name" not in columns


def test_auth_models_have_metadata_or_ciphertext_only() -> None:
    model_columns = {
        model.__tablename__: {column.name for column in inspect(model).columns}
        for model in (
            AuthenticationState,
            TotpSetup,
            AuthChallenge,
            NativeClient,
            OAuthAuthorization,
            AuthToken,
            AuthAuditEvent,
        )
    }
    forbidden = {
        "token",
        "code",
        "secret",
        "admin_token",
        "access_token",
        "refresh_token",
        "totp_secret",
        "context_plaintext",
    }
    assert all(columns.isdisjoint(forbidden) for columns in model_columns.values())
    assert "token_digest" in model_columns["auth_tokens"]
    assert "authorization_code_digest" in model_columns["oauth_authorizations"]
    assert "source_digest" in model_columns["auth_audit_events"]


def test_base64url_fixture_is_exactly_32_bytes() -> None:
    # Documents the format accepted by Settings without exposing a production key.
    encoded = base64.urlsafe_b64encode(b"m" * 32).decode().rstrip("=")
    assert len(base64.urlsafe_b64decode(encoded + "=")) == 32
