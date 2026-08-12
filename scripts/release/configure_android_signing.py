#!/usr/bin/env python3
"""Inject fail-closed release signing into a generated Tauri Android project."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from pathlib import Path

_CONFIGURED_MARKER = 'signingConfig = signingConfigs.getByName("release")'
_IMPORT_MARKER = "import java.util.Properties\n"
_BUILD_TYPES_MARKER = "    buildTypes {\n"
_RELEASE_MARKER = '        getByName("release") {\n'
_SIGNING_CONFIG = """    signingConfigs {
        create("release") {
            val keystorePropertiesFile = rootProject.file("keystore.properties")
            val keystoreProperties = Properties().apply {
                FileInputStream(keystorePropertiesFile).use { load(it) }
            }
            keyAlias = keystoreProperties.getProperty("keyAlias")
            keyPassword = keystoreProperties.getProperty("keyPassword")
            storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
            storePassword = keystoreProperties.getProperty("storePassword")
        }
    }
"""
_ENVIRONMENT_FIELDS = (
    "ANDROID_KEYSTORE_PATH",
    "ANDROID_KEYSTORE_PASSWORD",
    "ANDROID_KEY_ALIAS",
    "ANDROID_KEY_PASSWORD",
)


def _require_once(source: str, marker: str) -> None:
    if source.count(marker) != 1:
        raise ValueError("unsupported Tauri Android Gradle template")


def configure_gradle(source: str) -> str:
    """Return the generated Gradle source with one release signing config."""

    if _CONFIGURED_MARKER in source:
        if source.count(_CONFIGURED_MARKER) != 1 or source.count('create("release")') != 1:
            raise ValueError("unsupported Tauri Android Gradle template")
        return source
    for marker in (_IMPORT_MARKER, _BUILD_TYPES_MARKER, _RELEASE_MARKER):
        _require_once(source, marker)
    source = source.replace(
        _IMPORT_MARKER,
        f"{_IMPORT_MARKER}import java.io.FileInputStream\n",
        1,
    )
    source = source.replace(
        _BUILD_TYPES_MARKER,
        f"{_SIGNING_CONFIG}{_BUILD_TYPES_MARKER}",
        1,
    )
    source = source.replace(
        _RELEASE_MARKER,
        f'{_RELEASE_MARKER}            {_CONFIGURED_MARKER}\n',
        1,
    )
    return source


def _escape_property(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    escaped = escaped.replace("=", "\\=").replace(":", "\\:")
    if escaped.startswith(" "):
        escaped = f"\\{escaped}"
    return escaped


def write_keystore_properties(path: Path, environment: Mapping[str, str]) -> None:
    """Write the four Gradle properties without exposing values in command output."""

    missing = [name for name in _ENVIRONMENT_FIELDS if not environment.get(name)]
    if missing:
        raise ValueError(f"missing Android signing environment: {', '.join(missing)}")
    values = {
        "storePassword": environment["ANDROID_KEYSTORE_PASSWORD"],
        "keyPassword": environment["ANDROID_KEY_PASSWORD"],
        "keyAlias": environment["ANDROID_KEY_ALIAS"],
        "storeFile": environment["ANDROID_KEYSTORE_PATH"],
    }
    path.write_text(
        "".join(f"{key}={_escape_property(value)}\n" for key, value in values.items())
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gradle", type=Path, required=True)
    parser.add_argument("--properties", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    original = args.gradle.read_text()
    configured = configure_gradle(original)
    if configured != original:
        args.gradle.write_text(configured)
    write_keystore_properties(args.properties, os.environ)
    print(f"configured Android release signing: {args.gradle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
