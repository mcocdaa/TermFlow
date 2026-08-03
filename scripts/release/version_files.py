#!/usr/bin/env python3
"""Materialize a resolved version into registered TermFlow product files."""

from __future__ import annotations

import json
import re
from pathlib import Path

PYPROJECTS = (
    Path("apps/node/pyproject.toml"),
    Path("apps/control-plane/pyproject.toml"),
    Path("packages/protocol/pyproject.toml"),
)
PYTHON_VERSION_MODULES = (
    Path("apps/node/src/termflow_node/__init__.py"),
    Path("apps/control-plane/src/termflow_control_plane/__init__.py"),
)
NPM_MANIFESTS = (
    Path("package.json"),
    Path("apps/clients/web/package.json"),
    Path("apps/clients/tauri/package.json"),
    Path("packages/design-tokens/package.json"),
    Path("packages/client-contracts/package.json"),
    Path("packages/client-core/package.json"),
    Path("packages/client-ui/package.json"),
)
INTERNAL_NPM_NAMES = frozenset(
    {
        "@termflow/workspace",
        "@termflow/web-client",
        "@termflow/tauri-client",
        "@termflow/design-tokens",
        "@termflow/client-contracts",
        "@termflow/client-core",
        "@termflow/client-ui",
    }
)
UV_PACKAGES = frozenset(
    {"termflow-control-plane", "termflow-node", "termflow-protocol"}
)
CARGO_PACKAGES = frozenset({"termflow-client"})
DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)

PACKAGE_LOCK = Path("package-lock.json")
UV_LOCK = Path("uv.lock")
CARGO_MANIFEST = Path("apps/clients/tauri/src-tauri/Cargo.toml")
CARGO_LOCK = Path("apps/clients/tauri/src-tauri/Cargo.lock")
TAURI_CONFIG = Path("apps/clients/tauri/src-tauri/tauri.conf.json")
ANDROID_CONFIG = Path("apps/clients/tauri/src-tauri/tauri.android.conf.json")
MACOS_CONFIG = Path("apps/clients/tauri/src-tauri/tauri.macos.conf.json")
IOS_CONFIG = Path("apps/clients/tauri/src-tauri/tauri.ios.conf.json")

_PYTHON_VERSION = re.compile(
    r'^(?P<prefix>__version__\s*=\s*")[^"]+(?P<suffix>"\s*)$', re.MULTILINE
)
_LOCK_NAME = re.compile(r'^\s*name = "([^"]+)"', re.MULTILINE)
_LOCK_VERSION = re.compile(
    r'^(?P<prefix>\s*version = ")[^"]+(?P<suffix>"\s*)$', re.MULTILINE
)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _update_internal_dependencies(data: dict[str, object], version: str) -> None:
    for section_name in DEPENDENCY_SECTIONS:
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        for dependency in INTERNAL_NPM_NAMES & section.keys():
            section[dependency] = version


def _materialize_npm_manifest(path: Path, version: str) -> None:
    data = _read_json(path)
    name = data.get("name")
    if name not in INTERNAL_NPM_NAMES:
        raise ValueError(f"{path}: unregistered TermFlow package name {name!r}")
    if not isinstance(data.get("version"), str):
        raise ValueError(f"{path}: missing string version")
    data["version"] = version
    _update_internal_dependencies(data, version)
    _write_json(path, data)


def _materialize_package_lock(path: Path, version: str) -> None:
    data = _read_json(path)
    if not isinstance(data.get("version"), str):
        raise ValueError(f"{path}: missing top-level version")
    packages = data.get("packages")
    if not isinstance(packages, dict):
        raise ValueError(f"{path}: missing packages object")
    data["version"] = version
    found: set[str] = set()
    for location, raw_package in packages.items():
        if not isinstance(raw_package, dict):
            continue
        name = raw_package.get("name")
        if location == "":
            name = "@termflow/workspace"
        if name not in INTERNAL_NPM_NAMES:
            continue
        if not isinstance(raw_package.get("version"), str):
            raise ValueError(f"{path}: {location!r} is missing a string version")
        raw_package["version"] = version
        _update_internal_dependencies(raw_package, version)
        found.add(name)
    if found != INTERNAL_NPM_NAMES:
        missing = ", ".join(sorted(INTERNAL_NPM_NAMES - found))
        raise ValueError(f"{path}: missing workspace packages: {missing}")
    _write_json(path, data)


def _section_bounds(lines: list[str], section: str, path: Path) -> tuple[int, int]:
    header = f"[{section}]"
    starts = [index for index, line in enumerate(lines) if line.strip() == header]
    if len(starts) != 1:
        raise ValueError(f"{path}: expected exactly one {header} section")
    start = starts[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].lstrip().startswith("[")),
        len(lines),
    )
    return start, end


