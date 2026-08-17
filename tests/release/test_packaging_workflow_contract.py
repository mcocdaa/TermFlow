from pathlib import Path

import yaml

NODE_WORKFLOW = Path(".github/workflows/package-node.yml")
CONTROL_PLANE_WORKFLOW = Path(".github/workflows/package-control-plane.yml")
CLIENT_WORKFLOW = Path(".github/workflows/tauri-packages.yml")


def _workflow(path: Path) -> dict[object, object]:
    return yaml.safe_load(path.read_text())


def _step_index(steps: list[dict[str, object]], marker: str) -> int:
    return next(
        index
        for index, step in enumerate(steps)
        if marker in str(step.get("run", ""))
    )


def _step_index_by_action(steps: list[dict[str, object]], action: str) -> int:
    return next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith(action)
    )


def test_server_package_workflows_are_manual_and_materialize_before_build() -> None:
    node = _workflow(NODE_WORKFLOW)
    node_triggers = node[True]
    assert node["name"] == "Package A · Linux Node"
    assert set(node_triggers) == {"workflow_dispatch", "workflow_call"}
    assert node_triggers["workflow_call"]["inputs"]["release_tag"]["default"] == ""
    assert node["permissions"] == {"contents": "read"}
    assert set(node["jobs"]) == {"prepare", "package", "package-docker", "publish"}
    assert node["jobs"]["publish"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }

    node_text = NODE_WORKFLOW.read_text()
    for required in (
        "termflow-node-linux-x86_64",
        "termflow-${release_tag}-node-linux-x86_64",
        "retention_days=14",
        "actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4",
        "termflow-node-docker",
        "termflow-${release_tag}-node-docker",
        "termflow-node.tar",
        "linux/amd64,linux/arm64",
        "ghcr.io/${owner}/termflow-node",
        "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9",
        "cosign sign",
        "if: ${{ needs.prepare.outputs.is_release == 'true' }}",
    ):
        assert required in node_text
    assert ">/dev/null" not in node_text

    control_plane = _workflow(CONTROL_PLANE_WORKFLOW)
    cp_triggers = control_plane[True]
    assert control_plane["name"] == "Package B + Web C · Control Plane"
    assert set(cp_triggers) == {"workflow_dispatch", "workflow_call"}
    assert "release_tag" in cp_triggers["workflow_call"]["inputs"]
    assert control_plane["permissions"] == {"contents": "read"}
    assert control_plane["jobs"]["publish"]["needs"] == ["prepare", "package"]

    cp_text = CONTROL_PLANE_WORKFLOW.read_text()
    for required in (
        "termflow-control-plane",
        "termflow-${release_tag}-control-plane",
        "termflow-control-plane.tar",
        "linux/amd64,linux/arm64",
        "ghcr.io/${owner}/termflow-control-plane",
        "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9",
        "docker buildx build",
    ):
        assert required in cp_text
    assert '[[ "$IS_PRERELEASE" == "false" ]]' in cp_text
    assert '[[ "$RELEASE_TAG" != *-* ]]' not in cp_text
    assert ">/dev/null" not in cp_text

    for workflow, build_markers in (
        (NODE_WORKFLOW, ("scripts/build-node-image.sh", "docker buildx build")),
        (
            CONTROL_PLANE_WORKFLOW,
            ("scripts/build-control-plane-image.sh", "docker buildx build"),
        ),
    ):
        parsed = _workflow(workflow)
        for job_name, build_marker in (
            ("package-docker", build_markers[0]),
            ("publish", build_markers[1]),
        ):
            if job_name not in parsed["jobs"]:
                continue
            steps = parsed["jobs"][job_name]["steps"]
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


def test_client_workflow_is_manual_and_artifacts_are_tagged_and_verified() -> None:
    workflow = _workflow(CLIENT_WORKFLOW)
    triggers = workflow[True]
    assert workflow["name"] == "Package C · Native Clients"
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert triggers["workflow_dispatch"]["inputs"]["platform"]["default"] == "all"
    assert set(triggers["workflow_call"]["secrets"]) == {
        "ANDROID_KEYSTORE_BASE64",
        "ANDROID_KEYSTORE_PASSWORD",
        "ANDROID_KEY_ALIAS",
        "ANDROID_KEY_PASSWORD",
        "ANDROID_SIGNING_CERT_SHA256",
    }
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert set(jobs) == {
        "validate-version",
        "windows-nsis",
        "linux-packages",
        "macos-packages",
        "android-apk",
        "ios-simulator-app",
    }
    for job_name, runner, platform in (
        ("windows-nsis", "windows-latest", "windows"),
        ("linux-packages", "ubuntu-22.04", "linux"),
        ("macos-packages", "macos-15", "macos"),
        ("android-apk", "ubuntu-latest", "android"),
        ("ios-simulator-app", "macos-15", "ios"),
    ):
        job = jobs[job_name]
        assert job["runs-on"] == runner
        assert job["needs"] == "validate-version"
        assert f"inputs.platform == '{platform}'" in job["if"]

    text = CLIENT_WORKFLOW.read_text()
    assert "artifact_prefix=termflow" in text
    assert 'artifact_prefix="termflow-${release_tag}"' in text
    for required in (
        "--bundles nsis",
        "--bundles deb,appimage",
        "--bundles app,dmg",
        "android build --debug --ci --target aarch64 --apk",
        "ios build --debug --ci --target aarch64-sim --no-sign",
        "actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4",
        "*-release.apk",
        "--expected-cert-sha256",
    ):
        assert required in text
    for forbidden in ("contents: write", "gh release", "softprops/action-gh-release"):
        assert forbidden not in text
    assert ">/dev/null" not in text

    android = jobs["android-apk"]
    steps = android["steps"]
    init = _step_index(steps, "android init --ci")
    signing = _step_index(steps, "configure_android_signing.py")
    release_build = _step_index(steps, "android build --ci --target aarch64 --apk")
    verify = _step_index(steps, "verify_android_apk.py")
    upload = _step_index_by_action(steps, "actions/upload-artifact@")
    cleanup = _step_index(steps, 'rm -f "$RUNNER_TEMP/termflow-android-release.jks"')
    assert init < signing < release_build < verify < upload < cleanup
    assert steps[signing]["if"] == (
        "${{ needs.validate-version.outputs.android_release_build == 'true' }}"
    )
    assert steps[cleanup]["if"] == "${{ always() }}"

    for job_name in (
        "windows-nsis",
        "linux-packages",
        "macos-packages",
        "android-apk",
        "ios-simulator-app",
    ):
        job_steps = jobs[job_name]["steps"]
        materialize = next(
            index
            for index, step in enumerate(job_steps)
            if "scripts/release/prepare_version.py" in str(step.get("run", ""))
        )
        rust_cache = next(
            index
            for index, step in enumerate(job_steps)
            if step.get("uses")
            == "Swatinem/rust-cache@49a0bdc70d2e1b713ca9e2869b211fcce03d3c1c"
        )
        assert materialize < rust_cache