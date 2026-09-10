"""Fake-Docker behavioral tests for the label-discovered security inspector."""

# Long lines inside the embedded fake-Docker shell program are fixture data;
# reflowing them would change command matching and JSON output.
# ruff: noqa: E501

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET = "fixture-secret-that-must-not-appear"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "docker-argv.log"
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$TERMFLOW_TEST_DOCKER_LOG"
args="$*"
AGENT_ID=$(printf 'a%.0s' {1..64})
INIT_ID=$(printf 'i%.0s' {1..64})
PROXY_ID=$(printf 'p%.0s' {1..64})
CONTROL_ID=$(printf 'd%.0s' {1..64})
service=""
case "$args" in
  *com.docker.compose.service=opencode-agent*) service=opencode-agent ;;
  *com.docker.compose.service=opencode-init*) service=opencode-init ;;
  *com.docker.compose.service=provider-egress-proxy*) service=provider-egress-proxy ;;
  *com.docker.compose.service=control-plane*) service=control-plane ;;
esac
if [[ "$args" == *"network inspect"* ]]; then
  network="${!#}"
  case "$network" in
    audit_agent_internal) printf 'true|{"com.docker.compose.project":"audit","com.docker.compose.network":"agent_internal"}|{"%s":{},"%s":{}}\\n' "$AGENT_ID" "$CONTROL_ID" ;;
    audit_default)
      [[ "${TERMFLOW_FAKE_MODE:-ok}" == default-internal ]] && default_internal=true || default_internal=false
      printf '%s|{"com.docker.compose.project":"audit","com.docker.compose.network":"default"}|{"%s":{}}\\n' "$default_internal" "$CONTROL_ID" ;;
    audit_provider_egress)
      if [[ "${TERMFLOW_FAKE_EXTRA_MEMBER:-0}" == 1 ]]; then
        printf 'true|{"com.docker.compose.project":"audit","com.docker.compose.network":"provider_egress"}|{"%s":{},"%s":{},"%s":{}}\\n' "$AGENT_ID" "$PROXY_ID" "$(printf 'x%.0s' {1..64})"
      else
        printf 'true|{"com.docker.compose.project":"audit","com.docker.compose.network":"provider_egress"}|{"%s":{},"%s":{}}\\n' "$AGENT_ID" "$PROXY_ID"
      fi ;;
    audit_provider_uplink) printf 'false|{"com.docker.compose.project":"audit","com.docker.compose.network":"provider_uplink"}|{"%s":{}}\\n' "$PROXY_ID" ;;
  esac
  exit 0
fi
if [[ "$args" == *" ps "* || "$args" == ps* ]]; then
  if [[ "${TERMFLOW_FAKE_MODE:-ok}" == missing && "$service" == opencode-agent ]]; then exit 0; fi
  if [[ "${TERMFLOW_FAKE_MODE:-ok}" == duplicate && "$service" == opencode-agent ]]; then printf 'agent-a\\nagent-b\\n'; exit 0; fi
  case "$service" in opencode-agent) printf '%s\\n' "$AGENT_ID";; opencode-init) printf '%s\\n' "$INIT_ID";; provider-egress-proxy) printf '%s\\n' "$PROXY_ID";; control-plane) printf '%s\\n' "$CONTROL_ID";; esac
  exit 0
fi
id="${!#}"
if [[ "$args" == *"Config.Labels"* ]]; then
  case "$id" in
    "$AGENT_ID") echo '{"com.docker.compose.project":"audit","com.docker.compose.service":"opencode-agent"}' ;;
    "$INIT_ID") echo '{"com.docker.compose.project":"audit","com.docker.compose.service":"opencode-init"}' ;;
    "$PROXY_ID") echo '{"com.docker.compose.project":"audit","com.docker.compose.service":"provider-egress-proxy"}' ;;
  esac
elif [[ "$args" == *"Config.Image"* ]]; then
  [[ "$id" == "$PROXY_ID" ]] && echo 'ubuntu/squid:6@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' || echo 'ghcr.io/anomalyco/opencode:1@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
