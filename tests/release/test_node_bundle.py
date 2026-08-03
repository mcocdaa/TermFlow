from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
BUILD_SCRIPT = REPOSITORY_ROOT / "scripts/release/build_node_bundle.sh"


def test_node_bundle_rejects_a_non_release_tag(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(BUILD_SCRIPT), "0.1.0", str(tmp_path / "output")],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "v-prefixed" in result.stderr
    assert not (tmp_path / "output").exists()


def test_node_bundle_rejects_an_invalid_environment_version_without_a_tag(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [str(BUILD_SCRIPT), str(tmp_path / "output")],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "TERMFLOW_BUILD_VERSION": "latest"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "build version" in result.stderr
    assert not (tmp_path / "output").exists()
