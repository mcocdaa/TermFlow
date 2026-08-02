import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from termflow_node import cli
from termflow_node.instances.activation import ActivationError, ActivationResult
from termflow_node.instances.manager import AmbiguousInstance, InstanceManager
from termflow_node.instances.models import InstanceLifecycle, LocalInstance, RemoteAccessState
from termflow_node.instances.store import InstanceStore
from typer.testing import CliRunner


def _record(root: Path, name: str) -> LocalInstance:
    instance_id = uuid4()
    return LocalInstance(
        instance_id=instance_id,
        name=name,
        socket_path=root / str(instance_id) / "tmux.sock",
        created_at=datetime.now(UTC),
        bridge_pid=123,
        instance_token="must-not-print",
        lifecycle=InstanceLifecycle.RUNNING,
    )


def test_list_shows_independent_instance_health(tmp_path, monkeypatch) -> None:
    store = InstanceStore(tmp_path / "instances")
    first = _record(store.root, "alpha")
    second = _record(store.root, "beta")
    store.save(first)
    store.save(second)
    monkeypatch.setattr(InstanceStore, "default", classmethod(lambda cls: store))

    statuses = {
        first.instance_id: (True, True),
        second.instance_id: (True, False),
    }
    monkeypatch.setattr(
        cli,
        "probe_instance_health",
        lambda record: statuses[record.instance_id],
    )
    result = CliRunner().invoke(cli.app, ["list"])
    assert result.exit_code == 0, result.output
    assert (
        f"{first.instance_id} alpha running bridge-running remote_access=active"
        in result.stdout
    )
    assert (
        f"{second.instance_id} beta running bridge-down remote_access=active"
        in result.stdout
    )
    assert "connected" not in result.stdout
    assert "must-not-print" not in result.stdout


def test_list_json_has_no_credentials(tmp_path, monkeypatch) -> None:
    store = InstanceStore(tmp_path / "instances")
    record = _record(store.root, "alpha")
    store.save(record)
    monkeypatch.setattr(InstanceStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(cli, "probe_instance_health", lambda record: (True, True))
    result = CliRunner().invoke(cli.app, ["list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["instance_id"] == str(record.instance_id)
    assert payload[0]["remote_access"] == "active"
    assert "token" not in result.stdout.lower()


def test_list_marks_activation_required_without_claiming_connection(
    tmp_path, monkeypatch
) -> None:
    from termflow_node.instances.models import RemoteAccessState

    store = InstanceStore(tmp_path / "instances")
    record = _record(store.root, "alpha").model_copy(
        update={"remote_access": RemoteAccessState.ACTIVATION_REQUIRED}
    )
    store.save(record)
    monkeypatch.setattr(InstanceStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(cli, "probe_instance_health", lambda record: (True, False))

    result = CliRunner().invoke(cli.app, ["list"])

    assert result.exit_code == 0, result.output
    assert "activation-required remote_access=activation_required" in result.stdout
    assert "connected" not in result.stdout


def test_ambiguous_name_reports_candidate_ids(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    first = _record(store.root, "same")
    second = _record(store.root, "same")
    store.save(first)
    store.save(second)
    manager = InstanceManager(store, bridge_launcher=lambda instance: 123)
    try:
        manager.resolve("same")
    except AmbiguousInstance as error:
        assert str(first.instance_id) in str(error)
        assert str(second.instance_id) in str(error)
    else:
        raise AssertionError("expected ambiguous Instance name")


class FakeActivator:
    def __init__(self, result: ActivationResult | ActivationError) -> None:
        self.result = result
        self.identifiers: list[str] = []

    async def activate(self, identifier: str) -> ActivationResult:
        self.identifiers.append(identifier)
        if isinstance(self.result, ActivationError):
            raise self.result
        return self.result


def test_activate_command_reports_success_without_credentials(tmp_path, monkeypatch) -> None:
    record = _record(tmp_path, "alpha").model_copy(
        update={"remote_access": RemoteAccessState.ACTIVE}
    )
    activator = FakeActivator(ActivationResult(record, True))
    monkeypatch.setattr(
        cli, "default_instance_activator", lambda store: activator, raising=False
    )

    result = CliRunner().invoke(cli.app, ["activate", "alpha"])

    assert result.exit_code == 0, result.output
    assert activator.identifiers == ["alpha"]
    assert f"Activated {record.instance_id}" in result.stdout
    assert "token" not in result.stdout.lower()


def test_activate_command_reports_active_noop(tmp_path, monkeypatch) -> None:
    record = _record(tmp_path, "alpha").model_copy(
        update={"remote_access": RemoteAccessState.ACTIVE}
    )
    activator = FakeActivator(ActivationResult(record, False))
    monkeypatch.setattr(
        cli, "default_instance_activator", lambda store: activator, raising=False
    )

    result = CliRunner().invoke(cli.app, ["activate", str(record.instance_id)])

    assert result.exit_code == 0, result.output
    assert f"Remote access already active for {record.instance_id}" in result.stdout


def test_activate_command_reports_safe_failure_with_nonzero_exit(
    tmp_path, monkeypatch
) -> None:
    activator = FakeActivator(
        ActivationError("Remote activation failed; local tmux was not changed.")
    )
    monkeypatch.setattr(
        cli, "default_instance_activator", lambda store: activator, raising=False
    )

    result = CliRunner().invoke(cli.app, ["activate", "alpha"])

    assert result.exit_code == 1
    assert "Remote activation failed" in result.stderr
