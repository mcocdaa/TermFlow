import asyncio
import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from termflow_control_plane.auth.secret_box import AesGcmSecretBox
from termflow_control_plane.auth.service import AuthenticationRejected, AuthenticationService
from termflow_control_plane.auth.totp import match_totp_counter, totp_at, totp_for_counter
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle

RFC_SECRET = b"12345678901234567890"


@pytest.mark.parametrize(
    ("unix_time", "expected"),
    [
        (59, "94287082"),
        (1_111_111_109, "07081804"),
        (1_111_111_111, "14050471"),
        (1_234_567_890, "89005924"),
        (2_000_000_000, "69279037"),
        (20_000_000_000, "65353130"),
    ],
)
def test_rfc6238_sha1_vectors(unix_time: int, expected: str) -> None:
    assert totp_for_counter(RFC_SECRET, unix_time // 30, digits=8) == expected


def test_v1_totp_is_six_digits_and_accepts_only_one_adjacent_step() -> None:
    observed_at = datetime.fromtimestamp(1_234_567_890, UTC)
    current_counter = int(observed_at.timestamp()) // 30

    assert len(totp_at(RFC_SECRET, observed_at)) == 6
    for offset in (-1, 0, 1):
        code = totp_for_counter(RFC_SECRET, current_counter + offset)
        assert match_totp_counter(RFC_SECRET, code, observed_at) == current_counter + offset
    for offset in (-2, 2):
        code = totp_for_counter(RFC_SECRET, current_counter + offset)
        assert match_totp_counter(RFC_SECRET, code, observed_at) is None
    assert match_totp_counter(RFC_SECRET, "１２３４５６", observed_at) is None


def test_primary_token_comparison_supports_valid_non_ascii_utf8_tokens(tmp_path) -> None:
    token = "密" * 16
    settings = Settings(
        admin_token=token,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unicode.db'}",
        allow_insecure_loopback=True,
    )
    service = AuthenticationService(  # repositories are unused by this comparison
        object(),  # type: ignore[arg-type]
        settings,
        secret_box=None,
    )

    assert service.primary_token_matches(token)
    assert not service.primary_token_matches("错" * 16)


@pytest.mark.asyncio
async def test_invalid_primary_login_still_reads_auth_state_before_generic_rejection(
    tmp_path,
) -> None:
    calls = 0

    class AuthStateStub:
        async def get(self):
            nonlocal calls
            calls += 1
            return SimpleNamespace(totp_enabled_at=None)

    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'timing.db'}",
        allow_insecure_loopback=True,
    )
    repositories = SimpleNamespace(auth_state=AuthStateStub())
    service = AuthenticationService(  # only auth_state is used before rejection
        repositories,  # type: ignore[arg-type]
        settings,
        secret_box=None,
    )

    with pytest.raises(AuthenticationRejected):
        await service.begin_web_login("wrong")
    assert calls == 1


