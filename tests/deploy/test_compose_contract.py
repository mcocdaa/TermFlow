from pathlib import Path

import yaml


def test_production_compose_uses_an_explicit_release_image_without_source_build() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]

    assert service["image"] == "${TERMFLOW_IMAGE:?set TERMFLOW_IMAGE to a pinned GHCR tag}"
    assert "build" not in service


def test_development_override_owns_the_local_source_build() -> None:
    compose = yaml.safe_load(Path("deploy/compose.dev.yaml").read_text())

    assert compose["services"]["control-plane"]["build"] == {
        "context": "..",
        "dockerfile": "deploy/Dockerfile.control-plane",
    }


def test_release_image_smoke_uses_an_isolated_test_volume() -> None:
    verifier = Path("scripts/release/verify_control_plane_release_image.sh").read_text()
    workflow = Path(".github/workflows/ci.yml").read_text()

    assert "TERMFLOW_DATA_VOLUME=\"${PROJECT}-data\"" in verifier
    assert "up -d --wait" in verifier
    assert "down --volumes --remove-orphans" in verifier
    assert "http://127.0.0.1:18076/healthz" in verifier
    assert "verify_control_plane_release_image.sh termflow-control-plane:ci" in workflow


def test_full_verification_supplies_the_required_nonproduction_compose_values() -> None:
    verify = Path("scripts/verify.sh").read_text()

    assert "TERMFLOW_IMAGE=\"${CONTROL_PLANE_IMAGE}\"" in verify
    assert "TERMFLOW_ADMIN_TOKEN=\"verify-admin-token-that-is-long-enough\"" in verify


def test_compose_is_single_worker_and_persists_only_metadata() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]
    assert "--workers" not in " ".join(service["command"])
    assert service["volumes"] == ["termflow-data:/app/data"]
    assert service["healthcheck"]["test"][-1].endswith("/healthz")
    assert list(compose["services"]) == ["control-plane"]
    assert compose["volumes"]["termflow-data"] == {
        "name": "${TERMFLOW_DATA_VOLUME:-termflow-data}"
    }


def test_compose_configures_same_origin_web_control_limits() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    environment = compose["services"]["control-plane"]["environment"]
    assert environment["TERMFLOW_STATIC_DIR"] == "/app/frontend-dist"
    assert environment["TERMFLOW_ALLOW_INSECURE_LOOPBACK"] == (
        "${TERMFLOW_ALLOW_INSECURE_LOOPBACK:-true}"
    )
    assert "TERMFLOW_PUBLIC_BASE_URL" in environment
    assert "TERMFLOW_TRUSTED_WEB_ORIGINS" in environment
    assert "TERMFLOW_BROWSER_SESSION_TTL_SECONDS" in environment
    assert environment["TERMFLOW_TOTP_MASTER_KEY"] is None
    assert environment["TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE"] == (
        "/app/data/totp-master-key"
    )
    assert environment["TERMFLOW_ENROLLMENT_TOKEN_TTL_SECONDS"] == (
        "${TERMFLOW_ENROLLMENT_TOKEN_TTL_SECONDS:-60}"
    )
    assert "TERMFLOW_BROWSER_SESSION_CAPACITY" in environment
    assert "TERMFLOW_TERMINAL_MAX_FRAME_BYTES" in environment
    assert "TERMFLOW_TERMINAL_INPUT_RATE_BYTES_PER_SECOND" in environment
    assert "TERMFLOW_TERMINAL_QUEUE_MAX_MESSAGES" in environment
    assert "TERMFLOW_TERMINAL_QUEUE_MAX_BYTES" in environment
    assert "TERMFLOW_TERMINAL_RESUME_GRACE_SECONDS" in environment


