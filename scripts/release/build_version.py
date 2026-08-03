#!/usr/bin/env python3
"""Resolve one build version without reading or writing product files."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_BUILD_VERSION = "0.0.0-dev.0"
BUILD_VERSION_ENV = "TERMFLOW_BUILD_VERSION"

_CORE = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
_PRERELEASE = r"(?:-(?:dev|alpha|beta|rc)\.(?:0|[1-9]\d*))?"
_METADATA = r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
BUILD_VERSION_PATTERN = re.compile(rf"{_CORE}{_PRERELEASE}{_METADATA}\Z")


@dataclass(frozen=True, slots=True)
class BuildVersion:
    version: str
    tag: str
    is_release: bool


def validate_version(value: str) -> str:
    """Return a cross-ecosystem build version or raise ``ValueError``."""

    if not BUILD_VERSION_PATTERN.fullmatch(value):
        raise ValueError(
            "build version must be MAJOR.MINOR.PATCH with an optional "
            "dev/alpha/beta/rc prerelease and build metadata"
        )
    return value


def resolve_build_version(
    *,
    tag: str | None,
    environment: Mapping[str, str] = os.environ,
) -> BuildVersion:
    """Resolve Tag > ``TERMFLOW_BUILD_VERSION`` > the development default."""

    if tag not in {None, ""}:
        if not tag.startswith("v"):
            raise ValueError("release tag must be a v-prefixed build version")
        try:
            version = validate_version(tag[1:])
        except ValueError as exc:
            raise ValueError("release tag must be a v-prefixed build version") from exc
        return BuildVersion(version=version, tag=tag, is_release=True)

    configured = environment.get(BUILD_VERSION_ENV, "")
    version = validate_version(configured) if configured else DEFAULT_BUILD_VERSION
    return BuildVersion(version=version, tag=f"v{version}", is_release=False)
