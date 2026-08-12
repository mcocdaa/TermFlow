from pathlib import Path

import yaml


def test_default_compose_builds_current_checkout_without_an_image_source() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]

    assert service["build"] == {
        "context": "..",
        "dockerfile": "deploy/Dockerfile.control-plane",
    }
    assert "image" not in service
    assert "TERMFLOW_IMAGE" not in Path("deploy/compose.yaml").read_text()
    assert not Path("deploy/compose.dev.yaml").exists()


def test_release_image_smoke_is_independent_from_runtime_compose() -> None:
    verifier = Path("scripts/release/verify_control_plane_release_image.sh").read_text()
    workflow = Path(".github/workflows/ci.yml").read_text()

    assert 'IMAGE="$1"' in verifier
    assert "docker run --detach" in verifier
    assert "docker volume create" in verifier
    assert "docker rm --force" in verifier
    assert "docker volume rm" in verifier
    assert "TERMFLOW_IMAGE" not in verifier
    assert "TERMFLOW_IMAGE" not in workflow
    assert "docker compose" not in verifier
    assert "http://127.0.0.1:18076/healthz" in verifier
    assert "verify_control_plane_release_image.sh termflow-control-plane:ci" in workflow


def test_full_verification_checks_source_build_compose_configuration() -> None:
    verify = Path("scripts/verify.sh").read_text()

    for required in ("pytest -q", "ruff check .", "mypy", "docker compose"):
        assert required in verify
    for destructive in ("rm -", "docker stop", "kill-server"):
        assert destructive not in verify
    assert "TERMFLOW_IMAGE" not in verify
    assert 'TERMFLOW_ADMIN_TOKEN="verify-admin-token-that-is-long-enough"' in verify
    assert "docker compose -f deploy/compose.yaml config --quiet" in verify


def test_compose_is_single_worker_and_persists_only_metadata() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]
    assert "--workers" not in " ".join(service["command"])
    assert service["volumes"] == [
        "termflow-data:/app/data",
        "termflow-totp-key:/app/totp-secrets",
    ]
    assert service["healthcheck"]["test"][-1].endswith("/healthz")
    assert list(compose["services"]) == ["control-plane"]
    assert compose["volumes"]["termflow-data"] == {"name": "${TERMFLOW_DATA_VOLUME:-termflow-data}"}
    assert compose["volumes"]["termflow-totp-key"] == {
        "name": "${TERMFLOW_TOTP_KEY_VOLUME:-termflow-totp-key}"
    }


