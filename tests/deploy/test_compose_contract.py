from pathlib import Path

import yaml


def test_compose_keeps_the_hardened_single_worker_deployable_shape() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]
    agent = compose["services"]["opencode-agent"]
    text = Path("deploy/compose.yaml").read_text().lower()
    verify = Path("scripts/verify.sh").read_text().lower()

    assert service["build"] == {
        "context": "..",
        "dockerfile": "deploy/Dockerfile.control-plane",
    }
    assert "image" not in service
    assert "--workers" not in " ".join(service["command"])
    assert service["volumes"] == [
        "termflow-data:/app/data",
        "termflow-totp-key:/app/totp-secrets",
    ]
    assert service["healthcheck"]["test"][-1].endswith("/healthz")
    assert list(compose["services"]) == ["control-plane", "opencode-agent"]

    assert agent["networks"] == ["agent_internal"]
    assert "ports" not in agent
    assert agent["cap_drop"] == ["ALL"]
    assert agent["read_only"] is True
    assert "@sha256:" in agent["image"]
    assert "PLACEHOLDER" not in agent["image"]
    assert agent["user"] == "405:100"
    assert agent["environment"]["HOME"] == "/home/opencode"
    assert any("OPENCODE_SERVER_" in key for key in agent["environment"])
    assert "OPENCODE_SERVER_USERNAME" in agent["healthcheck"]["test"][1]

    environment = service["environment"]
    assert environment["TERMFLOW_STATIC_DIR"] == "/app/frontend-dist"
    assert environment["TERMFLOW_ALLOW_INSECURE_LOOPBACK"] == (
        "${TERMFLOW_ALLOW_INSECURE_LOOPBACK:-true}"
    )
    assert "TERMFLOW_TRUSTED_WEB_ORIGINS" not in environment
    assert environment["TERMFLOW_TOTP_MASTER_KEY"] is None
    assert environment["TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE"] == (
        "/app/totp-secrets/totp-master-key"
    )

    override = yaml.safe_load(Path("deploy/compose.totp-secret.yaml").read_text())
    assert override["services"]["control-plane"]["environment"][
        "TERMFLOW_TOTP_MASTER_KEY_FILE"
    ] == ("/run/secrets/termflow-totp-master-key")

    assert "stt-speaches" not in compose["services"]
    assert "termflow_stt_" not in text
    assert "verify-stt" not in verify
    assert not Path("deploy/compose.dev.yaml").exists()


def test_delivery_scripts_verify_artifact_contents_and_local_state() -> None:
    verify = Path("scripts/verify.sh").read_text()
    for required in ("pytest -q", "ruff check .", "mypy", "docker compose"):
        assert required in verify
    assert 'TERMFLOW_ADMIN_TOKEN="verify-admin-token-that-is-long-enough"' in verify
    assert "docker compose -f deploy/compose.yaml config --quiet" in verify
    assert "verify-stt" not in verify.lower()

    verifier = Path("scripts/release/verify_control_plane_release_image.sh").read_text()
    workflow = Path(".github/workflows/ci.yml").read_text()
    assert "docker run --detach" in verifier
    assert "docker volume create" in verifier
    assert "TERMFLOW_IMAGE" not in verifier
    assert "http://127.0.0.1:18076/healthz" in verifier
    assert "verify_control_plane_release_image.sh termflow-control-plane:ci" in workflow

    dockerfile = Path("deploy/Dockerfile.control-plane").read_text()
    assert "FROM node:22.23.2-bookworm-slim AS web" in dockerfile
    assert "COPY --from=web" in dockerfile
    assert "uv build --wheel --package termflow-protocol" in dockerfile
    runtime = dockerfile.split("FROM python:3.12-slim AS runtime", maxsplit=1)[1]
    assert "COPY --from=python-wheels /opt/termflow /opt/termflow" in runtime
    for forbidden in ("COPY packages", "COPY apps", "npm ", "cargo ", "rust"):
        assert forbidden not in runtime.lower()

    entrypoint = Path("deploy/entrypoint.control-plane.sh").read_text()
    assert "data_dir=/app/data" in entrypoint
    assert "setpriv" in entrypoint

    node_entrypoint = Path("deploy/entrypoint.node.sh").read_text()
    assert "home_dir=/home/termflow" in node_entrypoint
    assert "setpriv" in node_entrypoint
    assert "TERMFLOW_SHELL=sh" in Path("scripts/verify-node-image.sh").read_text()

    ignored = Path(".dockerignore").read_text().splitlines()
    for excluded in (".env", ".venv", ".worktrees", "**/node_modules", "**/target"):
        assert excluded in ignored
    assert "apps/node/src" not in ignored

    image_check = Path("scripts/verify-control-plane-image.sh").read_text()
    for expected in (
        "termflow_control_plane",
        "/app/frontend-dist/index.html",
        "/opt/termflow/bin/termflow-control",
        "stat -c %u /proc/1",
    ):
        assert expected in image_check

    tauri_check = Path("scripts/verify-tauri.sh").read_text()
    for command in ("cargo fmt", "cargo clippy", "cargo test"):
        assert command in tauri_check
    assert 'node-version: "22.23.2"' in workflow

    readme = Path("README.md").read_text()
    assert "mkdir -p termflow-node-identity termflow-node-work" in readme
    assert '--volume "$PWD/termflow-node-identity:/home/termflow"' in readme