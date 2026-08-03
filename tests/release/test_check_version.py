from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "release" / "check_version.py"


def _write_release_tree(root: Path, version: str = "0.1.0") -> None:
    (root / "apps/node/src/termflow_node").mkdir(parents=True)
    (root / "apps/control-plane").mkdir(parents=True)
    (root / "packages/protocol").mkdir(parents=True)
    (root / "apps/clients/tauri/src-tauri").mkdir(parents=True)
    (root / "apps/node").mkdir(exist_ok=True)
    (root / "apps/clients/tauri").mkdir(exist_ok=True)

    (root / "package.json").write_text(json.dumps({"version": version}))
    (root / "apps/node/pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    (root / "apps/node/src/termflow_node/__init__.py").write_text(
        f'__version__ = "{version}"\n'
    )
    (root / "apps/control-plane/pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n'
    )
    (root / "packages/protocol/pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n'
    )
    (root / "apps/clients/tauri/package.json").write_text(json.dumps({"version": version}))
    (root / "apps/clients/tauri/src-tauri/Cargo.toml").write_text(
        f'[package]\nversion = "{version}"\n'
    )
    (root / "apps/clients/tauri/src-tauri/tauri.conf.json").write_text(
        json.dumps({"version": version})
    )


def test_checker_prints_agreed_version_for_all_product_surfaces(tmp_path: Path) -> None:
    _write_release_tree(tmp_path)

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "0.1.0\n"


def test_native_package_workflow_is_manual_and_reusable_without_publish_permissions() -> None:
    path = ROOT / ".github/workflows/tauri-packages.yml"
    workflow = yaml.safe_load(path.read_text())

    assert set(workflow[True]) == {"workflow_dispatch", "workflow_call"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "scripts/release/check_version.py" in path.read_text()
