import stat
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

from termflow_node import diagnostics
from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.diagnostics import run_diagnostics
from termflow_node.instances.models import InstanceLifecycle, LocalInstance, RemoteAccessState
from termflow_node.instances.store import InstanceStore


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
