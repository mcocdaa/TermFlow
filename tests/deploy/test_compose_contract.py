from pathlib import Path

import yaml


def test_compose_is_single_worker_and_persists_only_metadata() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]
    assert "--workers" not in " ".join(service["command"])
    assert service["volumes"] == ["termflow-data:/app/data"]
    assert service["healthcheck"]["test"][-1].endswith("/healthz")
    assert list(compose["services"]) == ["control-plane"]


def test_compose_configures_same_origin_web_control_limits() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    environment = compose["services"]["control-plane"]["environment"]
    assert environment["TERMFLOW_STATIC_DIR"] == "/app/web"
    assert "TERMFLOW_PUBLIC_BASE_URL" in environment
    assert "TERMFLOW_TRUSTED_WEB_ORIGINS" in environment
    assert "TERMFLOW_BROWSER_SESSION_TTL_SECONDS" in environment
    assert "TERMFLOW_TERMINAL_MAX_FRAME_BYTES" in environment


def test_control_plane_image_builds_web_without_shipping_node() -> None:
    dockerfile = Path("deploy/Dockerfile.control-plane").read_text()
    assert "FROM node:22" in dockerfile
    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    assert "COPY --from=web" in dockerfile
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert "USER termflow" in dockerfile


def test_docker_context_excludes_local_state_and_frontend_build_output() -> None:
    ignored = Path(".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".venv" in ignored
    assert ".worktrees" in ignored
    assert "**/node_modules" in ignored
    assert "**/dist" in ignored
