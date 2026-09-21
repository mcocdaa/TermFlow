import json
from pathlib import Path

import yaml


def test_docker_a_and_b_use_a_host_reachable_bridge_not_an_internal_network() -> None:
    readme = Path("README.md").read_text()

    assert "docker network create termflow-net" in readme
    assert "docker network create --internal termflow-net" not in readme
    assert "A 主动连接 B" in readme
    assert "A 不开放端口" in readme
    assert "B 的 Web/API 通过宿主机 loopback 端口发布" in readme


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
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert set(service["cap_add"]) == {
        "CHOWN",
        "DAC_OVERRIDE",
        "SETUID",
        "SETGID",
        "SETPCAP",
    }
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert "/tmp:size=64m,mode=1777" in service["tmpfs"]
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
    assert agent_init["command"] == ["chown 0:0 /data && chmod 0700 /data && chown 405:100 /data"]

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
    # The pinned binary consumes provider-specific credentials. A generic
    # OPENCODE_MODEL_API_KEY is ignored and would make a deployment look
    # configured while every model call still fails authentication.
    assert "OPENCODE_MODEL_API_KEY" not in agent["environment"]
    assert "ANTHROPIC_API_KEY" not in agent["environment"]
    assert "OPENAI_API_KEY" not in agent["environment"]
    assert agent["volumes"] == [
        "opencode-data:/data",
        {
            "type": "bind",
            "source": "./opencode-config.yaml",
            "target": "/etc/termflow/opencode-config.yaml",
            "read_only": True,
            "bind": {"create_host_path": False},
        },
    ]
    assert agent["depends_on"] == {"opencode-init": {"condition": "service_completed_successfully"}}
    home_tmpfs = next(entry for entry in agent["tmpfs"] if entry.startswith("/home/opencode:"))
    assert "uid=405" in home_tmpfs
    assert "gid=100" in home_tmpfs
    assert "mode=0700" in home_tmpfs
    healthcheck = agent["healthcheck"]["test"][1]
    assert "OPENCODE_SERVER_USERNAME" in healthcheck
    assert "Authorization: Basic" in healthcheck
    assert "| base64 | tr -d '\\n'" in healthcheck
    assert "wget -Y off" in healthcheck
    assert "--user" not in healthcheck
    assert "--password" not in healthcheck

    opencode_config_text = Path("deploy/opencode-config.yaml").read_text()
    assert not any(line.lstrip().startswith("#") for line in opencode_config_text.splitlines())
    opencode_config = json.loads(opencode_config_text)
    assert opencode_config["permission"]["*"] == "deny"
    # OpenCode namespaces remote MCP tools as ``<server>_<tool>``.  The
    # wildcard therefore covers the actual ``termflow_termflow_*`` names.
    # OpenCode must not add a second pre-MCP approval: B cannot create its
    # exact-argument ApprovalRequest until the MCP call reaches B.  B remains
    # the sole terminal authority and performs the single-use write approval.
    assert opencode_config["permission"]["termflow_*"] == "allow"
    # The TermFlow MCP server entry rides OpenCode's native {env:}
    # interpolation (spike-verified on the pinned image): no raw token ever
    # lands in the frozen config or the image.
    termflow_mcp = opencode_config["mcp"]["termflow"]
    assert termflow_mcp["type"] == "remote"
    assert termflow_mcp["enabled"] is True
    assert termflow_mcp["url"] == "{env:TERMFLOW_AGENT_MCP_URL}"
    assert termflow_mcp["headers"]["Authorization"] == ("Bearer {env:TERMFLOW_AGENT_MCP_TOKEN}")
    # The container receives the same required bootstrap capability as B.  A
    # deployment without an Agent binding must fail closed during preflight,
    # rather than booting a runtime that can never reach MCP.
    assert agent["environment"]["TERMFLOW_AGENT_MCP_URL"] == (
        "http://control-plane:8000/api/v1/agent/mcp"
    )
    assert agent["environment"]["TERMFLOW_AGENT_MCP_TOKEN"] == (
        "${OPENCODE_AGENT_MCP_TOKEN:?set OPENCODE_AGENT_MCP_TOKEN}"
    )

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
    assert environment["TERMFLOW_AGENT_OPENCODE_MCP_TOKEN"] == (
        "${OPENCODE_AGENT_MCP_TOKEN:?set OPENCODE_AGENT_MCP_TOKEN}"
    )
    assert environment["TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN"] == (
        "${TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN:-}"
    )
    # The reference OpenCode container has a read-only root filesystem and no
    # /workspace mount.  Sessions must use its existing writable tmpfs instead
    # of failing every real prompt with FileSystem.realPath(/workspace).
    assert environment["TERMFLOW_AGENT_OPENCODE_DIRECTORY"] == (
        "${TERMFLOW_AGENT_OPENCODE_DIRECTORY:-/tmp}"
    )

    override = yaml.safe_load(Path("deploy/compose.totp-secret.yaml").read_text())
    assert override["services"]["control-plane"]["environment"][
        "TERMFLOW_TOTP_MASTER_KEY_FILE"
    ] == ("/run/secrets/termflow-totp-master-key")

    assert "stt-speaches" not in compose["services"]
    assert "termflow_stt_" not in text
    assert "verify-stt" not in verify
    assert not Path("deploy/compose.dev.yaml").exists()


