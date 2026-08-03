#!/usr/bin/env python3
"""Render the tag-pinned Linux TermFlow node installer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from check_version import V_PREFIXED_SEMVER

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIRECTORY / "install-node-template.sh"


def render_installer(tag: str, template: Path = TEMPLATE) -> str:
    if not V_PREFIXED_SEMVER.fullmatch(tag):
        raise ValueError(f"Release tag must be a v-prefixed SemVer: {tag}")
    source = template.read_text()
    if source.count("@TAG@") != 1:
        raise ValueError(f"{template}: expected exactly one @TAG@ placeholder")
    return source.replace("@TAG@", tag)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rendered = render_installer(args.tag)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.output.write_text(rendered)
    args.output.chmod(0o755)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
