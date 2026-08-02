import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from termflow_control_plane.auth.secret_box import AesGcmSecretBox
from termflow_control_plane.cli import app
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle
from typer.testing import CliRunner

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"


async def _seed_reset_database(database_url: str) -> tuple[UUID, str]:
    database = Database(database_url)
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    native_client_id = uuid4()
    public_jwk = '{"crv":"P-256","kty":"EC","x":"public-x","y":"public-y"}'
    box = AesGcmSecretBox(b"r" * 32, key_version=1)
    now = datetime.now(UTC)
    try:
        await repositories.auth_state.enable_totp(
            box.encrypt(b"authenticator-secret", purpose="totp-authenticator"),
            counter=42,
        )
        await repositories.totp_setups.create(
            box.encrypt(b"pending-secret", purpose="totp-setup"),
            expires_at=now + timedelta(minutes=10),
            epoch=1,
        )
        await repositories.auth_challenges.create(
            "web_session_totp",
            box.encrypt(b"pending-challenge", purpose="web-login-challenge"),
            expires_at=now + timedelta(minutes=5),
            epoch=1,
        )
        await repositories.native_clients.create(
            client_id=native_client_id,
            display_name="Desktop C",
            public_jwk=public_jwk,
            key_thumbprint="native-public-key-thumbprint",
            platform="linux",
            scopes=("terminal.read", "terminal.write"),
        )
        await repositories.oauth_authorizations.create(
            transaction_secret="pending-oauth-transaction",
            client_id=native_client_id,
            redirect_uri="termflow://auth/callback",
            request_state="request-state-long-enough",
            scopes=("terminal.read",),
            pkce_challenge="pkce-challenge",
            expires_at=now + timedelta(minutes=5),
            epoch=1,
        )
        await repositories.auth_tokens.issue(
            "native-access-secret",
            kind="access",
            scopes=("terminal.read",),
            key_thumbprint="native-public-key-thumbprint",
            expires_at=now + timedelta(minutes=10),
            epoch=1,
            client_id=native_client_id,
        )
        await repositories.auth_tokens.issue(
            "cli-access-secret",
            kind="cli",
            scopes=("terminal.read",),
            key_thumbprint=None,
            expires_at=now + timedelta(minutes=15),
            epoch=1,
        )
        return native_client_id, public_jwk
    finally:
        await database.dispose()


def test_enrollment_create_prints_token_once_and_stores_only_hash(tmp_path: Path) -> None:
    database_path = tmp_path / "cli.db"
    result = CliRunner().invoke(
        app,
        ["enrollment", "create"],
        env={
            "TERMFLOW_ADMIN_TOKEN": ADMIN_TOKEN,
            "TERMFLOW_DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
            "TERMFLOW_ENROLLMENT_TOKEN_TTL_SECONDS": "17",
        },
    )
    assert result.exit_code == 0, result.output
    token = result.stdout.strip()
    assert len(token) >= 43
    assert result.stdout.count(token) == 1
    assert database_path.read_bytes().count(token.encode()) == 0
    with sqlite3.connect(database_path) as connection:
        created_at, expires_at = connection.execute(
            "SELECT created_at, expires_at FROM enrollment_tokens"
        ).fetchone()
    assert 16 <= (
        datetime.fromisoformat(expires_at) - datetime.fromisoformat(created_at)
    ).total_seconds() <= 18


def test_serve_rejects_multiple_workers() -> None:
    result = CliRunner().invoke(
        app,
        ["serve", "--workers", "2"],
        env={"TERMFLOW_ADMIN_TOKEN": ADMIN_TOKEN},
    )
    assert result.exit_code != 0
    assert "exactly one worker" in result.output


def test_auth_totp_reset_aborts_without_explicit_interactive_confirmation(tmp_path: Path) -> None:
    database_path = tmp_path / "reset-abort.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    asyncio.run(_seed_reset_database(database_url))
    runner = CliRunner()
    env = {
        "TERMFLOW_ADMIN_TOKEN": ADMIN_TOKEN,
        "TERMFLOW_DATABASE_URL": database_url,
    }

    declined = runner.invoke(app, ["auth", "totp", "reset"], input="n\n", env=env)
    eof = runner.invoke(app, ["auth", "totp", "reset"], input="", env=env)
    help_result = runner.invoke(app, ["auth", "totp", "reset", "--help"], env=env)

    assert declined.exit_code != 0
    assert eof.exit_code != 0
    assert "--yes" not in help_result.output
    with sqlite3.connect(database_path) as connection:
        epoch, totp_ciphertext = connection.execute(
            "SELECT epoch, totp_ciphertext FROM authentication_state WHERE id = 1"
        ).fetchone()
    assert epoch == 1
    assert totp_ciphertext is not None


def test_auth_totp_reset_atomically_revokes_credentials_and_preserves_native_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reset-confirm.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    native_client_id, public_jwk = asyncio.run(_seed_reset_database(database_url))
    result = CliRunner().invoke(
        app,
        ["auth", "totp", "reset"],
        input="y\n",
        env={
            "TERMFLOW_ADMIN_TOKEN": ADMIN_TOKEN,
            "TERMFLOW_DATABASE_URL": database_url,
        },
    )

    assert result.exit_code == 0, result.output
    assert "epoch 2" in result.output.lower()
    for secret in (
        ADMIN_TOKEN,
        "authenticator-secret",
        "pending-secret",
        "pending-challenge",
        "pending-oauth-transaction",
        "native-access-secret",
        "cli-access-secret",
    ):
        assert secret not in result.output

    with sqlite3.connect(database_path) as connection:
        epoch, ciphertext, last_counter, generation = connection.execute(
            "SELECT epoch, totp_ciphertext, totp_last_accepted_counter, totp_generation "
            "FROM authentication_state WHERE id = 1"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM totp_setups").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM auth_challenges WHERE completed_at IS NULL"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM auth_tokens WHERE revoked_at IS NULL"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM oauth_authorizations WHERE consumed_at IS NULL"
        ).fetchone()[0] == 0
        stored_jwk, revoked_at = connection.execute(
            "SELECT public_jwk, revoked_at FROM native_clients WHERE id = ?",
            (native_client_id.hex,),
        ).fetchone()
        audit = connection.execute(
            "SELECT operation, result, source_digest, client_id, error_code "
            "FROM auth_audit_events"
        ).fetchall()

    assert (epoch, ciphertext, last_counter, generation) == (2, None, None, 2)
    assert stored_jwk == public_jwk
    assert revoked_at is None
    assert len(audit) == 1
    operation, audit_result, source_digest, client_id, error_code = audit[0]
    assert (operation, audit_result, client_id, error_code) == (
        "auth.reset",
        "reset",
        None,
        None,
    )
    assert len(source_digest) == 64