elif [[ "$args" == *"Config.User"* ]]; then
  [[ "$id" == "$PROXY_ID" ]] && echo '13:13' || { [[ "$id" == "$INIT_ID" ]] && echo '0:0' || echo '405:100'; }
elif [[ "$args" == *"HostConfig.Privileged"* ]]; then echo false
elif [[ "$args" == *"State.Running"* ]]; then [[ "$id" == "$INIT_ID" ]] && echo false || echo true
elif [[ "$args" == *"State.ExitCode"* ]]; then echo 0
elif [[ "$args" == *"HostConfig.NetworkMode"* ]]; then [[ "$id" == "$INIT_ID" ]] && echo none || echo default
elif [[ "$args" == *"HostConfig.ReadonlyRootfs"* ]]; then echo true
elif [[ "$args" == *"HostConfig.CapDrop"* ]]; then echo '["ALL"]'
elif [[ "$args" == *"HostConfig.CapAdd"* ]]; then
  if [[ "$id" == "$INIT_ID" && "${TERMFLOW_FAKE_INIT_EXTRA_CAP:-0}" == 1 ]]; then echo '["CAP_CHOWN","CAP_NET_ADMIN"]'
  elif [[ "$id" == "$INIT_ID" && "${TERMFLOW_FAKE_CAP_PREFIX:-0}" == 1 ]]; then echo '["CAP_CHOWN"]'
  elif [[ "$id" == "$INIT_ID" ]]; then echo '["CHOWN"]'
  elif [[ "${TERMFLOW_FAKE_NULL_CAP_ADD:-0}" == 1 ]]; then echo null
  elif [[ "${TERMFLOW_FAKE_EXTRA_CAP_ADD:-0}" == 1 ]]; then echo '["NET_ADMIN"]'
  else echo '[]'
  fi
elif [[ "$args" == *"HostConfig.SecurityOpt"* ]]; then echo '["no-new-privileges:true"]'
elif [[ "$args" == *"HostConfig.Tmpfs"* ]]; then echo '{"/tmp":"x","/home/opencode":"x","/run/squid":"x","/var/log/squid":"x","/var/spool/squid":"x"}'
elif [[ "$args" == *"HostConfig.PidsLimit"* ]]; then [[ "$id" == "$PROXY_ID" ]] && echo 128 || echo 512
elif [[ "$args" == *"HostConfig.Memory"* ]]; then echo 134217728
elif [[ "$args" == *"HostConfig.NanoCpus"* ]]; then echo 500000000
elif [[ "$args" == *"HostConfig.Ulimits"* ]]; then echo '[{"Name":"nofile","Soft":1024,"Hard":1024}]'
elif [[ "$args" == *"Config.Env"* ]]; then
  if [[ "${TERMFLOW_FAKE_B_SECRET:-0}" == 1 && "$id" == "$AGENT_ID" ]]; then echo '["HOME=/home/opencode","TERMFLOW_ADMIN_TOKEN=hidden-fixture"]'; else echo '["HOME=/home/opencode"]'; fi
elif [[ "$args" == *"NetworkSettings.Networks"* ]]; then
  if [[ "$id" == "$AGENT_ID" ]]; then [[ "${TERMFLOW_FAKE_MODE:-ok}" == opencode-uplink ]] && echo '{"audit_agent_internal":{},"audit_provider_egress":{},"audit_provider_uplink":{}}' || echo '{"audit_agent_internal":{},"audit_provider_egress":{}}';
  elif [[ "$id" == "$PROXY_ID" ]]; then [[ "${TERMFLOW_FAKE_MODE:-ok}" == proxy-agent ]] && echo '{"audit_agent_internal":{},"audit_provider_egress":{},"audit_provider_uplink":{}}' || echo '{"audit_provider_egress":{},"audit_provider_uplink":{}}';
  else echo '{}'; fi