def test_control_plane_image_uses_builders_and_a_source_free_runtime() -> None:
    dockerfile = Path("deploy/Dockerfile.control-plane").read_text()
    assert "FROM node:22.23.2-bookworm-slim AS web" in dockerfile
    assert "COPY package.json package-lock.json ./" in dockerfile
    assert "apps/clients/web/package-lock.json" not in dockerfile
    assert "npm ci" in dockerfile
    assert "npm run build:web" in dockerfile
    assert "COPY --from=web" in dockerfile
    assert "AS python-wheels" in dockerfile
    assert "uv build --wheel --package termflow-protocol" in dockerfile
    assert "uv build --wheel --package termflow-control-plane" in dockerfile
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert "USER termflow" in dockerfile

    runtime = dockerfile.split("FROM python:3.12-slim AS runtime", maxsplit=1)[1]
    assert "COPY --from=python-wheels /opt/termflow /opt/termflow" in runtime
    assert 'CMD ["/opt/termflow/bin/termflow-control"' in runtime
    for forbidden in (
        "COPY packages",
        "COPY apps",
        "pyproject.toml",
        "uv.lock",
        "package-lock.json",
        "package.json",
        "npm ",
        "node ",
        "cargo ",
        "rust",
        "/uv",
    ):
        assert forbidden not in runtime.lower()