@pytest.mark.asyncio
async def test_fresh_totp_counter_can_be_accepted_only_once_concurrently(tmp_path) -> None:
    observed_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    encoded_key = base64.urlsafe_b64encode(b"m" * 32).decode().rstrip("=")
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}",
        allow_insecure_loopback=True,
        totp_master_key=encoded_key,
    )
    database = Database(settings.database_url)
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    box = AesGcmSecretBox(b"m" * 32)
    secret = b"a" * 20
    try:
        await repositories.auth_state.configure_totp(
            box.encrypt(secret, purpose="totp-authenticator"),
            counter=(int(observed_at.timestamp()) // 30) - 1,
            expected_epoch=1,
            expected_generation=0,
            enabled=True,
        )
        service = AuthenticationService(
            repositories,
            settings,
            secret_box=box,
            clock=lambda: observed_at,
        )
        code = totp_at(secret, observed_at)

        results = await asyncio.gather(
            service.verify_fresh_totp(code),
            service.verify_fresh_totp(code),
        )

        assert sorted(results) == [False, True]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_web_login_challenge_is_destroyed_after_five_bad_codes(tmp_path) -> None:
    observed_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    encoded_key = base64.urlsafe_b64encode(b"m" * 32).decode().rstrip("=")
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'attempts.db'}",
        allow_insecure_loopback=True,
        totp_master_key=encoded_key,
    )
    database = Database(settings.database_url)
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    box = AesGcmSecretBox(b"m" * 32)
    secret = b"b" * 20
    try:
        await repositories.auth_state.configure_totp(
            box.encrypt(secret, purpose="totp-authenticator"),
            counter=(int(observed_at.timestamp()) // 30) - 1,
            expected_epoch=1,
            expected_generation=0,
            enabled=True,
        )
        service = AuthenticationService(
            repositories,
            settings,
            secret_box=box,
            clock=lambda: observed_at,
        )
        challenge = await service.begin_web_login(
            "admin-token-that-is-long-enough-for-tests"
        )
        assert challenge is not None

        for _ in range(5):
            assert await service.complete_web_login(challenge.challenge_id, "000000") is False
        assert (
            await service.complete_web_login(
                challenge.challenge_id,
                totp_at(secret, observed_at),
            )
            is False
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_pending_setup_expires_without_enabling_totp(tmp_path) -> None:
    observed_at = [datetime(2026, 8, 2, 12, 0, tzinfo=UTC)]
    encoded_key = base64.urlsafe_b64encode(b"m" * 32).decode().rstrip("=")
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'setup-expiry.db'}",
        allow_insecure_loopback=True,
        totp_master_key=encoded_key,
        totp_setup_ttl_seconds=60,
    )
    database = Database(settings.database_url)
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    secret = b"c" * 20
    service = AuthenticationService(
        repositories,
        settings,
        secret_box=AesGcmSecretBox(b"m" * 32),
        clock=lambda: observed_at[0],
        random_bytes=lambda size: secret if size == 20 else b"",
    )
    try:
        setup = await service.begin_totp_setup(
            "admin-token-that-is-long-enough-for-tests",
            None,
        )
        observed_at[0] += timedelta(seconds=61)

        assert not await service.confirm_totp_setup(
            setup.setup_id,
            totp_at(secret, observed_at[0]),
        )
        assert (await repositories.auth_state.get()).totp_enabled_at is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_totp_setup_and_login_protection_are_independent(tmp_path) -> None:
    observed_at = [datetime(2026, 8, 2, 12, 0, tzinfo=UTC)]
    encoded_key = base64.urlsafe_b64encode(b"m" * 32).decode().rstrip("=")
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'protection-state.db'}",
        allow_insecure_loopback=True,
        totp_master_key=encoded_key,
    )
    database = Database(settings.database_url)
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    first_secret = b"d" * 20
    replacement_secret = b"e" * 20
    secrets = iter((first_secret, replacement_secret))
    service = AuthenticationService(
        repositories,
        settings,
        secret_box=AesGcmSecretBox(b"m" * 32),
        clock=lambda: observed_at[0],
        random_bytes=lambda size: next(secrets) if size == 20 else b"",
    )
    admin_token = "admin-token-that-is-long-enough-for-tests"
    try:
        setup = await service.begin_totp_setup(admin_token, None)
        first_code = totp_at(first_secret, observed_at[0])
        assert await service.confirm_totp_setup(setup.setup_id, first_code)
        assert await service.totp_status() == (True, False, True)

        assert not await service.enable_totp(admin_token, first_code)
        observed_at[0] += timedelta(seconds=30)
        assert await service.enable_totp(
            admin_token,
            totp_at(first_secret, observed_at[0]),
        )
        assert await service.totp_status() == (True, True, True)

        observed_at[0] += timedelta(seconds=30)
        assert await service.disable_totp(
            admin_token,
            totp_at(first_secret, observed_at[0]),
        )
        assert await service.totp_status() == (True, False, True)

        with pytest.raises(AuthenticationRejected):
            await service.begin_totp_setup(admin_token, None)
        observed_at[0] += timedelta(seconds=30)
        replacement = await service.begin_totp_setup(
            admin_token,
            totp_at(first_secret, observed_at[0]),
        )
        observed_at[0] += timedelta(seconds=30)
        assert await service.confirm_totp_setup(
            replacement.setup_id,
            totp_at(replacement_secret, observed_at[0]),
        )
        assert await service.totp_status() == (True, False, True)
    finally:
        await database.dispose()