elif [[ "$args" == *"NetworkSettings.Ports"* ]]; then echo '{}'
elif [[ "$args" == *"Mounts"* ]]; then
  if [[ "$id" == "$AGENT_ID" ]]; then
    if [[ "${TERMFLOW_FAKE_WRONG_SOURCE:-0}" == 1 ]]; then echo '[{"Type":"volume","Name":"audit-other-data","Source":"/var/lib/docker/volumes/audit-other-data/_data","Destination":"/data","RW":true},{"Type":"bind","Source":"/workspace/other.yaml","Destination":"/etc/termflow/opencode-config.yaml","RW":false}]'; else printf '[{"Type":"volume","Name":"audit-opencode-data","Source":"/var/lib/docker/volumes/audit-opencode-data/_data","Destination":"/data","RW":true},{"Type":"bind","Source":"%s/deploy/opencode-config.yaml","Destination":"/etc/termflow/opencode-config.yaml","RW":false}]\n' "$TERMFLOW_TEST_REPOSITORY_ROOT"; fi
  elif [[ "$id" == "$PROXY_ID" ]]; then printf '[{"Type":"bind","Source":"%s/deploy/provider-egress/squid.conf","Destination":"/etc/squid/squid.conf","RW":false}]\n' "$TERMFLOW_TEST_REPOSITORY_ROOT";
  elif [[ "$id" == "$INIT_ID" ]]; then echo '[{"Type":"volume","Name":"audit-opencode-data","Source":"/var/lib/docker/volumes/audit-opencode-data/_data","Destination":"/data","RW":true}]';
  else echo '[]'; fi
else echo ''
fi
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return docker, log