def test_compose_configures_same_origin_web_control_limits() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    environment = compose["services"]["control-plane"]["environment"]
    assert environment["TERMFLOW_STATIC_DIR"] == "/app/frontend-dist"
    assert environment["TERMFLOW_ALLOW_INSECURE_LOOPBACK"] == (
        "${TERMFLOW_ALLOW_INSECURE_LOOPBACK:-true}"
    )
    assert "TERMFLOW_PUBLIC_BASE_URL" in environment
    assert "TERMFLOW_TRUSTED_WEB_ORIGINS" not in environment
    assert "TERMFLOW_BROWSER_SESSION_TTL_SECONDS" in environment
    assert environment["TERMFLOW_TOTP_MASTER_KEY"] is None
    assert environment["TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE"] == (
        "/app/totp-secrets/totp-master-key"
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
    assert "mkdir -p /app/data /app/totp-secrets" in dockerfile
    assert "termflow-control-entrypoint" in dockerfile

    entrypoint = Path("deploy/entrypoint.control-plane.sh").read_text()
    assert "data_dir=/app/data" in entrypoint
    assert "totp_dir=/app/totp-secrets" in entrypoint
    assert "-L" in entrypoint  # refuse symlinked mount points
    assert "-xdev" in entrypoint  # never recurse across filesystems
    assert "setpriv" in entrypoint  # exec drop keeps PID 1 non-root

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


def test_node_image_initializes_managed_mounts_then_drops_privileges() -> None:
    dockerfile = Path("deploy/Dockerfile.node").read_text()
    runtime = dockerfile.split("FROM python:3.12.11-slim-bookworm AS runtime", maxsplit=1)[1]
    entrypoint = Path("deploy/entrypoint.node.sh").read_text()
    verifier = Path("scripts/verify-node-image.sh").read_text()

    assert "USER termflow" not in runtime
    assert "home_dir=/home/termflow" in entrypoint
    assert "work_dir=/work" in entrypoint
    assert "-L" in entrypoint  # refuse symlinked mount points
    assert "-xdev" in entrypoint  # never recurse across filesystems
    assert "setpriv" in entrypoint  # re-exec before login/tmux/Bridge startup
    for optional_environment in (
        "TERMFLOW_SERVER",
        "TERMFLOW_CODE",
        "TERMFLOW_ALLOW_INSECURE_HTTP",
        "TERMFLOW_NEW",
    ):
        assert f"${{{optional_environment}:-}}" in entrypoint

    for expected in (
        "root-owned bind mounts",
        "stat -c %u /proc/1",
        "CapEff",
        "--cap-add CHOWN",
        "--cap-add DAC_OVERRIDE",
        "--cap-add SETUID",
        "--cap-add SETGID",
        "docker exec --user termflow",
    ):
        assert expected in verifier


def test_node_entrypoint_selects_only_supported_term_shells() -> None:
    entrypoint = Path("deploy/entrypoint.node.sh").read_text()

    assert 'case "${TERMFLOW_SHELL:-bash}" in' in entrypoint
    assert "bash)\n        SHELL=/bin/bash" in entrypoint
    assert "sh)\n        SHELL=/bin/sh" in entrypoint
    assert 'echo "invalid TERMFLOW_SHELL: expected bash or sh" >&2' in entrypoint
    assert "exit 64" in entrypoint
    assert "export SHELL" in entrypoint
    assert entrypoint.index('case "${TERMFLOW_SHELL:-bash}" in') > entrypoint.index(
        'cd "${work_dir}"'
    )
    assert entrypoint.index("export SHELL") < entrypoint.index(
        'if [ ! -f "${HOME}/.config/termflow/config.json" ]'
    )


def test_node_image_verifier_proves_the_actual_tmux_shell() -> None:
    verifier = Path("scripts/verify-node-image.sh").read_text()

    for expected in (
        "TERMFLOW_SHELL=sh",
        "TERMFLOW_SHELL=zsh",
        "#{pane_current_command}",
        'test "$(pane_shell "${first_status}")" = "bash"',
        'test "$(pane_shell "${third_status}")" = "sh"',
        "invalid TERMFLOW_SHELL: expected bash or sh",
    ):
        assert expected in verifier


def test_readme_docker_node_uses_local_managed_directories() -> None:
    readme = Path("README.md").read_text()

    assert "mkdir -p termflow-node-identity termflow-node-work" in readme
    assert '--volume "$PWD/termflow-node-identity:/home/termflow"' in readme
    assert '--volume "$PWD/termflow-node-work:/work"' in readme
    assert "docker volume create termflow-node-identity" not in readme
    assert "docker volume create termflow-node-work" not in readme
    for capability in ("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID"):
        assert f"--cap-add {capability}" in readme
    docker_run = readme.split("docker run -d \\\n", maxsplit=1)[1].split(
        "ghcr.io/mcocdaa/termflow-node:v0.1.0", maxsplit=1
    )[0]
    assert "--user" not in docker_run
    assert (
        "docker exec --user termflow -it termflow-node termflow attach demo"
        in readme
    )
    assert "Web C 进入 Docker A 的 Term 默认使用 Bash" in readme
    assert "--env TERMFLOW_SHELL=sh" in readme
    assert "只接受 `bash` 和 `sh`" in readme
    assert "重新创建 Docker A 容器" in readme


def test_docker_context_excludes_local_state_and_frontend_build_output() -> None:
    ignored = Path(".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".venv" in ignored
    assert ".worktrees" in ignored
    assert "**/node_modules" in ignored
    assert "**/dist" in ignored
    assert "apps/node/tests" in ignored
    assert "apps/clients/tauri" in ignored
    assert "**/target" in ignored
    assert "apps/node/src" not in ignored


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
        "termflow-control-entrypoint",
        "/app/totp-secrets",
        "stat -c %u /proc/1",
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

    node_check = Path("scripts/verify-node-image.sh").read_text()
    for expected in (
        "termflow serve --name demo",
        '"bridge_alive":true',
        "ExitCode",
        "single_instance",
    ):
        assert expected in node_check

    for command in ("cargo fmt", "cargo clippy", "cargo test", "cargo check", "--no-bundle"):
        assert command in tauri_check
    assert "project is not present" not in tauri_check
    assert 'node-version: "22.23.2"' in workflow
    assert "dtolnay/rust-toolchain@4360b52568e2003a75bf9bc1d59f33a8e3fc893c" in workflow
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
