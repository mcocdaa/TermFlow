import stat
from uuid import uuid4

from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.diagnostics import run_diagnostics
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
