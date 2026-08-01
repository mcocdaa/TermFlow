from pathlib import Path

from termflow_control_plane.cli import app
from typer.testing import CliRunner


def test_enrollment_create_prints_token_once_and_stores_only_hash(tmp_path: Path) -> None:
    database_path = tmp_path / "cli.db"
    result = CliRunner().invoke(
        app,
        ["enrollment", "create"],
        env={
            "TERMFLOW_ADMIN_TOKEN": "admin-token-that-is-long-enough-for-tests",
            "TERMFLOW_DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
        },
    )
    assert result.exit_code == 0, result.output
    token = result.stdout.strip()
    assert len(token) >= 43
    assert result.stdout.count(token) == 1
    assert database_path.read_bytes().count(token.encode()) == 0


def test_serve_rejects_multiple_workers() -> None:
    result = CliRunner().invoke(
        app,
        ["serve", "--workers", "2"],
        env={"TERMFLOW_ADMIN_TOKEN": "admin-token-that-is-long-enough-for-tests"},
    )
    assert result.exit_code != 0
    assert "exactly one worker" in result.output