def _read_toml_version(path: Path, section: str) -> str:
    lines = path.read_text().splitlines(keepends=True)
    start, end = _section_bounds(lines, section, path)
    matches = [
        match
        for line in lines[start:end]
        if (match := re.fullmatch(r'\s*version\s*=\s*"([^"]+)"\s*', line))
    ]
    if len(matches) != 1:
        raise ValueError(f"{path}: expected one version field in [{section}]")
    return matches[0].group(1)


def _materialize_toml_version(path: Path, section: str, version: str) -> None:
    lines = path.read_text().splitlines(keepends=True)
    start, end = _section_bounds(lines, section, path)
    indexes = [
        index
        for index in range(start, end)
        if re.fullmatch(r'\s*version\s*=\s*"[^"]+"\s*', lines[index])
    ]
    if len(indexes) != 1:
        raise ValueError(f"{path}: expected one version field in [{section}]")
    index = indexes[0]
    indentation = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    newline = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f'{indentation}version = "{version}"{newline}'
    path.write_text("".join(lines))


def _read_python_version(path: Path) -> str:
    matches = list(_PYTHON_VERSION.finditer(path.read_text()))
    if len(matches) != 1:
        raise ValueError(f"{path}: expected exactly one __version__ assignment")
    match = matches[0]
    line = match.group(0)
    return line.split('"', 2)[1]


def _materialize_python_version(path: Path, version: str) -> None:
    source = path.read_text()
    updated, count = _PYTHON_VERSION.subn(
        rf'\g<prefix>{version}\g<suffix>', source
    )
    if count != 1:
        raise ValueError(f"{path}: expected exactly one __version__ assignment")
    path.write_text(updated)


def _version_core(version: str) -> str:
    return version.split("+", 1)[0].split("-", 1)[0]


def _android_version_code(version: str) -> int:
    major, minor, patch = (int(component) for component in _version_core(version).split("."))
    version_code = major * 1_000_000 + minor * 1_000 + patch
    if not 1 <= version_code <= 2_100_000_000 or minor > 99 or patch > 99:
        raise ValueError(f"{version}: outside the supported mobile bundle range")
    return version_code


def _materialize_android_config(path: Path, version: str) -> None:
    data = _read_json(path)
    bundle = data.get("bundle")
    if not isinstance(bundle, dict):
        raise ValueError(f"{path}: missing bundle object")
    android = bundle.get("android")
    if not isinstance(android, dict) or not isinstance(android.get("versionCode"), int):
        raise ValueError(f"{path}: missing integer bundle.android.versionCode")
    android["versionCode"] = _android_version_code(version)
    _write_json(path, data)


def _materialize_apple_config(path: Path, platform: str, version: str) -> None:
    data = _read_json(path)
    if not isinstance(data.get("version"), str):
        raise ValueError(f"{path}: missing string version")
    bundle = data.get("bundle")
    if not isinstance(bundle, dict):
        raise ValueError(f"{path}: missing bundle object")
    platform_config = bundle.get(platform)
    if not isinstance(platform_config, dict) or not isinstance(
        platform_config.get("bundleVersion"), str
    ):
        raise ValueError(f"{path}: missing string bundle.{platform}.bundleVersion")
    core = _version_core(version)
    data["version"] = core
    platform_config["bundleVersion"] = core
    _write_json(path, data)


