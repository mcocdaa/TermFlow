"""Operator commands for the single-process TermFlow Control Plane."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import typer
import uvicorn

from termflow_control_plane.app import create_app
from termflow_control_plane.auth.tokens import hash_token, issue_token
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle

app = typer.Typer(no_args_is_help=True, help="Operate the TermFlow Control Plane.")
enrollment_app = typer.Typer(no_args_is_help=True, help="Manage one-time enrollment tokens.")
auth_app = typer.Typer(no_args_is_help=True, help="Manage local authentication recovery.")
totp_app = typer.Typer(no_args_is_help=True, help="Manage local TOTP recovery.")
app.add_typer(enrollment_app, name="enrollment")
app.add_typer(auth_app, name="auth")
auth_app.add_typer(totp_app, name="totp")


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


async def _issue_enrollment(settings: Settings) -> str:
    database = Database(settings.database_url)
    await database.initialize()
    try:
        repositories = RepositoryBundle(database.session_factory)
        raw_token = issue_token()
        await repositories.enrollments.create(
            hash_token(raw_token),
            datetime.now(UTC)
            + timedelta(seconds=settings.enrollment_token_ttl_seconds),
        )
        return raw_token
    finally:
        await database.dispose()


async def _reset_authentication(settings: Settings) -> int:
    database = Database(settings.database_url)
    await database.initialize()
    try:
        repositories = RepositoryBundle(database.session_factory)
        return await repositories.auth_state.reset_and_increment_epoch()
    finally:
        await database.dispose()


async def _rotate_authentication(settings: Settings) -> int:
    database = Database(settings.database_url)
    await database.initialize()
    try:
        repositories = RepositoryBundle(database.session_factory)
        return await repositories.auth_state.rotate_credentials()
    finally:
        await database.dispose()


@enrollment_app.command("create")
def create_enrollment() -> None:
    """Create a short-lived, single-use Installation enrollment token."""

    typer.echo(asyncio.run(_issue_enrollment(_settings())))


@totp_app.command("reset")
def reset_totp() -> None:
    """Clear TOTP and revoke credentials after an explicit local confirmation."""

    typer.echo(
        "This clears TOTP and revokes all active Web, native, and CLI credentials."
    )
    typer.confirm("Continue with the authentication reset?", abort=True)
    epoch = asyncio.run(_reset_authentication(_settings()))
    typer.echo(f"Authentication reset complete; epoch {epoch} is now active.")


@auth_app.command("rotate")
def rotate_credentials() -> None:
    """Revoke all Web, native, and CLI credentials while keeping TOTP."""

    typer.echo(
        "This revokes all active Web, native, and CLI credentials. "
        "TOTP two-factor configuration is preserved."
    )
    typer.confirm("Continue with the credential rotation?", abort=True)
    epoch = asyncio.run(_rotate_authentication(_settings()))
    typer.echo(f"Credential rotation complete; epoch {epoch} is now active.")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, min=1, max=65535, help="Bind port."),
    workers: int = typer.Option(1, min=1, help="Must remain one in V1."),
) -> None:
    """Run the V1 API and WebSocket relay."""

    if workers != 1:
        raise typer.BadParameter(
            "TermFlow V1 requires exactly one worker for in-memory live state.",
            param_hint="--workers",
        )
    settings = _settings()
    uvicorn.run(
        create_app(settings=settings),
        host=host,
        port=port,
        workers=1,
        ws_max_size=settings.terminal_max_frame_bytes * 8,
    )
