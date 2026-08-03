from pathlib import Path

import yaml

WORKFLOW_PATH = Path(".github/workflows/release.yml")


def test_release_calls_the_three_base_packaging_workflows() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    assert workflow[True]["push"]["tags"] == ["v*"]
    assert jobs["package-node"] == {
        "name": "Package A",
        "needs": "validate-version",
        "uses": "./.github/workflows/package-node.yml",
        "with": {"release_tag": "${{ github.ref_name }}"},
    }
    assert jobs["package-clients"] == {
        "name": "Package native C",
        "needs": "validate-version",
        "uses": "./.github/workflows/tauri-packages.yml",
        "with": {
            "platform": "all",
            "release_tag": "${{ github.ref_name }}",
        },
    }
    control = jobs["package-control-plane"]
    assert set(control["needs"]) == {"package-node", "package-clients"}
    assert control["uses"] == "./.github/workflows/package-control-plane.yml"
    assert control["with"] == {"release_tag": "${{ github.ref_name }}"}
    assert control["permissions"] == {"contents": "read", "packages": "write"}


def test_release_publishes_only_after_all_reusable_workflows() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    publish = workflow["jobs"]["publish"]

    assert set(publish["needs"]) == {
        "validate-version",
        "package-node",
        "package-clients",
        "package-control-plane",
    }
    assert publish["permissions"] == {"contents": "write"}


def test_release_contains_no_product_packaging_implementation() -> None:
    text = WORKFLOW_PATH.read_text()

    for forbidden in (
        "build_node_bundle.sh",
        "tauri:build",
        "android build",
        "ios build",
        "docker buildx build",
        "Dockerfile.control-plane",
        "*-setup.exe",
        "*.AppImage",
        "*.dmg",
        "*-debug.apk",
    ):
        assert forbidden not in text
    for required in (
        "actions/download-artifact@v4",
        "merge-multiple: true",
        "SHA256SUMS",
        "gh release create",
        "--prerelease",
    ):
        assert required in text
