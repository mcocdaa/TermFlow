import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "scripts/build-control-plane-image.sh"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    attempts = tmp_path / "attempts"
    attempts.write_text("0")
    docker = binary_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
count="$(cat "$FAKE_DOCKER_ATTEMPTS")"
count="$((count + 1))"
printf '%s' "$count" > "$FAKE_DOCKER_ATTEMPTS"
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if (( count <= FAKE_DOCKER_FAILURES )); then
  exit "$FAKE_DOCKER_FAILURE_STATUS"
fi
"""
    )
    docker.chmod(0o755)
    return binary_dir, attempts


def _run_build(
    tmp_path: Path,
    *,
    failures: int,
    failure_status: int = 71,
    maximum_attempts: str = "3",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    binary_dir, attempts = _fake_docker(tmp_path)
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_ATTEMPTS": str(attempts),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_FAILURES": str(failures),
        "FAKE_DOCKER_FAILURE_STATUS": str(failure_status),
        "TERMFLOW_DOCKER_BUILD_ATTEMPTS": maximum_attempts,
        "TERMFLOW_DOCKER_BUILD_RETRY_DELAY_SECONDS": "0",
    }
    result = subprocess.run(
        [str(BUILD_SCRIPT), "termflow-control-plane:test"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, attempts, log


def test_control_plane_image_build_retries_then_succeeds(tmp_path: Path) -> None:
    result, attempts, log = _run_build(tmp_path, failures=2)

    assert result.returncode == 0
    assert attempts.read_text() == "3"
    commands = log.read_text().splitlines()
    assert len(commands) == 3
    assert all(
        command.startswith(
            "build -f "
            f"{ROOT / 'deploy/Dockerfile.control-plane'} "
            "-t termflow-control-plane:test "
        )
        for command in commands
    )
    assert "retrying in 0s" in result.stderr


def test_control_plane_image_build_preserves_the_final_failure(tmp_path: Path) -> None:
    result, attempts, _ = _run_build(tmp_path, failures=9, maximum_attempts="2")

    assert result.returncode == 71
    assert attempts.read_text() == "2"
    assert "failed after 2 attempts" in result.stderr


@pytest.mark.parametrize("status", [125, 126, 127, 130, 137, 143])
def test_control_plane_image_build_does_not_retry_terminal_statuses(
    tmp_path: Path,
    status: int,
) -> None:
    result, attempts, _ = _run_build(
        tmp_path,
        failures=9,
        failure_status=status,
    )

    assert result.returncode == status
    assert attempts.read_text() == "1"
    assert "will not be retried" in result.stderr


@pytest.mark.parametrize("attempts", ["0", "-1", "abc"])
def test_control_plane_image_build_rejects_invalid_attempt_count(
    tmp_path: Path,
    attempts: str,
) -> None:
    result, recorded_attempts, _ = _run_build(
        tmp_path,
        failures=0,
        maximum_attempts=attempts,
    )

    assert result.returncode == 2
    assert recorded_attempts.read_text() == "0"
    assert "positive integer" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [[], [""], ["termflow-control-plane:test", "unexpected"]],
)
def test_control_plane_image_build_requires_exactly_one_image_argument(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    binary_dir, attempts = _fake_docker(tmp_path)
    result = subprocess.run(
        [str(BUILD_SCRIPT), *arguments],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{binary_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert attempts.read_text() == "0"
    assert "usage:" in result.stderr
