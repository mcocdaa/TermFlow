import stat
from datetime import UTC, datetime
from subprocess import CompletedProcess
from unittest.mock import Mock
from uuid import uuid4

from termflow_node import diagnostics
from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.diagnostics import run_diagnostics
from termflow_node.instances.models import (
    InstanceLifecycle,
    LocalInstance,
    RemoteAccessState,
    RemoteInstanceStatus,
)
from termflow_node.instances.store import InstanceStore
from termflow_node.tmux import runner as runner_module


def test_doctor_is_read_only_by_default_and_can_repair_known_permissions(tmp_path) -> None:
    config_store = ConfigStore(tmp_path / "config" / "config.json")
    config_store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=uuid4(),
            installation_token="secret",
        )
    )
    instance_store = InstanceStore(tmp_path / "state" / "instances")
    config_store.path.chmod(0o640)

    checks = run_diagnostics(config_store, instance_store, repair=False)
    assert any(check.name == "config_permissions" and not check.ok for check in checks)
    assert stat.S_IMODE(config_store.path.stat().st_mode) == 0o640

    repaired = run_diagnostics(config_store, instance_store, repair=True)
    assert any(check.name == "config_permissions" and check.ok for check in repaired)
    assert stat.S_IMODE(config_store.path.stat().st_mode) == 0o600


def test_doctor_repair_does_not_bypass_activation_required(tmp_path, monkeypatch) -> None:
    config_store = ConfigStore(tmp_path / "config" / "config.json")
    config_store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=uuid4(),
            installation_token="secret",
        )
    )
    instance_store = InstanceStore(tmp_path / "state" / "instances")
    instance_id = uuid4()
    record = LocalInstance(
        schema_version=3,
        instance_id=instance_id,
        name="local-only",
        session_id="$3",
        session_name="local-only",
        socket_path=instance_store.instance_dir(instance_id) / "tmux.sock",
        created_at=datetime.now(UTC),
        lifecycle=InstanceLifecycle.RUNNING,
        remote_access=RemoteAccessState.ACTIVATION_REQUIRED,
    )
    instance_store.save(record)
    launch = Mock(return_value=9876)
    monkeypatch.setattr(diagnostics, "probe_instance_health", lambda record: (True, False))
    monkeypatch.setattr(diagnostics, "launch_bridge", launch)

    checks = run_diagnostics(config_store, instance_store, repair=True)

    launch.assert_not_called()
    check = next(check for check in checks if check.name == f"instance:{instance_id}")
    assert not check.ok
    assert "remote_access=activation_required" in check.detail


def test_doctor_tmux_probe_does_not_inherit_frozen_private_library_paths(
    tmp_path, monkeypatch
) -> None:
    config_store = ConfigStore(tmp_path / "config" / "config.json")
    config_store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=uuid4(),
            installation_token="secret",
        )
    )
    instance_store = InstanceStore(tmp_path / "state" / "instances")
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return CompletedProcess(argv, 0, stdout="tmux 3.4\n", stderr="")

    monkeypatch.setattr(runner_module.sys, "frozen", True, raising=False)
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/pyinstaller-private")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/tmp/pyinstaller-private")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    checks = run_diagnostics(config_store, instance_store, repair=False)

    tmux_check = next(check for check in checks if check.name == "tmux")
    assert tmux_check.ok
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["PATH"] == "/usr/local/bin:/usr/bin"
    assert environment["LANG"] == "zh_CN.UTF-8"
    assert "LD_LIBRARY_PATH" not in environment
    assert "LD_LIBRARY_PATH_ORIG" not in environment


def test_doctor_distinguishes_local_runtime_from_remote_connection(tmp_path, monkeypatch) -> None:
    config_store = ConfigStore(tmp_path / "config" / "config.json")
    config_store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=uuid4(),
            installation_token="secret",
        )
    )
    instance_store = InstanceStore(tmp_path / "state" / "instances")
    instance_id = uuid4()
    instance_store.save(
        LocalInstance(
            schema_version=4,
            instance_id=instance_id,
            name="offline-everywhere",
            session_id="$4",
            session_name="offline-everywhere",
            socket_path=instance_store.instance_dir(instance_id) / "tmux.sock",
            created_at=datetime.now(UTC),
            lifecycle=InstanceLifecycle.STOPPED,
            remote_status=RemoteInstanceStatus.OFFLINE,
            last_sync_error="previous sync timed out",
        )
    )
    monkeypatch.setattr(diagnostics, "probe_instance_health", lambda record: (False, False))
    monkeypatch.setattr(
        diagnostics,
        "probe_control_plane_health",
        lambda config: (False, "relay unavailable"),
        raising=False,
    )

    checks = run_diagnostics(
        config_store,
        instance_store,
        repair=False,
        check_control_plane=True,
    )

    relay = next(check for check in checks if check.name == "control_plane")
    instance = next(check for check in checks if check.name == f"instance:{instance_id}")
    assert relay.ok is False
    assert relay.detail == "relay unavailable"
    assert instance.ok is False
    assert "tmux=down bridge=down remote=offline" in instance.detail
    assert "last_sync_error=previous sync timed out" in instance.detail
