#!/usr/bin/env python3
"""Resolve and optionally materialize the TermFlow build version."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from build_version import resolve_build_version
from version_files import materialize_version, verify_materialized_version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag", default="")
    parser.add_argument("--resolve-only", action="store_true")
    return parser.parse_args()


def _emit_github_outputs(
    *, version: str, tag: str, is_release: bool, is_prerelease: bool
) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a") as output:
        output.write(f"version={version}\n")
        output.write(f"tag={tag}\n")
        output.write(f"is_release={str(is_release).lower()}\n")
        output.write(f"is_prerelease={str(is_prerelease).lower()}\n")


def main() -> int:
    args = parse_args()
    try:
        resolved = resolve_build_version(tag=args.tag or None)
        root = args.root.resolve()
        if not args.resolve_only:
            materialize_version(root, resolved.version)
            errors = verify_materialized_version(root, resolved.version)
            if errors:
                raise ValueError("; ".join(errors))
        _emit_github_outputs(
            version=resolved.version,
            tag=resolved.tag,
            is_release=resolved.is_release,
            is_prerelease=resolved.is_prerelease,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(resolved.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