def _lock_versions(path: Path, package_names: frozenset[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for block in path.read_text().split("[[package]]")[1:]:
        name_match = _LOCK_NAME.search(block)
        if name_match is None or name_match.group(1) not in package_names:
            continue
        name = name_match.group(1)
        version_match = _LOCK_VERSION.search(block)
        if version_match is None or name in result:
            raise ValueError(f"{path}: expected one versioned package block for {name}")
        result[name] = version_match.group(0).split('"', 2)[1]
    if result.keys() != package_names:
        missing = ", ".join(sorted(package_names - result.keys()))
        raise ValueError(f"{path}: missing package blocks: {missing}")
    return result


def _materialize_lock_versions(
    path: Path,
    package_names: frozenset[str],
    version: str,
) -> None:
    pieces = path.read_text().split("[[package]]")
    seen: set[str] = set()
    for index, block in enumerate(pieces[1:], start=1):
        name_match = _LOCK_NAME.search(block)
        if name_match is None or name_match.group(1) not in package_names:
            continue
        name = name_match.group(1)
        updated, count = _LOCK_VERSION.subn(
            rf'\g<prefix>{version}\g<suffix>', block, count=1
        )
        if count != 1 or name in seen:
            raise ValueError(f"{path}: expected one versioned package block for {name}")
        pieces[index] = updated
        seen.add(name)
    if seen != package_names:
        missing = ", ".join(sorted(package_names - seen))
        raise ValueError(f"{path}: missing package blocks: {missing}")
    path.write_text("[[package]]".join(pieces))


def materialize_version(root: Path, version: str) -> None:
    """Write ``version`` only to registered TermFlow version surfaces."""

    for relative in PYPROJECTS:
        _materialize_toml_version(root / relative, "project", version)
    for relative in PYTHON_VERSION_MODULES:
        _materialize_python_version(root / relative, version)
    for relative in NPM_MANIFESTS:
        _materialize_npm_manifest(root / relative, version)
    _materialize_package_lock(root / PACKAGE_LOCK, version)
    _materialize_toml_version(root / CARGO_MANIFEST, "package", version)
    tauri = _read_json(root / TAURI_CONFIG)
    if not isinstance(tauri.get("version"), str):
        raise ValueError(f"{root / TAURI_CONFIG}: missing string version")
    tauri["version"] = version
    _write_json(root / TAURI_CONFIG, tauri)
    _materialize_android_config(root / ANDROID_CONFIG, version)
    _materialize_apple_config(root / MACOS_CONFIG, "macOS", version)
    _materialize_apple_config(root / IOS_CONFIG, "iOS", version)
    _materialize_lock_versions(root / UV_LOCK, UV_PACKAGES, version)
    _materialize_lock_versions(root / CARGO_LOCK, CARGO_PACKAGES, version)


def verify_materialized_version(root: Path, expected: str) -> list[str]:
    """Return descriptions of registered surfaces that disagree with ``expected``."""

    errors: list[str] = []

    def require(path: Path, actual: str) -> None:
        if actual != expected:
            errors.append(f"{path}: expected {expected}, found {actual}")

    for relative in PYPROJECTS:
        require(relative, _read_toml_version(root / relative, "project"))
    for relative in PYTHON_VERSION_MODULES:
        require(relative, _read_python_version(root / relative))
    for relative in NPM_MANIFESTS:
        data = _read_json(root / relative)
        require(relative, str(data.get("version")))
        for section_name in DEPENDENCY_SECTIONS:
            section = data.get(section_name)
            if not isinstance(section, dict):
                continue
            for name in INTERNAL_NPM_NAMES & section.keys():
                require(relative, str(section[name]))

    package_lock = _read_json(root / PACKAGE_LOCK)
    require(PACKAGE_LOCK, str(package_lock.get("version")))
    packages = package_lock.get("packages")
    if not isinstance(packages, dict):
        raise ValueError(f"{root / PACKAGE_LOCK}: missing packages object")
    for location, raw_package in packages.items():
        if not isinstance(raw_package, dict):
            continue
        name = "@termflow/workspace" if location == "" else raw_package.get("name")
        if name in INTERNAL_NPM_NAMES:
            require(PACKAGE_LOCK, str(raw_package.get("version")))
            for section_name in DEPENDENCY_SECTIONS:
                section = raw_package.get(section_name)
                if not isinstance(section, dict):
                    continue
                for dependency in INTERNAL_NPM_NAMES & section.keys():
                    require(PACKAGE_LOCK, str(section[dependency]))

    require(CARGO_MANIFEST, _read_toml_version(root / CARGO_MANIFEST, "package"))
    require(TAURI_CONFIG, str(_read_json(root / TAURI_CONFIG).get("version")))
    android = _read_json(root / ANDROID_CONFIG)
    android_bundle = android.get("bundle")
    android_config = (
        android_bundle.get("android") if isinstance(android_bundle, dict) else None
    )
    android_code = (
        android_config.get("versionCode") if isinstance(android_config, dict) else None
    )
    expected_android_code = _android_version_code(expected)
    if android_code != expected_android_code:
        errors.append(
            f"{ANDROID_CONFIG}: expected versionCode {expected_android_code}, "
            f"found {android_code}"
        )
    apple_core = _version_core(expected)
    for relative, platform in ((MACOS_CONFIG, "macOS"), (IOS_CONFIG, "iOS")):
        config = _read_json(root / relative)
        bundle = config.get("bundle")
        platform_config = bundle.get(platform) if isinstance(bundle, dict) else None
        bundle_version = (
            platform_config.get("bundleVersion")
            if isinstance(platform_config, dict)
            else None
        )
        if str(config.get("version")) != apple_core:
            errors.append(
                f"{relative}: expected platform version {apple_core}, "
                f"found {config.get('version')}"
            )
        if str(bundle_version) != apple_core:
            errors.append(
                f"{relative}: expected bundleVersion {apple_core}, found {bundle_version}"
            )
    for name, actual in _lock_versions(root / UV_LOCK, UV_PACKAGES).items():
        require(Path(f"{UV_LOCK}:{name}"), actual)
    for name, actual in _lock_versions(root / CARGO_LOCK, CARGO_PACKAGES).items():
        require(Path(f"{CARGO_LOCK}:{name}"), actual)
    return errors
