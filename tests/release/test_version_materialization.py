from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from scripts.release.version_files import (
    INTERNAL_NPM_NAMES,
    materialize_version,
    verify_materialized_version,
)

REPOSITORY_ROOT = Path(__file__).parents[2]
MANAGED_FILES = (
    "package.json",
    "package-lock.json",
    "uv.lock",
    "apps/node/pyproject.toml",
    "apps/node/src/termflow_node/__init__.py",
    "apps/control-plane/pyproject.toml",
    "apps/control-plane/src/termflow_control_plane/__init__.py",
    "packages/protocol/pyproject.toml",
    "apps/clients/web/package.json",
    "apps/clients/tauri/package.json",
    "apps/clients/tauri/src-tauri/Cargo.toml",
    "apps/clients/tauri/src-tauri/Cargo.lock",
    "apps/clients/tauri/src-tauri/tauri.conf.json",
    "apps/clients/tauri/src-tauri/tauri.android.conf.json",
    "apps/clients/tauri/src-tauri/tauri.macos.conf.json",
    "apps/clients/tauri/src-tauri/tauri.ios.conf.json",
    "packages/design-tokens/package.json",
    "packages/client-contracts/package.json",
    "packages/client-core/package.json",
    "packages/client-ui/package.json",
)
LOCAL_LOCK_PACKAGES = {
    "termflow-control-plane",
    "termflow-node",
    "termflow-protocol",
    "termflow-client",
}


def _copy_release_tree(destination: Path) -> None:
    for relative in MANAGED_FILES:
        source = REPOSITORY_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (destination / "apps/control-plane/src/termflow_control_plane/__init__.py").write_text(
        '__version__ = "0.1.0"\n'
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in MANAGED_FILES:
        digest.update(relative.encode())
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def _third_party_toml_blocks(path: Path) -> list[str]:
    blocks = path.read_text().split("[[package]]")
    third_party = [blocks[0]]
    for block in blocks[1:]:
        name = re.search(r'^\s*name = "([^"]+)"', block, re.MULTILINE)
        if name is None or name.group(1) not in LOCAL_LOCK_PACKAGES:
            third_party.append(block)
    return third_party


def _third_party_npm_packages(path: Path) -> dict[str, object]:
    packages = json.loads(path.read_text())["packages"]
    return {
        location: value
        for location, value in packages.items()
        if not (
            location == ""
            or location.startswith("apps/clients/")
            or location.startswith("packages/")
            or location.startswith("node_modules/@termflow/")
        )
    }


def test_materializer_updates_registered_surfaces_and_internal_dependencies(
    tmp_path: Path,
) -> None:
    _copy_release_tree(tmp_path)

    materialize_version(tmp_path, "1.4.0-rc.2")

    assert verify_materialized_version(tmp_path, "1.4.0-rc.2") == []
    assert json.loads((tmp_path / "package.json").read_text())["version"] == (
        "1.4.0-rc.2"
    )
    web = json.loads((tmp_path / "apps/clients/web/package.json").read_text())
    assert web["dependencies"]["@termflow/client-core"] == "1.4.0-rc.2"
    assert web["dependencies"]["@termflow/client-ui"] == "1.4.0-rc.2"
    assert 'name = "termflow-node"\nversion = "1.4.0-rc.2"' in (
        tmp_path / "uv.lock"
    ).read_text()
    assert 'name = "termflow-client"\nversion = "1.4.0-rc.2"' in (
        tmp_path / "apps/clients/tauri/src-tauri/Cargo.lock"
    ).read_text()
    android = json.loads(
        (tmp_path / "apps/clients/tauri/src-tauri/tauri.android.conf.json").read_text()
    )
    assert android["bundle"]["android"]["versionCode"] == 1_004_000
    for platform in ("macos", "ios"):
        config = json.loads(
            (
                tmp_path
                / f"apps/clients/tauri/src-tauri/tauri.{platform}.conf.json"
            ).read_text()
        )
        assert config["version"] == "1.4.0"
        platform_key = "iOS" if platform == "ios" else "macOS"
        assert config["bundle"][platform_key]["bundleVersion"] == "1.4.0"
    assert '__version__ = "1.4.0-rc.2"' in (
        tmp_path / "apps/node/src/termflow_node/__init__.py"
    ).read_text()


def test_materializer_is_idempotent(tmp_path: Path) -> None:
    _copy_release_tree(tmp_path)
    materialize_version(tmp_path, "2.0.0")
    first = _tree_digest(tmp_path)

    materialize_version(tmp_path, "2.0.0")

    assert _tree_digest(tmp_path) == first


def test_materializer_preserves_third_party_locks_and_unregistered_files(
    tmp_path: Path,
) -> None:
    _copy_release_tree(tmp_path)
    fixture = tmp_path / "unregistered.py"
    fixture.write_text('client_version = "0.1.0"\n')
    npm_before = _third_party_npm_packages(tmp_path / "package-lock.json")
    uv_before = _third_party_toml_blocks(tmp_path / "uv.lock")
    cargo_before = _third_party_toml_blocks(
        tmp_path / "apps/clients/tauri/src-tauri/Cargo.lock"
    )

    materialize_version(tmp_path, "3.1.4+build.7")

    assert _third_party_npm_packages(tmp_path / "package-lock.json") == npm_before
    assert _third_party_toml_blocks(tmp_path / "uv.lock") == uv_before
    assert _third_party_toml_blocks(
        tmp_path / "apps/clients/tauri/src-tauri/Cargo.lock"
    ) == cargo_before
    assert fixture.read_text() == 'client_version = "0.1.0"\n'


def test_materializer_fails_when_a_registered_version_field_is_missing(
    tmp_path: Path,
) -> None:
    _copy_release_tree(tmp_path)
    package = tmp_path / "apps/clients/web/package.json"
    data = json.loads(package.read_text())
    del data["version"]
    package.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="apps/clients/web/package.json.*version"):
        materialize_version(tmp_path, "1.2.3")


def test_internal_package_registry_is_explicit() -> None:
    assert INTERNAL_NPM_NAMES == {
        "@termflow/workspace",
        "@termflow/web-client",
        "@termflow/tauri-client",
        "@termflow/design-tokens",
        "@termflow/client-contracts",
        "@termflow/client-core",
        "@termflow/client-ui",
    }
