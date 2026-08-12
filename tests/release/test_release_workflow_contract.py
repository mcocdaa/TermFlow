from pathlib import Path

import yaml

WORKFLOW_PATH = Path(".github/workflows/release.yml")


def test_release_resolves_the_tag_without_comparing_source_placeholders() -> None:
    text = WORKFLOW_PATH.read_text()

    assert (
        'python scripts/release/prepare_version.py --tag "$GITHUB_REF_NAME" '
        "--resolve-only"
    ) in text
    assert "scripts/release/check_version.py" not in text


def test_release_calls_the_three_base_packaging_workflows() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    assert workflow[True]["push"]["tags"] == ["v*"]
    assert jobs["package-node"] == {
        "name": "Package A",
        "needs": "validate-version",
        "permissions": {
            "contents": "read",
            "packages": "write",
            "id-token": "write",
        },
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
    assert control["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }


def test_release_publishes_only_after_all_reusable_workflows() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    publish = workflow["jobs"]["publish"]

    assert set(publish["needs"]) == {
        "validate-version",
        "package-node",
        "package-clients",
        "package-control-plane",
    }
    assert publish["permissions"] == {
        "contents": "write",
        "attestations": "write",
        "id-token": "write",
    }


def test_release_uses_resolved_prerelease_state_not_raw_tag_punctuation() -> None:
    text = WORKFLOW_PATH.read_text()
    workflow = yaml.safe_load(text)

    validate = workflow["jobs"]["validate-version"]
    assert validate["outputs"]["is_prerelease"] == (
        "${{ steps.version.outputs.is_prerelease }}"
    )
    publish_steps = workflow["jobs"]["publish"]["steps"]
    release_step = next(
        step for step in publish_steps if "gh release create" in step.get("run", "")
    )
    assert release_step["env"]["IS_PRERELEASE"] == (
        "${{ needs.validate-version.outputs.is_prerelease }}"
    )
    assert '[[ "$IS_PRERELEASE" == "true" ]]' in release_step["run"]
    assert '[[ "${GITHUB_REF_NAME}" == *-* ]]' not in text


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
        "actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0",
        "merge-multiple: true",
        "find release-assets -mindepth 1 -maxdepth 1 -type d",
        "SHA256SUMS",
        "gh release create",
        "--prerelease",
    ):
        assert required in text
