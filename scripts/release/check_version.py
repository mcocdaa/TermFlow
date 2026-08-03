#!/usr/bin/env python3
"""Verify that every materialized TermFlow surface has one version."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from build_version import (
    V_PREFIXED_VERSION_PATTERN,
    resolve_build_version,
    validate_version,
)
from version_files import verify_materialized_version

# Compatibility for the installer renderer while it transitions to build_version.py.
V_PREFIXED_SEMVER = V_PREFIXED_VERSION_PATTERN


def configured_version(root: Path) -> str:
    package = json.loads((root / "package.json").read_text())
    version = package.get("version")
    if not isinstance(version, str):
        raise ValueError(f"{root / 'package.json'}: version must be a string")
    validate_version(version)
    errors = verify_materialized_version(root, version)
    if errors:
        raise ValueError("; ".join(errors))
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
            expected = resolve_build_version(tag=args.tag, environment={}).version
            if version != expected:
                raise ValueError(
                    f"materialized version {version} disagrees with release tag {args.tag}"
                )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
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