def _run(
    tmp_path: Path, mode: str = "ok", fake_mode: str = "ok"
) -> subprocess.CompletedProcess[str]:
    docker, log = _fake_docker(tmp_path)
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TERMFLOW_TEST_DOCKER_LOG": str(log),
        "TERMFLOW_TEST_REPOSITORY_ROOT": str(ROOT),
        "TERMFLOW_FAKE_MODE": fake_mode,
        "TERMFLOW_FIXTURE_SECRET": SECRET,
    }
    return subprocess.run(
        [
            str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
            "--mode",
            mode,
            "audit",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_script_discovers_exact_service_by_compose_labels_without_compose(tmp_path: Path) -> None:
    result = _run(tmp_path, "live")
    assert result.returncode == 0, result.stderr
    commands = (tmp_path / "docker-argv.log").read_text()
    assert "ps -aq --no-trunc" in commands
    assert "com.docker.compose.project=audit" in commands
    assert "com.docker.compose.service=opencode-agent" in commands
    assert " compose " not in f" {commands} "


def test_duplicate_or_missing_service_fails_closed(tmp_path: Path) -> None:
    for fake_mode in ("missing", "duplicate"):
        result = _run(tmp_path / fake_mode, "live", fake_mode)
        assert result.returncode != 0
        assert "opencode-agent" in result.stderr


def test_absent_cap_add_accepts_engine_null_but_rejects_real_capability(
    tmp_path: Path,
) -> None:
    for variable, expected in (
        ("TERMFLOW_FAKE_NULL_CAP_ADD", 0),
        ("TERMFLOW_FAKE_EXTRA_CAP_ADD", 1),
    ):
        docker, log = _fake_docker(tmp_path / variable)
        environment = os.environ | {
            "PATH": f"{docker.parent}:{os.environ['PATH']}",
            "TERMFLOW_TEST_DOCKER_LOG": str(log),
            "TERMFLOW_TEST_REPOSITORY_ROOT": str(ROOT),
            variable: "1",
        }
        result = subprocess.run(
            [
                str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
                "--mode",
                "live",
                "audit",
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if expected == 0:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert "cap-add" in result.stderr


def test_init_cap_add_accepts_engine_prefix_but_rejects_a_wider_set(
    tmp_path: Path,
) -> None:
    for variable, expected in (
        ("TERMFLOW_FAKE_CAP_PREFIX", 0),
        ("TERMFLOW_FAKE_INIT_EXTRA_CAP", 1),
    ):
        docker, log = _fake_docker(tmp_path / variable)
        environment = os.environ | {
            "PATH": f"{docker.parent}:{os.environ['PATH']}",
            "TERMFLOW_TEST_DOCKER_LOG": str(log),
            "TERMFLOW_TEST_REPOSITORY_ROOT": str(ROOT),
            variable: "1",
        }
        result = subprocess.run(
            [
                str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
                "--mode",
                "live",
                "audit",
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if expected == 0:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert "chown" in result.stderr or "cap-add" in result.stderr


def test_live_topology_rejects_opencode_on_uplink(tmp_path: Path) -> None:
    result = _run(tmp_path, "live", "opencode-uplink")
    assert result.returncode != 0
    assert "opencode-agent" in result.stderr


def test_live_topology_rejects_proxy_on_agent_internal(tmp_path: Path) -> None:
    result = _run(tmp_path, "live", "proxy-agent")
    assert result.returncode != 0
    assert "provider-egress-proxy" in result.stderr


def test_live_topology_rejects_internal_control_plane_default_network(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "live", "default-internal")
    assert result.returncode != 0
    assert "audit_default" in result.stderr


def test_output_never_contains_fixture_secrets_or_full_inspect_json(tmp_path: Path) -> None:
    result = _run(tmp_path, "live")
    output = result.stdout + result.stderr
    assert SECRET not in output
    assert '"NetworkSettings"' not in output
    assert '"HostConfig"' not in output


def test_live_network_contract_fails_closed_on_extra_member_or_wrong_internal(
    tmp_path: Path,
) -> None:
    # The fake intentionally returns only the exact membership contract above;
    # changing either member set or `Internal` must make the shell verifier fail.
    _docker, log = _fake_docker(tmp_path)
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TERMFLOW_TEST_DOCKER_LOG": str(log),
        "TERMFLOW_TEST_REPOSITORY_ROOT": str(ROOT),
        "TERMFLOW_FAKE_EXTRA_MEMBER": "1",
    }
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
            "--mode",
            "live",
            "audit",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "provider_egress" in result.stderr


def test_inspector_rejects_privileged_runtime_and_proxy_socket_mount(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)
    source = docker.read_text()
    docker.write_text(
        source.replace(
            'elif [[ "$args" == *"HostConfig.Privileged"* ]]; then echo false',
            'elif [[ "$args" == *"HostConfig.Privileged"* ]]; then echo true',
        )
    )
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TERMFLOW_TEST_DOCKER_LOG": str(log),
        "TERMFLOW_TEST_REPOSITORY_ROOT": str(ROOT),
    }
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
            "--mode",
            "live",
            "audit",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "privileged" in result.stderr


def test_inspector_rejects_unconfined_security_profile(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)
    source = docker.read_text()
    docker.write_text(
        source.replace(
            "echo '[\"no-new-privileges:true\"]'",
            'echo \'["no-new-privileges:true","seccomp=unconfined"]\'',
        )
    )
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TERMFLOW_TEST_DOCKER_LOG": str(log),
        "TERMFLOW_TEST_REPOSITORY_ROOT": str(ROOT),
    }
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
            "--mode",
            "live",
            "audit",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "security profile" in result.stderr


def test_inspector_rejects_wrong_exact_mount_source(tmp_path: Path) -> None:
    _docker, log = _fake_docker(tmp_path)
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TERMFLOW_TEST_DOCKER_LOG": str(log),
        "TERMFLOW_TEST_REPOSITORY_ROOT": str(ROOT),
        "TERMFLOW_FAKE_WRONG_SOURCE": "1",
    }
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
            "--mode",
            "live",
            "audit",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "mounts" in result.stderr


def test_inspector_rejects_b_secret_in_runtime_environment(tmp_path: Path) -> None:
    _docker, log = _fake_docker(tmp_path)
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TERMFLOW_TEST_DOCKER_LOG": str(log),
        "TERMFLOW_TEST_REPOSITORY_ROOT": str(ROOT),
        "TERMFLOW_FAKE_B_SECRET": "1",
    }
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "security" / "verify-agent-containers.sh"),
            "--mode",
            "live",
            "audit",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "secret env" in result.stderr
