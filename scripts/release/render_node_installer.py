#!/usr/bin/env python3
"""Render the tag-pinned Linux TermFlow node installer."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from check_version import V_PREFIXED_SEMVER

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIRECTORY / "install-node-template.sh"
REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def render_installer(
    tag: str,
    repository: str,
    template: Path = TEMPLATE,
) -> str:
    if not V_PREFIXED_SEMVER.fullmatch(tag):
        raise ValueError(f"Release tag must be a v-prefixed SemVer: {tag}")
    if not REPOSITORY_SLUG.fullmatch(repository):
        raise ValueError(f"Repository must be an owner/repository slug: {repository}")
    source = template.read_text()
    if source.count("@TAG@") != 1 or source.count("@REPOSITORY@") < 1:
        raise ValueError(
            f"{template}: expected exactly one @TAG@ and at least one @REPOSITORY@ placeholder"
        )
    return source.replace("@TAG@", tag).replace("@REPOSITORY@", repository)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("output", type=Path)
    parser.add_argument("--repository", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rendered = render_installer(args.tag, args.repository)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.output.write_text(rendered)
    args.output.chmod(0o755)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
