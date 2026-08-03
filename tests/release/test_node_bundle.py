from __future__ import annotations

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
    assert "v-prefixed SemVer" in result.stderr
    assert not (tmp_path / "output").exists()
