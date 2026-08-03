from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARCHIVER = ROOT / "scripts/release/archive_control_plane_image.sh"


def _fake_docker(tmp_path: Path) -> Path:
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1 $2" in
  "image inspect")
    count="$(cat "$FAKE_DOCKER_INSPECT_COUNT")"
    count="$((count + 1))"
    printf '%s' "$count" > "$FAKE_DOCKER_INSPECT_COUNT"
    if (( count == 1 )); then
      printf '%s\n' "$FAKE_DOCKER_BEFORE_ID"
    else
      printf '%s\n' "$FAKE_DOCKER_AFTER_ID"
    fi
    ;;
  "save --output")
    printf 'docker image tar' > "$3"
    ;;
  "image rm"|"load --input") ;;
  *) exit 64 ;;
esac
"""
    )
    docker.chmod(0o755)
    return binary


def _run(
    tmp_path: Path,
    *,
    after_id: str = "sha256:same",
) -> subprocess.CompletedProcess[str]:
    binary = _fake_docker(tmp_path)
    log = tmp_path / "docker.log"
    count = tmp_path / "inspect-count"
    count.write_text("0")
    return subprocess.run(
        [str(ARCHIVER), "termflow:test", str(tmp_path / "termflow.tar")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_INSPECT_COUNT": str(count),
            "FAKE_DOCKER_BEFORE_ID": "sha256:same",
            "FAKE_DOCKER_AFTER_ID": after_id,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_archiver_saves_unloads_reloads_and_preserves_identity(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "termflow.tar").read_text() == "docker image tar"
    assert (tmp_path / "docker.log").read_text().splitlines() == [
        "image inspect --format {{.Id}} termflow:test",
        f"save --output {tmp_path / 'termflow.tar.tmp'} termflow:test",
        "image rm termflow:test",
        f"load --input {tmp_path / 'termflow.tar.tmp'}",
        "image inspect --format {{.Id}} termflow:test",
    ]


def test_archiver_rejects_a_changed_reloaded_image(tmp_path: Path) -> None:
    result = _run(tmp_path, after_id="sha256:different")

    assert result.returncode == 1
    assert "image identity changed" in result.stderr
    assert not (tmp_path / "termflow.tar").exists()
