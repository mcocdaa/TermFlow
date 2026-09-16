"""Behavioral tests for the non-mutating deployment preflight."""

# The fake Docker JSON is kept as one literal shell response so quoting is the
# same on every test platform.
# ruff: noqa: E501

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {
    "TERMFLOW_ADMIN_TOKEN": "admin-7E8mF3qR4xW9cN2vK6pL5sT1yU0zA",
    "OPENCODE_SERVER_USERNAME": "termflow-e2e-user",
    "OPENCODE_SERVER_PASSWORD": "runtime-4pL8mQ2xR7wN5vC9zK1sT6yU",
    "DEEPSEEK_API_KEY": "provider-9zT3mW7qR2vK8xN5cL1sU6yP",
    "TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN": "cleanup-6nR1xV8qM3tK9wC5zL2sU7yP",
    "OPENCODE_AGENT_MCP_TOKEN": "mcp-2qW8nR5xT1vK7mC9zL3sU6yP",
    "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN": "https://api.deepseek.com",
    "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS": "deepseek-v4-flash",
    "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_REGION": "global",
    "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS": "account policy",
    "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_VERSION": "policy-2026-09",
    "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING": "true",
    "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE": "DEEPSEEK_API_KEY",
    "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_POLICY_VERSION": "policy-2026-09",
}


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    sandbox = tmp_path / "repository"
    (sandbox / "scripts" / "deploy").mkdir(parents=True)
    shutil.copy2(
        ROOT / "scripts" / "deploy" / "agent-local-preflight.sh", sandbox / "scripts" / "deploy"
    )
    shutil.copytree(ROOT / "deploy", sandbox / "deploy")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TERMFLOW_TEST_DOCKER_LOG"\n'
        'if [[ "$*" == *"--format json"* ]]; then echo \'{"name":"termflow-v020-local","services":{"control-plane":{"environment":{"TERMFLOW_AGENT_OPENCODE_MCP_TOKEN":"shared-mcp","TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE":"DEEPSEEK_API_KEY"}},"opencode-agent":{"networks":{"agent_internal":null,"provider_uplink":null},"environment":{"TERMFLOW_AGENT_MCP_TOKEN":"shared-mcp"}}},"networks":{"default":{},"agent_internal":{"internal":true},"provider_uplink":{}},"volumes":{"a":{"name":"termflow-v020-local-opencode-data"},"b":{"name":"termflow-v020-local-data"},"c":{"name":"termflow-v020-local-totp-key"}}}\'; fi\n'
    )
    docker.chmod(0o755)
    env_file = tmp_path / "deployment.env"
    env_file.write_text("\n".join(f"{name}={value}" for name, value in REQUIRED.items()) + "\n")
    env_file.chmod(0o600)
    return sandbox, fake_bin, env_file


