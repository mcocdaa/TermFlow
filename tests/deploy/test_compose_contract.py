import json
from pathlib import Path

import yaml


def test_compose_keeps_the_hardened_single_worker_deployable_shape() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    services = compose["services"]
    service = services["control-plane"]
    agent = services["opencode-agent"]
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
    assert list(services) == [
        "control-plane",
        "opencode-init",
        "opencode-agent",
    ]

    agent_init = services["opencode-init"]
    assert agent_init["image"] == agent["image"]
    assert agent_init["network_mode"] == "none"
    assert agent_init["user"] == "0:0"
    assert agent_init["cap_drop"] == ["ALL"]
    assert agent_init["cap_add"] == ["CHOWN"]
    assert agent_init["read_only"] is True
    assert agent_init["restart"] == "no"
    assert agent_init["volumes"] == ["opencode-data:/data"]
    assert agent_init["command"] == [
        "chown 0:0 /data && chmod 0700 /data && chown 405:100 /data"
    ]

    assert agent["networks"] == ["agent_internal"]
    assert "ports" not in agent
    assert agent["cap_drop"] == ["ALL"]
    assert agent["read_only"] is True
    assert "@sha256:" in agent["image"]
    assert "PLACEHOLDER" not in agent["image"]
    assert agent["user"] == "405:100"
    assert agent["environment"]["HOME"] == "/home/opencode"
    assert agent["environment"].get("XDG_DATA_HOME") == "/data"
    assert any("OPENCODE_SERVER_" in key for key in agent["environment"])
    assert agent["volumes"] == [
        "opencode-data:/data",
        "./opencode-config.yaml:/etc/termflow/opencode-config.yaml:ro",
    ]
    assert agent["depends_on"] == {
        "opencode-init": {"condition": "service_completed_successfully"}
    }
    home_tmpfs = next(entry for entry in agent["tmpfs"] if entry.startswith("/home/opencode:"))
    assert "uid=405" in home_tmpfs
    assert "gid=100" in home_tmpfs
    assert "mode=0700" in home_tmpfs
    healthcheck = agent["healthcheck"]["test"][1]
    assert "OPENCODE_SERVER_USERNAME" in healthcheck
    assert "Authorization: Basic" in healthcheck
    assert "| base64" in healthcheck
    assert "--user" not in healthcheck
    assert "--password" not in healthcheck

    opencode_config_text = Path("deploy/opencode-config.yaml").read_text()
    assert not any(line.lstrip().startswith("#") for line in opencode_config_text.splitlines())
    opencode_config = json.loads(opencode_config_text)
    assert opencode_config["permission"]["*"] == "deny"
    assert {
        name for name in opencode_config["permission"] if name.startswith("termflow_")
    } == {
        "termflow_list_panes",
        "termflow_pane_read",
        "termflow_pane_send_text",
        "termflow_pane_send_keys",
        "termflow_watch_create",
        "termflow_watch_list",
        "termflow_watch_get",
        "termflow_watch_cancel",
    }

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
    # In-container OpenCode reaches B by the agent_internal service name, so
    # the loopback-only MCP allowlist defaults must be overridden (plan §16).
    assert environment["TERMFLOW_AGENT_MCP_ALLOWED_HOSTS"] == (
        "${TERMFLOW_AGENT_MCP_ALLOWED_HOSTS:-control-plane:8000}"
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
