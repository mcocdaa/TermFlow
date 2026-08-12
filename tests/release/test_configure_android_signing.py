from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release.configure_android_signing import (
    configure_gradle,
    write_keystore_properties,
)

TAURI_GRADLE = """import java.util.Properties

plugins {
    id("com.android.application")
}

android {
    buildTypes {
        getByName("debug") {
            isDebuggable = true
        }
        getByName("release") {
            isMinifyEnabled = true
        }
    }
}
"""


def test_configures_release_signing_once() -> None:
    updated = configure_gradle(TAURI_GRADLE)

    assert "import java.io.FileInputStream" in updated
    assert 'create("release")' in updated
    assert 'rootProject.file("keystore.properties")' in updated
    assert 'signingConfig = signingConfigs.getByName("release")' in updated
    assert configure_gradle(updated) == updated


def test_rejects_unknown_or_ambiguous_gradle_template() -> None:
    with pytest.raises(ValueError, match="unsupported Tauri Android Gradle template"):
        configure_gradle("plugins {}")
    with pytest.raises(ValueError, match="unsupported Tauri Android Gradle template"):
        configure_gradle(
            TAURI_GRADLE.replace(
                "    buildTypes {",
                "    buildTypes {\n    buildTypes {",
            )
        )


def test_writes_escaped_java_properties(tmp_path: Path) -> None:
    path = tmp_path / "keystore.properties"
    write_keystore_properties(
        path,
        {
            "ANDROID_KEYSTORE_PATH": r"C:\keys\termflow.jks",
            "ANDROID_KEYSTORE_PASSWORD": " leading=value:one",
            "ANDROID_KEY_ALIAS": "termflow=release",
            "ANDROID_KEY_PASSWORD": "line1\nline2",
        },
    )

    assert path.read_text() == (
        "storePassword=\\ leading\\=value\\:one\n"
        "keyPassword=line1\\nline2\n"
        "keyAlias=termflow\\=release\n"
        "storeFile=C\\:\\\\keys\\\\termflow.jks\n"
    )


def test_rejects_missing_signing_environment(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ANDROID_KEY_ALIAS"):
        write_keystore_properties(
            tmp_path / "keystore.properties",
            {
                "ANDROID_KEYSTORE_PATH": "/tmp/key.jks",
                "ANDROID_KEYSTORE_PASSWORD": "store",
                "ANDROID_KEY_PASSWORD": "key",
            },
        )