def _run(
    script_root: Path, fake_bin: Path, env_file: Path, log: Path
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TERMFLOW_TEST_DOCKER_LOG": str(log),
    }
    return subprocess.run(
        [
            str(script_root / "scripts" / "deploy" / "agent-local-preflight.sh"),
            "--env-file",
            str(env_file),
        ],
        cwd=script_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_preflight_rejects_env_mode_other_than_0600_without_printing_values(tmp_path: Path) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    env_file.chmod(0o644)
    result = _run(sandbox, fake_bin, env_file, tmp_path / "docker-argv.log")
    assert result.returncode != 0
    assert "0600" in result.stderr
    sensitive_values = [
        REQUIRED[name]
        for name in (
            "TERMFLOW_ADMIN_TOKEN",
            "OPENCODE_SERVER_PASSWORD",
            "DEEPSEEK_API_KEY",
            "OPENCODE_AGENT_MCP_TOKEN",
            "TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN",
        )
    ]
    assert all(value not in result.stdout + result.stderr for value in sensitive_values)


def test_preflight_rejects_config_directory_or_missing_regular_file(tmp_path: Path) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    config = sandbox / "deploy" / "opencode-config.yaml"
    config.unlink()
    config.mkdir()
    result = _run(sandbox, fake_bin, env_file, tmp_path / "docker-argv.log")
    assert result.returncode != 0
    assert "opencode-config.yaml must be a regular file" in result.stderr


def test_preflight_lists_only_missing_variable_names(tmp_path: Path) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    env_file.write_text(f"TERMFLOW_ADMIN_TOKEN={REQUIRED['TERMFLOW_ADMIN_TOKEN']}\n")
    env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    result = _run(sandbox, fake_bin, env_file, tmp_path / "docker-argv.log")
    assert result.returncode != 0
    assert "OPENCODE_SERVER_USERNAME" in result.stderr
    assert "DEEPSEEK_API_KEY" in result.stderr
    sensitive_values = [
        REQUIRED[name]
        for name in (
            "TERMFLOW_ADMIN_TOKEN",
            "OPENCODE_SERVER_PASSWORD",
            "DEEPSEEK_API_KEY",
            "OPENCODE_AGENT_MCP_TOKEN",
            "TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN",
        )
    ]
    assert all(value not in result.stdout + result.stderr for value in sensitive_values)


def test_preflight_never_calls_up_down_rm_or_volume_remove(tmp_path: Path) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    log = tmp_path / "docker-argv.log"
    result = _run(sandbox, fake_bin, env_file, log)
    assert result.returncode == 0, result.stderr
    commands = log.read_text().splitlines()
    assert commands
    forbidden = (" up", " down", " restart", " rm", " volume rm", " system prune")
    assert not any(fragment in f" {command}" for command in commands for fragment in forbidden)


def test_preflight_rejects_duplicate_and_empty_quoted_dotenv_values(tmp_path: Path) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    env_file.write_text('TERMFLOW_ADMIN_TOKEN=""\nTERMFLOW_ADMIN_TOKEN=second-admin-value\n')
    env_file.chmod(0o600)
    result = _run(sandbox, fake_bin, env_file, tmp_path / "docker-argv.log")
    assert result.returncode != 0
    assert "second-admin-value" not in result.stdout + result.stderr
    assert (
        "TERMFLOW_ADMIN_TOKEN" in result.stdout + result.stderr
        or "dotenv parse failed" in result.stderr
    )


def test_preflight_rejects_provider_policy_missing_or_false_without_values(tmp_path: Path) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    env_file.write_text(
        "\n".join(
            f"{name}={value}"
            for name, value in REQUIRED.items()
            if name != "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING"
        )
        + "\nTERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING=false\n"
    )
    env_file.chmod(0o600)
    result = _run(sandbox, fake_bin, env_file, tmp_path / "docker-argv.log")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING" in output
    sensitive_values = [
        REQUIRED[name]
        for name in (
            "TERMFLOW_ADMIN_TOKEN",
            "OPENCODE_SERVER_PASSWORD",
            "DEEPSEEK_API_KEY",
            "OPENCODE_AGENT_MCP_TOKEN",
            "TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN",
        )
    ]
    assert all(value not in output for value in sensitive_values)


def test_preflight_rejects_placeholder_and_reused_sensitive_values(tmp_path: Path) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    values = dict(REQUIRED)
    values["OPENCODE_SERVER_PASSWORD"] = values["TERMFLOW_ADMIN_TOKEN"]
    values["DEEPSEEK_API_KEY"] = "replace-with-deployment-secret"
    env_file.write_text("\n".join(f"{name}={value}" for name, value in values.items()) + "\n")
    env_file.chmod(0o600)
    result = _run(sandbox, fake_bin, env_file, tmp_path / "docker-argv.log")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "DEEPSEEK_API_KEY" in output
    assert "reused" in output or "distinct" in output
    assert "replace-with-deployment-secret" not in output


def test_preflight_rejects_placeholder_provider_policy_without_printing_it(
    tmp_path: Path,
) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    values = dict(REQUIRED)
    marker = "operator-supplied-retention"
    values["TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS"] = marker
    env_file.write_text("\n".join(f"{name}={value}" for name, value in values.items()) + "\n")
    env_file.chmod(0o600)
    result = _run(sandbox, fake_bin, env_file, tmp_path / "docker-argv.log")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS" in output
    assert marker not in output


def test_preflight_requires_mcp_token_and_matches_rendered_network_boundaries(
    tmp_path: Path,
) -> None:
    sandbox, fake_bin, env_file = _sandbox(tmp_path)
    values = dict(REQUIRED)
    values.pop("OPENCODE_AGENT_MCP_TOKEN")
    env_file.write_text("\n".join(f"{name}={value}" for name, value in values.items()) + "\n")
    env_file.chmod(0o600)
    result = _run(sandbox, fake_bin, env_file, tmp_path / "docker-argv.log")
    assert result.returncode != 0
    assert "OPENCODE_AGENT_MCP_TOKEN" in result.stdout + result.stderr
