"""Opt-in, LLM-free lifecycle test for the pinned OpenCode container.

The test deliberately exercises only the runtime HTTP surface and persistent
session lifecycle; it never sends a model request.  It is skipped unless
``TERMFLOW_E2E_OPENCODE=1`` is set. The fixture starts the complete disposable
B + OpenCode slice, injects one generated bootstrap MCP capability into both,
and overlays unique physical volume names.  It confirms Compose ownership
before it removes only those test resources during teardown.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


ROOT = Path(__file__).resolve().parents[2]
_BASIC_AUTH = (
    'auth="$(printf \'%s:%s\' "$OPENCODE_SERVER_USERNAME" '
    '"$OPENCODE_SERVER_PASSWORD" | base64 | tr -d \'\\n\')"; '
)


def _with_basic_auth(command: str) -> str:
    return f"{_BASIC_AUTH}{command}"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _compose_args(project: str) -> list[str]:
    args = ["docker", "compose", "-p", project, "-f", str(ROOT / "deploy" / "compose.yaml")]
    if override := os.environ.get("TERMFLOW_E2E_COMPOSE_OVERRIDE"):
        args.extend(["-f", override])
    return args


def _run_compose(project: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_compose_args(project), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
        timeout=180,
    )


def _safe_project_state(project: str) -> str:
    """Return service lifecycle fields only; never inspect environment/logs."""

    found = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    states: list[str] = []
    for container_id in found.stdout.split():
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                (
                    '{{index .Config.Labels "com.docker.compose.service"}}'
                    "|{{.State.Status}}|"
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
                    "|{{.State.ExitCode}}"
                ),
                container_id,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        if inspected.returncode == 0:
            states.append(inspected.stdout.strip())
    return ", ".join(sorted(states)) or "no project containers"


def _safe_service_log(project: str, service: str, env: dict[str, str]) -> str:
    """Return a bounded startup tail after removing every fixture value."""

    result = _run_compose(
        project,
        "logs",
        "--no-color",
        "--tail",
        "80",
        service,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    for value in env.values():
        if value:
            output = output.replace(value, "<redacted>")
    output = re.sub(
        r"(?i)(authorization|bearer|token|api[_-]?key|password|secret)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        output,
    )
    return output[-4_000:].strip() or "no service log output"


@pytest.fixture
def opencode_compose_project(tmp_path) -> Iterator[str]:
    if os.environ.get("TERMFLOW_E2E_OPENCODE") != "1":
        pytest.skip("set TERMFLOW_E2E_OPENCODE=1 to run the Docker OpenCode smoke test")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    try:
        docker_info = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker daemon is unavailable")
    if docker_info.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    project = f"termflow-opencode-e2e-{uuid.uuid4().hex[:12]}"
    override = tmp_path / "compose.e2e-volumes.yaml"
    override.write_text(
        "volumes:\n"
        f"  termflow-data:\n    name: {project}-data\n"
        f"  termflow-totp-key:\n    name: {project}-totp-key\n"
        f"  opencode-data:\n    name: {project}-opencode-data\n"
    )
    # Every credential is generated for this disposable project.  In
    # particular, never fall back to a host `.env`: doing so could expose a
    # durable deployment secret to an opt-in test container.
    env = {
        "COMPOSE_PROJECT_NAME": project,
        "TERMFLOW_ADMIN_TOKEN": secrets.token_urlsafe(32),
        "OPENCODE_SERVER_USERNAME": f"termflow-e2e-{uuid.uuid4().hex[:8]}",
        "OPENCODE_SERVER_PASSWORD": secrets.token_urlsafe(32),
        "OPENCODE_AGENT_MCP_TOKEN": secrets.token_urlsafe(32),
        "TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN": secrets.token_urlsafe(32),
        "TERMFLOW_HOST_PORT": str(_free_port()),
        "TERMFLOW_E2E_COMPOSE_OVERRIDE": str(override),
    }
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        _assert_project_unused(project, env)
        # Render the exact disposable configuration before any lifecycle
        # command.  This catches missing interpolation and records no secret
        # values in test output.
        _run_compose(project, "config", "--quiet")
        started = _run_compose(
            project,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "60",
            "control-plane",
            "opencode-init",
            "opencode-agent",
            check=False,
        )
        assert started.returncode == 0, (
            "disposable Compose services did not become ready: "
            f"{_safe_project_state(project)}; "
            f"opencode log: {_safe_service_log(project, 'opencode-agent', env)}"
        )
        containers = _resource_ids(project, env)
        # The project must contain the complete B + init + OpenCode slice;
        # service labels are checked by the security inspector below, while
        # this fixture records the full (non-truncated) IDs for ownership.
        assert len(containers) == 3
        _assert_owned(project, containers, env)
        networks = _network_ids(project, env)
        assert len(networks) == 2
        _assert_owned_networks(project, networks, env)
        yield project
    finally:
        # Teardown is scoped to resources discovered after this test's render;
        # if ownership cannot be proven, leave them for manual inspection
        # instead of risking a similarly named durable volume.
        containers = _resource_ids(project, env)
        volumes = _volume_ids(project, env)
        networks = _network_ids(project, env)
        if containers or volumes or networks:
            _assert_owned(project, containers, env)
            _assert_owned_volumes(project, volumes, env)
            _assert_owned_networks(project, networks, env)
            _run_compose(project, "down", "--volumes", "--remove-orphans", check=False)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _assert_project_unused(project: str, env: dict[str, str]) -> None:
    commands = (
        [
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
        [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=os.environ | env,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        assert not result.stdout.split(), "random Compose project already exists"


def _resource_ids(project: str, env: dict[str, str]) -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        cwd=ROOT,
        env=os.environ | env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        return []
    return [item for item in result.stdout.split() if item]


def _volume_ids(project: str, env: dict[str, str]) -> list[str]:
    result: list[str] = []
    for volume in (
        f"{project}-data",
        f"{project}-totp-key",
        f"{project}-opencode-data",
    ):
        inspected = subprocess.run(
            ["docker", "volume", "inspect", "--format", "{{.Name}}", volume],
            cwd=ROOT,
            env=os.environ | env,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        if inspected.returncode == 0 and inspected.stdout.strip() == volume:
            result.append(volume)
    return result


def _network_ids(project: str, env: dict[str, str]) -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        cwd=ROOT,
        env=os.environ | env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    return result.stdout.split() if result.returncode == 0 else []


def _assert_owned(project: str, containers: list[str], env: dict[str, str]) -> None:
    for container in containers:
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "com.docker.compose.project" }}',
                container,
            ],
            cwd=ROOT,
            env=os.environ | env,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        assert inspected.stdout.strip() == project


def _assert_owned_volumes(project: str, volumes: list[str], env: dict[str, str]) -> None:
    for volume in volumes:
        inspected = subprocess.run(
            [
                "docker",
                "volume",
                "inspect",
                "--format",
                '{{ index .Labels "com.docker.compose.project" }}',
                volume,
            ],
            cwd=ROOT,
            env=os.environ | env,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        assert inspected.stdout.strip() == project


def _assert_owned_networks(project: str, networks: list[str], env: dict[str, str]) -> None:
    for network in networks:
        inspected = subprocess.run(
            [
                "docker",
                "network",
                "inspect",
                "--format",
                '{{ index .Labels "com.docker.compose.project" }}',
                network,
            ],
            cwd=ROOT,
            env=os.environ | env,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        assert inspected.stdout.strip() == project


def _exec_wget(project: str, script: str) -> str:
    result = _run_compose(
        project,
        "exec",
        "-T",
        "opencode-agent",
        "sh",
        "-ec",
        script,
    )
    return result.stdout.strip()


def test_pinned_opencode_health_session_resume_abort_and_delete(
    opencode_compose_project,
) -> None:
    project = opencode_compose_project
    security = subprocess.run(
        [
            str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
            "--mode",
            "offline",
            project,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert security.returncode == 0, security.stdout + security.stderr
    assert "agent-container-security: PASS" in security.stdout

    health_raw = _exec_wget(
        project,
        _with_basic_auth(
            'wget -q -O- --header "Authorization: Basic $auth" http://127.0.0.1:4096/global/health'
        ),
    )
    health = json.loads(health_raw)
    assert health.get("healthy") is True

    event_raw = _exec_wget(
        project,
        _with_basic_auth(
            'output="$(timeout 3 wget -q -O- --header "Authorization: Basic $auth" '
            'http://127.0.0.1:4096/global/event || true)"; '
            'printf "%s" "$output"'
        ),
    )
    assert "data:" in event_raw
    assert "server.connected" in event_raw

    session_raw = _exec_wget(
        project,
        _with_basic_auth(
            'wget -q -O- --header "Authorization: Basic $auth" '
            '--header "Content-Type: application/json" '
            '--post-data \'{"title":"termflow-e2e-smoke"}\' '
            "http://127.0.0.1:4096/session?directory=%2Ftmp"
        ),
    )
    session = json.loads(session_raw)
    session_id = session.get("id")
    assert isinstance(session_id, str) and session_id
    assert session.get("directory") == "/tmp"

    prompt_raw = _exec_wget(
        project,
        _with_basic_auth(
            'wget -q -O- --header "Authorization: Basic $auth" '
            '--header "Content-Type: application/json" '
            '--post-data \'{"parts":[{"type":"text","text":"do not call a model"}]}\' '
            "http://127.0.0.1:4096/session/" + session_id + "/prompt_async?directory=%2Ftmp"
        ),
    )
    # The pinned API returns 204 for prompt_async; an empty body is expected.
    assert prompt_raw == ""

    abort_raw = _exec_wget(
        project,
        _with_basic_auth(
            'wget -q -O- --header "Authorization: Basic $auth" '
            "--post-data '' http://127.0.0.1:4096/session/" + session_id + "/abort?directory=%2Ftmp"
        ),
    )
    assert json.loads(abort_raw) is True

    _run_compose(project, "restart", "opencode-agent")
    _run_compose(
        project,
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "60",
        "opencode-agent",
    )
    resumed_raw = _exec_wget(
        project,
        _with_basic_auth(
            'wget -q -O- --header "Authorization: Basic $auth" '
            "http://127.0.0.1:4096/session/" + session_id + "?directory=%2Ftmp"
        ),
    )
    resumed = json.loads(resumed_raw)
    assert resumed.get("id") == session_id
    assert resumed.get("directory") == "/tmp"

    delete_raw = _exec_wget(
        project,
        _with_basic_auth(
            "printf 'DELETE /session/"
            + session_id
            + "?directory=%%2Ftmp HTTP/1.1\\r\\nHost: 127.0.0.1:4096\\r\\n"
            + 'Authorization: Basic %s\\r\\nConnection: close\\r\\n\\r\\n\' "$auth" '
            + "| nc -w 5 127.0.0.1 4096"
        ),
    )
    assert delete_raw.startswith("HTTP/1.1 200"), delete_raw
    assert delete_raw.rstrip().endswith("true"), delete_raw
    assert project.startswith("termflow-opencode-e2e-")


def test_container_reports_termflow_mcp_connected() -> None:
    """Read-only check against an explicitly supplied, already-live deployment."""
    project = os.environ.get("TERMFLOW_E2E_AGENT_LIVE_PROJECT")
    if not project:
        pytest.skip("set TERMFLOW_E2E_AGENT_LIVE_PROJECT for a read-only live MCP check")
    username = os.environ.get("OPENCODE_SERVER_USERNAME")
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    if not username or not password:
        pytest.skip("set OpenCode basic-auth values for the read-only live MCP check")
    found = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=opencode-agent",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    ids = [line for line in found.stdout.splitlines() if line]
    assert found.returncode == 0 and len(ids) == 1
    mcp_raw = subprocess.run(
        [
            "docker",
            "exec",
            ids[0],
            "sh",
            "-ec",
            'auth="$(printf \'%s:%s\' "$OPENCODE_SERVER_USERNAME" '
            '"$OPENCODE_SERVER_PASSWORD" | base64 | tr -d \'\\n\')"; '
            'wget -q -O- --header "Authorization: Basic $auth" '
            "http://127.0.0.1:4096/mcp?directory=%2Ftmp",
        ],
        env=os.environ
        | {"OPENCODE_SERVER_USERNAME": username, "OPENCODE_SERVER_PASSWORD": password},
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert mcp_raw.returncode == 0, mcp_raw.stderr
    mcp = json.loads(mcp_raw.stdout)
    assert mcp["termflow"]["status"] == "connected"