def test_docker_context_excludes_local_state_and_frontend_build_output() -> None:
    ignored = Path(".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".venv" in ignored
    assert ".worktrees" in ignored
    assert "**/node_modules" in ignored
    assert "**/dist" in ignored
    assert "apps/node/src" in ignored
    assert "apps/clients/tauri" in ignored
    assert "**/target" in ignored


def test_delivery_scripts_verify_image_contents_and_tauri_compile_gates() -> None:
    verify = Path("scripts/verify.sh").read_text()
    image_build = Path("scripts/build-control-plane-image.sh")
    image_check = Path("scripts/verify-control-plane-image.sh").read_text()
    tauri_check = Path("scripts/verify-tauri.sh").read_text()
    workflow = Path(".github/workflows/ci.yml").read_text()

    assert Path(".nvmrc").read_text().strip() == "22.23.2"
    assert 'EXPECTED_NODE_VERSION="v22.23.2"' in verify
    assert "npm run build --workspaces --if-present" in verify
    assert "scripts/verify-tauri.sh" in verify
    assert image_build.is_file()
    assert "scripts/build-control-plane-image.sh" in verify
    assert "scripts/verify-control-plane-image.sh" in verify
    assert "scripts/build-control-plane-image.sh termflow-control-plane:ci" in workflow

    for expected in (
        "termflow_control_plane",
        "termflow_protocol",
        "/app/frontend-dist/index.html",
        "/opt/termflow/bin/termflow-control",
        "auth totp reset --help",
        "find /",
    ):
        assert expected in image_check
    for forbidden in (
        "/app/apps",
        "/app/packages",
        "/app/tests",
        "/app/uv.lock",
        "/app/package-lock.json",
        "node",
        "npm",
        "cargo",
        "rustc",
    ):
        assert forbidden in image_check

    for command in ("cargo fmt", "cargo clippy", "cargo test", "cargo check", "--no-bundle"):
        assert command in tauri_check
    assert "project is not present" not in tauri_check
    assert 'node-version: "22.23.2"' in workflow
    assert "dtolnay/rust-toolchain@stable" in workflow
    assert "tauri-desktop-unsigned" in workflow
    assert "tauri-android-unsigned" in workflow
    assert "tauri-ios-unsigned" in workflow
    assert "android init --ci" in workflow
    assert "ios init --ci" in workflow
    assert "tauri=false" not in workflow
    assert "needs.native-projects.outputs" not in workflow


def test_compose_has_an_explicit_optional_totp_secret_file_override() -> None:
    override = yaml.safe_load(Path("deploy/compose.totp-secret.yaml").read_text())
    service = override["services"]["control-plane"]
    assert service["environment"]["TERMFLOW_TOTP_MASTER_KEY_FILE"] == (
        "/run/secrets/termflow-totp-master-key"
    )
    assert service["secrets"] == ["termflow-totp-master-key"]
    assert override["secrets"]["termflow-totp-master-key"]["file"] == (
        "${TERMFLOW_TOTP_MASTER_KEY_FILE:?set TERMFLOW_TOTP_MASTER_KEY_FILE}"
    )


def test_tauri_packages_are_manual_native_artifacts_not_public_releases() -> None:
    path = Path(".github/workflows/tauri-packages.yml")
    old_path = Path(".github/workflows/tauri-windows-package.yml")
    assert path.is_file()
    assert not old_path.exists()

    workflow = yaml.safe_load(path.read_text())
    triggers = workflow[True]
    assert set(triggers) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    manual_platform = triggers["workflow_dispatch"]["inputs"]["platform"]
    assert manual_platform == {
        "description": "Platform package to build",
        "required": True,
        "default": "all",
        "type": "choice",
        "options": ["all", "windows", "linux", "macos", "android", "ios"],
    }
    assert workflow["run-name"] == (
        "Tauri packages · "
        "${{ inputs.platform }}"
    )
    assert workflow["concurrency"] == {
        "group": "tauri-packages-${{ github.ref }}-${{ inputs.platform }}",
        "cancel-in-progress": False,
    }

    jobs = workflow["jobs"]
    assert {
        name: job["name"]
        for name, job in jobs.items()
    } == {
        "validate-version": "Validate package version",
        "windows-nsis": "Windows x64 · NSIS",
        "linux-packages": "Linux x64 · deb and AppImage",
        "macos-packages": "macOS arm64 · app and DMG",
        "android-debug-apk": "Android arm64 · debug APK",
        "ios-simulator-app": "iOS arm64 simulator · app",
    }
    assert jobs["validate-version"]["runs-on"] == "ubuntu-22.04"
    assert jobs["windows-nsis"]["runs-on"] == "windows-latest"
    assert jobs["linux-packages"]["runs-on"] == "ubuntu-22.04"
    assert jobs["macos-packages"]["runs-on"] == "macos-15"
    assert jobs["android-debug-apk"]["runs-on"] == "ubuntu-latest"
    assert jobs["ios-simulator-app"]["runs-on"] == "macos-15"
    for job_name in (
        "windows-nsis",
        "linux-packages",
        "macos-packages",
        "android-debug-apk",
        "ios-simulator-app",
    ):
        assert jobs[job_name]["needs"] == "validate-version"
    for job_name, platform in {
        "windows-nsis": "windows",
        "linux-packages": "linux",
        "macos-packages": "macos",
        "android-debug-apk": "android",
        "ios-simulator-app": "ios",
    }.items():
        condition = jobs[job_name]["if"]
        assert condition == (
            "${{ needs.validate-version.result == 'success' &&\n"
            "    (inputs.platform == 'all' || "
            f"inputs.platform == '{platform}') }}}}"
        )

    rendered = path.read_text()
    assert "scripts/release/check_version.py" in rendered
    for expected in (
        'python-version: "3.12"',
        "scripts/release/check_version.py",
        'node-version: "22.23.2"',
        "dtolnay/rust-toolchain@stable",
        "npm ci",
        "--bundles nsis",
        "--bundles deb,appimage",
        "--bundles app,dmg",
        "Require one package of each Linux format",
        "Expected exactly one macOS DMG",
        "android init --ci",
        "android build --debug --ci --target aarch64 --apk",
        "ios init --ci",
        "ios build --debug --ci --target aarch64-sim --no-sign",
        "ditto -c -k --sequesterRsrc --keepParent",
        "actions/upload-artifact@v4",
        "if-no-files-found: error",
        "retention-days: 14",
    ):
        assert expected in rendered
    for artifact in (
        "apps/clients/tauri/src-tauri/target/release/bundle/nsis/*-setup.exe",
        "apps/clients/tauri/src-tauri/target/release/bundle/deb/*.deb",
        "apps/clients/tauri/src-tauri/target/release/bundle/appimage/*.AppImage",
        "apps/clients/tauri/src-tauri/target/release/bundle/dmg/*.dmg",
        "TermFlow-macos-arm64.app.zip",
        "apps/clients/tauri/src-tauri/gen/android/app/build/outputs/apk/**/*-debug.apk",
        "TermFlow-ios-simulator-aarch64.app.zip",
    ):
        assert artifact in rendered

    assert "contents: write" not in rendered
    assert "gh release" not in rendered
    assert "softprops/action-gh-release" not in rendered
    assert "deploy/Dockerfile.control-plane" not in rendered