def test_release_compose_uses_published_images_without_building() -> None:
    release = yaml.safe_load(Path("deploy/compose.release.yaml").read_text())
    base = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    overlay = yaml.safe_load(Path("deploy/compose.agent-live.yaml").read_text())

    services = release["services"]
    base_services = base["services"]
    overlay_services = overlay["services"]
    assert set(services) == set(base_services)
    assert "provider-egress-proxy" not in services
    for service in services.values():
        assert "build" not in service

    control_plane = services["control-plane"]
    assert control_plane["image"] == (
        "${TERMFLOW_RELEASE_IMAGE_REPOSITORY:-ghcr.io/mcocdaa/termflow-control-plane}"
        ":${TERMFLOW_RELEASE_IMAGE_TAG:?set TERMFLOW_RELEASE_IMAGE_TAG in .env}"
    )
    for key in (
        "read_only",
        "cap_drop",
        "cap_add",
        "security_opt",
        "tmpfs",
        "command",
        "ports",
        "volumes",
        "networks",
        "logging",
        "healthcheck",
    ):
        assert control_plane[key] == base_services["control-plane"][key]

    base_env = base_services["control-plane"]["environment"]
    overlay_env = overlay_services["control-plane"]["environment"]
    release_env = control_plane["environment"]
    assert set(release_env) == set(base_env) | set(overlay_env)
    for key, value in base_env.items():
        if key in overlay_env:
            continue
        assert release_env[key] == value
    for key, value in overlay_env.items():
        assert release_env[key] == value

    agent = services["opencode-agent"]
    base_agent = base_services["opencode-agent"]
    assert agent["image"] == base_agent["image"]
    assert agent["depends_on"] == base_agent["depends_on"]
    assert agent["networks"] == ["agent_internal", "provider_uplink"]
    for key in (
        "command",
        "user",
        "cap_drop",
        "security_opt",
        "read_only",
        "tmpfs",
        "mem_limit",
        "cpus",
        "pids_limit",
        "ulimits",
        "healthcheck",
    ):
        assert agent[key] == base_agent[key]
    assert agent["volumes"][0] == "termflow-opencode-data:/data"
    assert agent["volumes"][1] == base_agent["volumes"][1]
    base_agent_env = base_agent["environment"]
    overlay_agent_env = overlay_services["opencode-agent"]["environment"]
    assert set(agent["environment"]) == set(base_agent_env) | set(overlay_agent_env)
    for key, value in {**base_agent_env, **overlay_agent_env}.items():
        assert agent["environment"][key] == value

    init = services["opencode-init"]
    assert init["volumes"] == ["termflow-opencode-data:/data"]
    assert {key: value for key, value in init.items() if key != "volumes"} == {
        key: value for key, value in base_services["opencode-init"].items() if key != "volumes"
    }
    assert {spec["name"] for spec in release["volumes"].values()} == {
        "termflow-data",
        "termflow-totp-key",
        "termflow-opencode-data",
    }
    networks = release["networks"]
    assert networks["default"]["internal"] is False
    assert networks["agent_internal"]["internal"] is True
    assert networks["provider_uplink"]["internal"] is False
    assert "provider_egress" not in networks

    readme = Path("README.md").read_text()
    assert "compose.release.yaml" in readme
    assert "TERMFLOW_RELEASE_IMAGE_TAG" in readme
    assert "compose.release.yaml" in Path("docs/operations.md").read_text()


def test_delivery_scripts_verify_artifact_contents_and_local_state() -> None:
    verify = Path("scripts/verify.sh").read_text()
    for required in ("pytest -q", "ruff check .", "mypy", "docker compose"):
        assert required in verify
    assert 'TERMFLOW_ADMIN_TOKEN="verify-admin-token-that-is-long-enough"' in verify
    assert "docker compose -f deploy/compose.yaml config --quiet" in verify
    assert 'verify-agent-containers.sh --mode "${TERMFLOW_SECURITY_MODE}"' in verify
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
    assert "totp_dir=/app/totp-secrets" in entrypoint
    assert "-L" in entrypoint  # refuse symlinked mount points
    assert "-xdev" in entrypoint  # never recurse across filesystems
    assert "setpriv" in entrypoint  # exec drop keeps PID 1 non-root
    assert "--bounding-set=-all" in entrypoint
    assert "--inh-caps=-all" in entrypoint
    assert "--ambient-caps=-all" in entrypoint

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
