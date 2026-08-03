from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from scripts.release.version_files import (
    ANDROID_CONFIG,
    CARGO_LOCK,
    CARGO_MANIFEST,
    IOS_CONFIG,
    MACOS_CONFIG,
    NPM_MANIFESTS,
    PACKAGE_LOCK,
    PYPROJECTS,
    PYTHON_VERSION_MODULES,
    TAURI_CONFIG,
    UV_LOCK,
    materialize_version,
)

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "release" / "check_version.py"


def _write_release_tree(root: Path, version: str = "2.0.0") -> None:
    paths = (
        *PYPROJECTS,
        *PYTHON_VERSION_MODULES,
        *NPM_MANIFESTS,
        PACKAGE_LOCK,
        UV_LOCK,
        CARGO_MANIFEST,
        CARGO_LOCK,
        TAURI_CONFIG,
        ANDROID_CONFIG,
        MACOS_CONFIG,
        IOS_CONFIG,
    )
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (root / "apps/control-plane/src/termflow_control_plane/__init__.py").write_text(
        '__version__ = "0.1.0"\n'
    )
    materialize_version(root, version)


def test_checker_prints_agreed_version_for_all_product_surfaces(tmp_path: Path) -> None:
    _write_release_tree(tmp_path)

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "2.0.0\n"


def test_checker_accepts_a_matching_materialized_tag(tmp_path: Path) -> None:
    _write_release_tree(tmp_path, "2.1.0-rc.1")

    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--root",
            str(tmp_path),
            "--tag",
            "v2.1.0-rc.1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "2.1.0-rc.1\n"


def test_checker_rejects_an_internal_dependency_version_mismatch(tmp_path: Path) -> None:
    _write_release_tree(tmp_path)
    web_path = tmp_path / "apps/clients/web/package.json"
    web = json.loads(web_path.read_text())
    web["dependencies"]["@termflow/client-core"] = "9.9.9"
    web_path.write_text(json.dumps(web))

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "apps/clients/web/package.json" in result.stderr


def test_native_package_workflow_is_manual_and_reusable_without_publish_permissions() -> None:
    path = ROOT / ".github/workflows/tauri-packages.yml"
    workflow = yaml.safe_load(path.read_text())

    assert set(workflow[True]) == {"workflow_dispatch", "workflow_call"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "scripts/release/prepare_version.py" in path.read_text()
