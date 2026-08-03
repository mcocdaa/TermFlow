#!/usr/bin/env python3
"""Validate that every TermFlow product surface has one release version."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

V_PREFIXED_SEMVER = re.compile(
    r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)


def _json_version(path: Path) -> str:
    value = json.loads(path.read_text())["version"]
    if not isinstance(value, str):
        raise ValueError(f"{path}: version must be a string")
    return value


def _toml_version(path: Path) -> str:
    value = tomllib.loads(path.read_text())["project"]["version"]
    if not isinstance(value, str):
        raise ValueError(f"{path}: project.version must be a string")
    return value


def _cargo_version(path: Path) -> str:
    value = tomllib.loads(path.read_text())["package"]["version"]
    if not isinstance(value, str):
        raise ValueError(f"{path}: package.version must be a string")
    return value


def _node_module_version(path: Path) -> str:
    module = ast.parse(path.read_text(), filename=str(path))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            return statement.value.value
    raise ValueError(f"{path}: missing string __version__ assignment")


VERSION_READERS: dict[str, Callable[[Path], str]] = {
    "package.json": _json_version,
    "apps/node/pyproject.toml": _toml_version,
    "apps/node/src/termflow_node/__init__.py": _node_module_version,
    "apps/control-plane/pyproject.toml": _toml_version,
    "packages/protocol/pyproject.toml": _toml_version,
    "apps/clients/tauri/package.json": _json_version,
    "apps/clients/tauri/src-tauri/Cargo.toml": _cargo_version,
    "apps/clients/tauri/src-tauri/tauri.conf.json": _json_version,
}


def load_versions(root: Path) -> dict[str, str]:
    return {
        relative_path: reader(root / relative_path)
        for relative_path, reader in VERSION_READERS.items()
    }


def configured_version(root: Path) -> str:
    versions = load_versions(root)
    distinct = set(versions.values())
    if len(distinct) != 1:
        details = ", ".join(f"{path}={version}" for path, version in versions.items())
        raise ValueError(f"Configured product versions disagree: {details}")
    return distinct.pop()


def validate_tag(version: str, tag: str) -> str:
    if not V_PREFIXED_SEMVER.fullmatch(tag):
        raise ValueError(f"Release tag must be a v-prefixed SemVer: {tag}")
    if tag[1:] != version:
        raise ValueError(f"Release tag {tag} disagrees with configured version {version}")
    return version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        version = configured_version(args.root.resolve())
        if args.tag is not None:
            validate_tag(version, args.tag)
    except (KeyError, OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(version)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a") as output:
            output.write(f"version={version}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
