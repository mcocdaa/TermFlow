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
app.add_typer(enrollment_app, name="enrollment")


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
            datetime.now(UTC) + timedelta(minutes=10),
        )
        return raw_token
    finally:
        await database.dispose()


@enrollment_app.command("create")
def create_enrollment() -> None:
    """Create a ten-minute, single-use Installation enrollment token."""

    typer.echo(asyncio.run(_issue_enrollment(_settings())))


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
    )
