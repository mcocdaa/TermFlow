from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# The spec fixes the script name as scripts/patch-mobile-manifests.py; the hyphens
# make it unimportable as a Python module, so the tests exercise it through its CLI
# (the same way CI invokes it) instead of importing pure functions.
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "patch-mobile-manifests.py"

FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application
        android:theme="@style/Theme.app">
        <activity android:name=".MainActivity" />
    </application>
</manifest>
"""


def run_patch(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_injects_both_audio_permissions_and_is_idempotent(tmp_path: Path) -> None:
    manifest = tmp_path / "AndroidManifest.xml"
    manifest.write_text(FIXTURE, encoding="utf-8")

    first = run_patch("--manifest", str(manifest))
    assert first.returncode == 0
    patched = manifest.read_text(encoding="utf-8")
    assert '    <uses-permission android:name="android.permission.RECORD_AUDIO"/>' in patched
    assert (
        '    <uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS"/>' in patched
    )
    assert patched.index("<uses-permission") < patched.index("<application")

    second = run_patch("--manifest", str(manifest))
    assert second.returncode == 0
    assert "already declared" in second.stdout
    assert manifest.read_text(encoding="utf-8") == patched


def test_existing_permissions_are_skipped_without_duplicates(tmp_path: Path) -> None:
    manifest = tmp_path / "AndroidManifest.xml"
    manifest.write_text(
        FIXTURE.replace(
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">',
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
            '    <uses-permission android:name="android.permission.RECORD_AUDIO" />',
        ),
        encoding="utf-8",
    )

    result = run_patch("--manifest", str(manifest))

    assert result.returncode == 0
    assert "android.permission.MODIFY_AUDIO_SETTINGS" in result.stdout
    patched = manifest.read_text(encoding="utf-8")
    assert patched.count("android.permission.RECORD_AUDIO") == 1
    assert patched.count("android.permission.MODIFY_AUDIO_SETTINGS") == 1


def test_missing_manifest_file_fails_loud_with_nonzero_exit(tmp_path: Path) -> None:
    missing = tmp_path / "missing.xml"

    result = run_patch("--manifest", str(missing))

    assert result.returncode == 1
    assert result.stderr
    assert not missing.exists()


def test_malformed_manifest_without_opening_tag_fails_loud(tmp_path: Path) -> None:
    manifest = tmp_path / "AndroidManifest.xml"
    manifest.write_text("<application />\n", encoding="utf-8")

    result = run_patch("--manifest", str(manifest))

    assert result.returncode == 1
    assert "opening tag" in result.stderr
