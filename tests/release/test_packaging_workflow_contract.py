from pathlib import Path

import yaml

NODE_WORKFLOW = Path(".github/workflows/package-node.yml")
CONTROL_PLANE_WORKFLOW = Path(".github/workflows/package-control-plane.yml")
CLIENT_WORKFLOW = Path(".github/workflows/tauri-packages.yml")


def _workflow(path: Path) -> dict[object, object]:
    return yaml.safe_load(path.read_text())


def test_node_workflow_is_manual_and_reusable() -> None:
    workflow = _workflow(NODE_WORKFLOW)
    triggers = workflow[True]

    assert workflow["name"] == "Package A · Linux Node"
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert triggers["workflow_dispatch"]["inputs"]["version"] == {
        "description": "Build version override; defaults to 0.0.0-dev.0",
        "required": False,
        "default": "",
        "type": "string",
    }
    assert triggers["workflow_call"]["inputs"]["release_tag"] == {
        "description": "Validated v-prefixed release tag; empty for manual packaging",
        "required": False,
        "default": "",
        "type": "string",
    }
    assert triggers["workflow_call"]["inputs"]["version"] == {
        "description": "Non-release build version override",
        "required": False,
        "default": "",
        "type": "string",
    }
    assert workflow["permissions"] == {"contents": "read"}


def test_node_workflow_owns_names_retention_and_build_commands() -> None:
    text = NODE_WORKFLOW.read_text()

    for required in (
        "termflow-node-linux-x86_64",
        "termflow-${release_tag}-node-linux-x86_64",
        "retention_days=14",
        "retention_days=1",
        "scripts/release/build_node_bundle.sh",
        "scripts/release/prepare_version.py",
        "scripts/release/render_node_installer.py",
        "scripts/release/verify_node_bundle.sh",
        '--repository "$GITHUB_REPOSITORY"',
        "actions/upload-artifact@v4",
        "release-assets/SHA256SUMS",
    ):
        assert required in text
    assert "TERMFLOW_BUILD_VERSION" in text
    assert text.index("scripts/release/prepare_version.py") < text.index(
        "uv sync --frozen --all-packages"
    )


def test_control_plane_workflow_is_manual_and_reusable() -> None:
    workflow = _workflow(CONTROL_PLANE_WORKFLOW)
    triggers = workflow[True]

    assert workflow["name"] == "Package B + Web C · Control Plane"
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert triggers["workflow_dispatch"]["inputs"]["version"]["default"] == ""
    assert "release_tag" in triggers["workflow_call"]["inputs"]
    assert triggers["workflow_call"]["inputs"]["version"]["default"] == ""
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["publish"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert workflow["jobs"]["publish"]["needs"] == ["prepare", "package"]


def test_control_plane_manual_artifact_and_tag_publication_are_separated() -> None:
    text = CONTROL_PLANE_WORKFLOW.read_text()

    for required in (
        "termflow-control-plane",
        "termflow-${release_tag}-control-plane",
        "termflow-control-plane.tar",
        "scripts/build-control-plane-image.sh",
        "scripts/verify-control-plane-image.sh",
        "scripts/release/verify_control_plane_release_image.sh",
        "scripts/release/archive_control_plane_image.sh",
        "scripts/release/prepare_version.py",
        "linux/amd64,linux/arm64",
        "ghcr.io/${owner}/termflow-control-plane",
        'image_tag="${RELEASE_TAG//+/_}"',
        "docker/login-action@v3",
        "docker buildx build",
    ):
        assert required in text
    assert "if: ${{ needs.prepare.outputs.is_release == 'true' }}" in text
    assert "TERMFLOW_BUILD_VERSION" in text


def test_control_plane_materializes_each_image_build_checkout() -> None:
    workflow = _workflow(CONTROL_PLANE_WORKFLOW)

    for job_name, build_marker in (
        ("package", "scripts/build-control-plane-image.sh"),
        ("publish", "docker buildx build"),
    ):
        steps = workflow["jobs"][job_name]["steps"]
        materialize = next(
            index
            for index, step in enumerate(steps)
            if "scripts/release/prepare_version.py" in str(step.get("run", ""))
        )
        build = next(
            index
            for index, step in enumerate(steps)
            if build_marker in str(step.get("run", ""))
        )
        assert materialize < build


def test_client_workflow_is_manual_and_reusable() -> None:
    workflow = _workflow(CLIENT_WORKFLOW)
    triggers = workflow[True]

    assert workflow["name"] == "Package C · Native Clients"
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert triggers["workflow_dispatch"]["inputs"]["platform"]["default"] == "all"
    assert triggers["workflow_dispatch"]["inputs"]["version"]["default"] == ""
    assert triggers["workflow_call"]["inputs"]["platform"] == {
        "description": "Native client platform set",
        "required": False,
        "default": "all",
        "type": "string",
    }
    assert "release_tag" in triggers["workflow_call"]["inputs"]
    assert triggers["workflow_call"]["inputs"]["version"]["default"] == ""


def test_client_artifact_names_are_manual_by_default_and_tagged_when_called() -> None:
    text = CLIENT_WORKFLOW.read_text()

    assert "artifact_prefix=termflow" in text
    assert 'artifact_prefix="termflow-${release_tag}"' in text
    for suffix in (
        "windows-x64-nsis",
        "linux-x64",
        "macos-arm64",
        "android-arm64-debug",
        "ios-simulator-aarch64",
    ):
        assert f"${{{{ needs.validate-version.outputs.artifact_prefix }}}}-{suffix}" in text


def test_every_native_runner_materializes_before_reading_package_manifests() -> None:
    workflow = _workflow(CLIENT_WORKFLOW)

    for job_name in (
        "windows-nsis",
        "linux-packages",
        "macos-packages",
        "android-debug-apk",
        "ios-simulator-app",
    ):
        steps = workflow["jobs"][job_name]["steps"]
        materialize = next(
            index
            for index, step in enumerate(steps)
            if "scripts/release/prepare_version.py" in str(step.get("run", ""))
        )
        rust_cache = next(
            index
            for index, step in enumerate(steps)
            if step.get("uses") == "Swatinem/rust-cache@v2"
        )
        npm_install = next(
            index
            for index, step in enumerate(steps)
            if str(step.get("run", "")).strip() == "npm ci"
        )
        assert materialize < rust_cache
        assert materialize < npm_install
