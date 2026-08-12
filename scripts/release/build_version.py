#!/usr/bin/env python3
"""Resolve one build version without reading or writing product files."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_BUILD_VERSION = "0.0.1-dev.0"
BUILD_VERSION_ENV = "TERMFLOW_BUILD_VERSION"

_CORE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_PRERELEASE = r"(?:-(?:dev|alpha|beta|rc)\.(?:0|[1-9][0-9]*))?"
_METADATA = r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
BUILD_VERSION_PATTERN = re.compile(rf"{_CORE}{_PRERELEASE}{_METADATA}\Z")
V_PREFIXED_VERSION_PATTERN = re.compile(rf"v{_CORE}{_PRERELEASE}{_METADATA}\Z")
_ANDROID_STAGE_BASE = {"dev": 0, "alpha": 20, "beta": 40, "rc": 60}
_ANDROID_STAGE_MAX = {"dev": 19, "alpha": 19, "beta": 19, "rc": 38}


@dataclass(frozen=True, slots=True)
class BuildVersion:
    version: str
    tag: str
    is_release: bool
    is_prerelease: bool


def android_version_code(version: str) -> int:
    """Map a validated build version to a monotonically ordered Android code."""

    release = version.split("+", 1)[0]
    core, separator, prerelease = release.partition("-")
    major, minor, patch = (int(component) for component in core.split("."))
    if (
        (major, minor, patch) == (0, 0, 0)
        or major > 2099
        or minor > 99
        or patch > 99
    ):
        raise ValueError("build version is outside the supported mobile bundle range")
    rank = 99
    if separator:
        stage, raw_number = prerelease.split(".", 1)
        number = int(raw_number)
        maximum = _ANDROID_STAGE_MAX.get(stage)
        if maximum is None or number > maximum:
            raise ValueError("build version is outside the supported mobile bundle range")
        rank = _ANDROID_STAGE_BASE[stage] + number
    return major * 1_000_000 + minor * 10_000 + patch * 100 + rank


def validate_version(value: str) -> str:
    """Return a cross-ecosystem build version or raise ``ValueError``."""

    if not BUILD_VERSION_PATTERN.fullmatch(value):
        raise ValueError(
            "build version must be MAJOR.MINOR.PATCH with an optional "
            "dev/alpha/beta/rc prerelease and build metadata"
        )
    android_version_code(value)
    return value


def is_prerelease_version(version: str) -> bool:
    """Return whether ``version`` has a prerelease before build metadata."""

    return "-" in version.split("+", 1)[0]


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
        return BuildVersion(
            version=version,
            tag=tag,
            is_release=True,
            is_prerelease=is_prerelease_version(version),
        )

    configured = environment.get(BUILD_VERSION_ENV, "")
    version = validate_version(configured) if configured else DEFAULT_BUILD_VERSION
    return BuildVersion(
        version=version,
        tag=f"v{version}",
        is_release=False,
        is_prerelease=is_prerelease_version(version),
    )
