"""Opt-in provider-egress checks for a disposable full Agent deployment.

The fixture is intentionally disabled by default.  When enabled it owns a
random Compose project and unique named volumes, starts B + OpenCode + the
allowlist proxy together, and proves ownership before its scoped teardown.
No stable project or pre-existing volume is ever addressed by this module.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass(frozen=True, slots=True)
class EgressProject:
    name: str
    env: dict[str, str] = field(repr=False)
    volumes: tuple[str, ...]
    containers: tuple[str, ...]
    networks: tuple[str, ...]


def _compose_args(project: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(ROOT / "deploy" / "compose.yaml"),
        "-f",
        str(ROOT / "deploy" / "compose.agent-live.yaml"),
    ]


def _run(
    project: str, env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_compose_args(project), *args],
        cwd=ROOT,
        env=os.environ | env,
        text=True,
        capture_output=True,
        check=check,
        timeout=180,
    )


def _random_secret() -> str:
    return secrets.token_urlsafe(32)


def _safe_project_state(project: str, env: dict[str, str]) -> str:
    """Return lifecycle fields only; never inspect container environments."""

    found = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        env=os.environ | env,
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
            env=os.environ | env,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        if inspected.returncode == 0:
            states.append(inspected.stdout.strip())
    return ", ".join(sorted(states)) or "no project containers"


def _safe_service_log(
    compose: list[str], service: str, env: dict[str, str]
) -> str:
    """Return a bounded startup tail after removing fixture values."""

    result = subprocess.run(
        [*compose, "logs", "--no-color", "--tail", "80", service],
        env=os.environ | env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    output = f"{result.stdout}\n{result.stderr}"
    for value in sorted(env.values(), key=len, reverse=True):
        if value:
            output = output.replace(value, "<redacted>")
    output = re.sub(
        r"(?i)(authorization|bearer|token|api[_-]?key|password|secret)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        output,
    )
    return output[-4_000:].strip() or "no service log output"


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
            env=os.environ | env,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        assert not result.stdout.split(), "random Compose project already exists"


def _resource_ids(project: str, env: dict[str, str]) -> tuple[list[str], list[str]]:
    containers = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        env=os.environ | env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout.split()
    volumes: list[str] = []
    for logical in ("termflow-data", "termflow-totp-key", "opencode-data"):
        physical = f"{project}-{logical.removeprefix('termflow-')}"
        result = subprocess.run(
            ["docker", "volume", "inspect", "--format", "{{.Name}}", physical],
            env=os.environ | env,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        if result.returncode == 0 and result.stdout.strip() == physical:
            volumes.append(physical)
    return containers, volumes


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
        env=os.environ | env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return result.stdout.split()


def _assert_owned(project: str, resources: list[str], kind: str, env: dict[str, str]) -> None:
    for resource in resources:
        if kind == "container":
            command = [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}',
                resource,
            ]
        elif kind == "volume":
            command = [
                "docker",
                "volume",
                "inspect",
                "--format",
                '{{index .Labels "com.docker.compose.project"}}',
                resource,
            ]
        else:
            command = [
                "docker",
                "network",
                "inspect",
                "--format",
                '{{index .Labels "com.docker.compose.project"}}',
                resource,
            ]
        result = subprocess.run(
            command,
            env=os.environ | env,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        assert result.stdout.strip() == project


@pytest.fixture(scope="module")
def disposable_egress_project(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[EgressProject]:
    if os.environ.get("TERMFLOW_E2E_PROVIDER_EGRESS") != "1":
        pytest.skip("set TERMFLOW_E2E_PROVIDER_EGRESS=1 for disposable egress checks")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    try:
        daemon = subprocess.run(
            ["docker", "info"], text=True, capture_output=True, check=False, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker daemon is unavailable")
    if daemon.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    project = f"termflow-egress-audit-{uuid.uuid4().hex[:12]}"
    env = {
        "COMPOSE_PROJECT_NAME": project,
        "TERMFLOW_ADMIN_TOKEN": _random_secret(),
        "OPENCODE_SERVER_USERNAME": "termflow-e2e",
        "OPENCODE_SERVER_PASSWORD": _random_secret(),
        "OPENCODE_AGENT_MCP_TOKEN": _random_secret(),
        "DEEPSEEK_API_KEY": _random_secret(),
        "TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN": _random_secret(),
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN": "https://api.deepseek.com",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS": "deepseek-v4-flash",
        # This test verifies transport isolation, not a provider disclosure.
        # Keep B's ProviderCatalog fail-closed instead of inventing a
        # no-training policy for a disposable network fixture.
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_REGION": "fixture-only",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS": "fixture-only",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_VERSION": "fixture-only",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING": "false",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_POLICY_VERSION": "fixture-only",
        "TERMFLOW_HOST_PORT": str(_free_port()),
    }
    override = tmp_path_factory.mktemp("agent-egress") / "compose.egress-volumes.yaml"
    override.write_text(
        "volumes:\n"
        f"  termflow-data:\n    name: {project}-data\n"
        f"  termflow-totp-key:\n    name: {project}-totp-key\n"
        f"  opencode-data:\n    name: {project}-opencode-data\n",
        encoding="utf-8",
    )
    compose = [*_compose_args(project), "-f", str(override)]
    _assert_project_unused(project, env)
    subprocess.run(
        [*compose, "config", "--quiet"],
        cwd=ROOT,
        env=os.environ | env,
        text=True,
        capture_output=True,
        check=True,
        timeout=180,
    )
    try:
        started = subprocess.run(
            [
                *compose,
                "up",
                "-d",
                "--build",
                "--wait",
                "--wait-timeout",
                "90",
                "control-plane",
                "opencode-init",
                "opencode-agent",
                "provider-egress-proxy",
            ],
            cwd=ROOT,
            env=os.environ | env,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
        if started.returncode != 0:
            logs = "; ".join(
                f"{service} log: {_safe_service_log(compose, service, env)}"
                for service in (
                    "control-plane",
                    "opencode-init",
                    "opencode-agent",
                    "provider-egress-proxy",
                )
            )
            pytest.fail(
                "disposable egress services did not become ready: "
                f"{_safe_project_state(project, env)}; {logs}"
            )
        containers, volumes = _resource_ids(project, env)
        networks = _network_ids(project, env)
        expected_volumes = {
            f"{project}-data",
            f"{project}-totp-key",
            f"{project}-opencode-data",
        }
        assert set(volumes) == expected_volumes
        assert len(containers) == 4
        assert len(networks) == 4
        _assert_owned(project, containers, "container", env)
        _assert_owned(project, volumes, "volume", env)
        _assert_owned(project, networks, "network", env)
        inspector = subprocess.run(
            [
                str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
                "--mode",
                "live",
                project,
            ],
            cwd=ROOT,
            env=os.environ | env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert inspector.returncode == 0, inspector.stdout + inspector.stderr
        yield EgressProject(
            project,
            env,
            tuple(volumes),
            tuple(containers),
            tuple(networks),
        )
    finally:
        # Never issue a project teardown until every discovered resource has
        # been proven to carry this exact project label.
        containers, volumes = _resource_ids(project, env)
        networks = _network_ids(project, env)
        if containers or volumes or networks:
            _assert_owned(project, containers, "container", env)
            _assert_owned(project, volumes, "volume", env)
            _assert_owned(project, networks, "network", env)
            subprocess.run(
                [*compose, "down", "--volumes", "--remove-orphans"],
                cwd=ROOT,
                env=os.environ | env,
                text=True,
                capture_output=True,
                check=False,
                timeout=180,
            )


def _service_id(project: EgressProject, service: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project.name}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        env=os.environ | project.env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    ids = result.stdout.split()
    assert len(ids) == 1
    return ids[0]


def _exec_probe(
    project: EgressProject, service: str, command: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", _service_id(project, service), "sh", "-ec", command],
        env=os.environ | project.env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def _safe_probe_output(project: EgressProject, result: subprocess.CompletedProcess[str]) -> str:
    output = f"{result.stdout}\n{result.stderr}"
    for value in sorted(project.env.values(), key=len, reverse=True):
        if value:
            output = output.replace(value, "<redacted>")
    return output[-2_000:].strip() or "no probe output"


def test_allowed_provider_host_connects_through_proxy(
    disposable_egress_project: EgressProject,
) -> None:
    result = _exec_probe(
        disposable_egress_project,
        "opencode-agent",
        'test "$http_proxy" = "http://provider-egress-proxy:3128"; '
        "output=\"$(printf 'CONNECT api.deepseek.com:443 HTTP/1.1\\r\\n"
        "Host: api.deepseek.com:443\\r\\n\\r\\n' | "
        'nc -w 8 provider-egress-proxy 3128 2>&1 || true)"; '
        'printf "%s\n" "$output"; '
        'printf "%s" "$output" | grep -Eq "^HTTP/[0-9.]+ 200 "',
    )
    assert result.returncode == 0, _safe_probe_output(disposable_egress_project, result)


def test_other_domain_raw_ip_and_other_port_are_denied(
    disposable_egress_project: EgressProject,
) -> None:
    for authority in (
        "example.com:443",
        "1.1.1.1:443",
        "api.deepseek.com:8443",
    ):
        result = _exec_probe(
            disposable_egress_project,
            "opencode-agent",
            f"output=\"$(printf 'CONNECT {authority} HTTP/1.1\\r\\n"
            f"Host: {authority}\\r\\n\\r\\n' | "
            "nc -w 8 provider-egress-proxy 3128 2>&1 || true)\"; "
            'printf "%s" "$output" | grep -Eq "^HTTP/[0-9.]+ 403 "',
        )
        assert result.returncode == 0, _safe_probe_output(disposable_egress_project, result)


def test_opencode_direct_internet_path_is_absent(disposable_egress_project: EgressProject) -> None:
    runtime = _service_id(disposable_egress_project, "opencode-agent")
    networks = subprocess.run(
        ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", runtime],
        env=os.environ | disposable_egress_project.env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    assert "provider_uplink" not in networks
    assert "default" not in networks


def test_proxy_cannot_reach_control_plane_agent_network(
    disposable_egress_project: EgressProject,
) -> None:
    proxy = _service_id(disposable_egress_project, "provider-egress-proxy")
    networks = subprocess.run(
        ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", proxy],
        env=os.environ | disposable_egress_project.env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    assert "agent_internal" not in networks
    result = _exec_probe(
        disposable_egress_project,
        "provider-egress-proxy",
        "! bash -c 'exec 3<>/dev/tcp/control-plane/8000' >/dev/null 2>&1",
    )
    assert result.returncode == 0, _safe_probe_output(disposable_egress_project, result)
