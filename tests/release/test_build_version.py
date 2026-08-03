from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release.build_version import (
    DEFAULT_BUILD_VERSION,
    BuildVersion,
    resolve_build_version,
    validate_version,
)
from scripts.release.version_files import (
    CARGO_LOCK,
    CARGO_MANIFEST,
    NPM_MANIFESTS,
    PACKAGE_LOCK,
    PYPROJECTS,
    PYTHON_VERSION_MODULES,
    TAURI_CONFIG,
    UV_LOCK,
    verify_materialized_version,
)

REPOSITORY_ROOT = Path(__file__).parents[2]
PREPARE_VERSION = REPOSITORY_ROOT / "scripts/release/prepare_version.py"


def _copy_managed_tree(destination: Path) -> None:
    paths = (
        *PYPROJECTS,
        *PYTHON_VERSION_MODULES,
        *NPM_MANIFESTS,
        PACKAGE_LOCK,
        UV_LOCK,
        CARGO_MANIFEST,
        CARGO_LOCK,
        TAURI_CONFIG,
    )
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / relative, target)
    (destination / "apps/control-plane/src/termflow_control_plane/__init__.py").write_text(
        '__version__ = "0.1.0"\n'
    )


def test_tag_wins_over_environment() -> None:
    resolved = resolve_build_version(
        tag="v1.2.3-rc.1",
        environment={"TERMFLOW_BUILD_VERSION": "9.9.9"},
    )

    assert resolved == BuildVersion(
        version="1.2.3-rc.1",
        tag="v1.2.3-rc.1",
        is_release=True,
    )


def test_environment_wins_without_tag() -> None:
    resolved = resolve_build_version(
        tag=None,
        environment={"TERMFLOW_BUILD_VERSION": "2.3.4"},
    )

    assert resolved == BuildVersion("2.3.4", "v2.3.4", False)


def test_default_is_used_without_tag_or_environment() -> None:
    assert DEFAULT_BUILD_VERSION == "0.0.0-dev.0"
    assert resolve_build_version(tag=None, environment={}) == BuildVersion(
        "0.0.0-dev.0",
        "v0.0.0-dev.0",
        False,
    )
    assert resolve_build_version(
        tag="",
        environment={"TERMFLOW_BUILD_VERSION": ""},
    ) == BuildVersion("0.0.0-dev.0", "v0.0.0-dev.0", False)


@pytest.mark.parametrize(
    "version",
    [
        "1.2.3",
        "1.2.3-dev.4",
        "1.2.3-alpha.1",
        "1.2.3-beta.2",
        "1.2.3-rc.3",
        "1.2.3+build.7",
        "1.2.3-rc.3+build.7",
    ],
)
def test_cross_ecosystem_versions_are_accepted(version: str) -> None:
    assert validate_version(version) == version


@pytest.mark.parametrize(
    "tag",
    ["v1", "1.2.3", "v1.2.3-foo.1", "latest", " v1.2.3", "v1.2.3 "],
)
def test_invalid_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ValueError, match="v-prefixed"):
        resolve_build_version(tag=tag, environment={})


@pytest.mark.parametrize(
    "version",
    ["v1.2.3", "1", "1.2", "1.2.3-foo.1", "latest", " 1.2.3", "1.2.3 "],
)
def test_invalid_environment_versions_do_not_fall_back(version: str) -> None:
    with pytest.raises(ValueError, match="build version"):
        resolve_build_version(
            tag=None,
            environment={"TERMFLOW_BUILD_VERSION": version},
        )


def test_prepare_cli_resolves_a_tag_without_writing_files(tmp_path: Path) -> None:
    _copy_managed_tree(tmp_path)
    before = (tmp_path / "package.json").read_bytes()
    github_output = tmp_path / "github-output"

    result = subprocess.run(
        [
            sys.executable,
            str(PREPARE_VERSION),
            "--root",
            str(tmp_path),
            "--tag",
            "v2.0.0",
            "--resolve-only",
        ],
        env={**os.environ, "GITHUB_OUTPUT": str(github_output)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "2.0.0\n"
    assert github_output.read_text() == (
        "version=2.0.0\ntag=v2.0.0\nis_release=true\n"
    )
    assert (tmp_path / "package.json").read_bytes() == before


def test_prepare_cli_materializes_an_environment_version(tmp_path: Path) -> None:
    _copy_managed_tree(tmp_path)

    result = subprocess.run(
        [sys.executable, str(PREPARE_VERSION), "--root", str(tmp_path)],
        env={**os.environ, "TERMFLOW_BUILD_VERSION": "3.1.0"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "3.1.0\n"
    assert verify_materialized_version(tmp_path, "3.1.0") == []


def test_prepare_cli_materializes_the_fixed_default(tmp_path: Path) -> None:
    _copy_managed_tree(tmp_path)
    environment = os.environ.copy()
    environment.pop("TERMFLOW_BUILD_VERSION", None)

    result = subprocess.run(
        [sys.executable, str(PREPARE_VERSION), "--root", str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "0.0.0-dev.0\n"
    assert verify_materialized_version(tmp_path, "0.0.0-dev.0") == []


def test_prepare_cli_rejects_an_invalid_environment_version(tmp_path: Path) -> None:
    _copy_managed_tree(tmp_path)

    result = subprocess.run(
        [sys.executable, str(PREPARE_VERSION), "--root", str(tmp_path)],
        env={**os.environ, "TERMFLOW_BUILD_VERSION": "latest"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "build version" in result.stderr
