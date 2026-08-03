from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW_PATH = Path(".github/workflows/release.yml")


def test_release_waits_for_all_assets_before_scoped_publication() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())

    assert workflow[True]["push"]["tags"] == ["v*"]
    assert workflow["permissions"] == {"contents": "read"}
    publish = workflow["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "write", "packages": "write"}
    assert set(publish["needs"]) == {
        "validate-version",
        "node-bundle",
        "node-bundle-verify",
        "control-plane-verify",
        "windows-nsis",
        "linux-packages",
        "macos-packages",
        "android-debug-apk",
        "ios-simulator-app",
    }


def test_release_builds_multiarch_image_and_checksum_release_assets() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    text = WORKFLOW_PATH.read_text()

    assert workflow["jobs"]["node-bundle-verify"]["needs"] == "validate-version"
    assert workflow["jobs"]["control-plane-verify"]["needs"] == "validate-version"
    for job_name in (
        "windows-nsis",
        "linux-packages",
        "macos-packages",
        "android-debug-apk",
        "ios-simulator-app",
    ):
        assert workflow["jobs"][job_name]["needs"] == "validate-version"

    for expected in (
        "linux/amd64,linux/arm64",
        "SHA256SUMS",
        "docker/setup-buildx-action@v3",
        "docker/login-action@v3",
        "gh release create",
        "--prerelease",
        "imagetools create",
        "iOS Simulator-only",
        "Windows package is unsigned",
    ):
        assert expected in text
