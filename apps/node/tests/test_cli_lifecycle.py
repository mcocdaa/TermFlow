import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from termflow_node import __version__, cli
from termflow_node.instances.activation import ActivationError, ActivationResult
from termflow_node.instances.manager import AmbiguousInstance, InstanceManager
from termflow_node.instances.models import (
    InstanceLifecycle,
    LocalInstance,
    RemoteAccessState,
    RemoteInstanceStatus,
)
from termflow_node.instances.store import InstanceStore
from termflow_node.instances.synchronization import SyncResult
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


def test_version_reports_the_node_package_version() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == __version__


def test_exec_tmux_removes_frozen_library_paths(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_exec(file, argv, env):
        captured.update(file=file, argv=argv, env=env)
        raise SystemExit

    monkeypatch.setattr(cli.os, "execvpe", fake_exec)
    from termflow_node.tmux import runner as runner_module

    monkeypatch.setattr(runner_module.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/pyinstaller-private")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/tmp/pyinstaller-private")

    with pytest.raises(SystemExit):
        cli._exec_tmux(["tmux", "-S", "/tmp/termflow.sock", "attach-session"])

    assert "LD_LIBRARY_PATH" not in captured["env"]
    assert "LD_LIBRARY_PATH_ORIG" not in captured["env"]


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
        f"{first.instance_id} alpha running bridge-running remote=unknown remote_access=active"
        in result.stdout
    )
    assert (
        f"{second.instance_id} beta running bridge-down remote=unknown remote_access=active"
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


def test_list_reports_remote_state_and_last_sync_error(tmp_path, monkeypatch) -> None:
    store = InstanceStore(tmp_path / "instances")
    record = _record(store.root, "removed-remotely").model_copy(
        update={
            "remote_status": RemoteInstanceStatus.REMOTE_DELETED,
            "last_sync_error": "relay unavailable",
        }
    )
    store.save(record)
    monkeypatch.setattr(InstanceStore, "default", classmethod(lambda cls: store))
    monkeypatch.setattr(cli, "probe_instance_health", lambda record: (False, False))

    result = CliRunner().invoke(cli.app, ["list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload[0]["remote_status"] == "remote_deleted"
    assert payload[0]["last_sync_error"] == "relay unavailable"
    assert "remote=remote-deleted" in CliRunner().invoke(cli.app, ["list"]).stdout


def test_sync_command_reports_completed_sync(tmp_path, monkeypatch) -> None:
    class FakeSynchronizer:
        @classmethod
        def from_defaults(cls):
            return cls()

        async def sync(self) -> SyncResult:
            return SyncResult(remote_deleted=[uuid4()], updated=[uuid4()])

    monkeypatch.setattr(cli, "InstanceSynchronizer", FakeSynchronizer, raising=False)

    result = CliRunner().invoke(cli.app, ["sync"])

    assert result.exit_code == 0, result.output
    assert "Synced 1 instances; 1 removed remotely" in result.stdout


def test_prune_dry_run_does_not_remove_metadata(monkeypatch) -> None:
    class FakeSynchronizer:
        removed = False

        @classmethod
        def from_defaults(cls):
            return cls()

        def prune_candidates(self):
            return ["stale-instance"]

        def print_candidates(self, candidates):
            print("stale-instance remote=remote_deleted tmux=down bridge=down")

        def remove_candidates(self, candidates):
            self.removed = True
            return []

    monkeypatch.setattr(cli, "InstanceSynchronizer", FakeSynchronizer, raising=False)

    result = CliRunner().invoke(cli.app, ["prune", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "stale-instance" in result.stdout
    assert FakeSynchronizer.removed is False


def test_prune_requires_confirmation_unless_force(monkeypatch) -> None:
    class FakeSynchronizer:
        removed = False

        @classmethod
        def from_defaults(cls):
            return cls()

        def prune_candidates(self):
            return ["stale-instance"]

        def print_candidates(self, candidates):
            pass

        def remove_candidates(self, candidates):
            type(self).removed = True
            return candidates

    monkeypatch.setattr(cli, "InstanceSynchronizer", FakeSynchronizer, raising=False)

    result = CliRunner().invoke(cli.app, ["prune"], input="n\n")

    assert result.exit_code != 0
    assert FakeSynchronizer.removed is False


def test_prune_force_removes_without_interactive_confirmation(monkeypatch) -> None:
    class FakeSynchronizer:
        removed = False

        @classmethod
        def from_defaults(cls):
            return cls()

        def prune_candidates(self):
            return ["stale-instance"]

        def print_candidates(self, candidates):
            pass

        def remove_candidates(self, candidates):
            type(self).removed = True
            return candidates

    monkeypatch.setattr(cli, "InstanceSynchronizer", FakeSynchronizer, raising=False)

    result = CliRunner().invoke(cli.app, ["prune", "--force"])

    assert result.exit_code == 0, result.output
    assert FakeSynchronizer.removed is True
    assert "Removed 1 stale instances" in result.stdout


def test_list_marks_activation_required_without_claiming_connection(tmp_path, monkeypatch) -> None:
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
    assert "activation-required remote=unknown remote_access=activation_required" in result.stdout
    assert "connected" not in result.stdout


def test_serve_command_runs_the_foreground_supervisor(monkeypatch) -> None:
    captured: list[tuple[str, float]] = []

    def fake_run_serve(name, *, interval=5.0, shutdown=None):
        captured.append((name, interval))

    monkeypatch.setattr(cli, "_run_serve", fake_run_serve)

    result = CliRunner().invoke(cli.app, ["serve", "--name", "demo"])

    assert result.exit_code == 0, result.output
    assert captured == [("demo", 5.0)]


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


def test_resolve_short_id_prefix(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    record = _record(store.root, "alpha")
    store.save(record)
    manager = InstanceManager(store, bridge_launcher=lambda instance: 123)
    resolved = manager.resolve(record.instance_id.hex[:8])
    assert resolved.instance_id == record.instance_id


def test_resolve_ambiguous_id_prefix_reports_candidates(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    first = _record(store.root, "first")
    second = _record(store.root, "second")
    shared_prefix = first.instance_id.hex[:8]
    first = first.model_copy(
        update={"instance_id": UUID(f"{shared_prefix}-0000-0000-0000-000000000001")}
    )
    second = second.model_copy(
        update={"instance_id": UUID(f"{shared_prefix}-0000-0000-0000-000000000002")}
    )
    store.save(first)
    store.save(second)
    manager = InstanceManager(store, bridge_launcher=lambda instance: 123)
    try:
        manager.resolve(shared_prefix)
    except AmbiguousInstance as error:
        assert str(first.instance_id) in str(error)
        assert str(second.instance_id) in str(error)
    else:
        raise AssertionError("expected ambiguous Instance ID prefix")


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
    monkeypatch.setattr(cli, "default_instance_activator", lambda store: activator, raising=False)

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
    monkeypatch.setattr(cli, "default_instance_activator", lambda store: activator, raising=False)

    result = CliRunner().invoke(cli.app, ["activate", str(record.instance_id)])

    assert result.exit_code == 0, result.output
    assert f"Remote access already active for {record.instance_id}" in result.stdout


def test_activate_command_reports_safe_failure_with_nonzero_exit(tmp_path, monkeypatch) -> None:
    activator = FakeActivator(
        ActivationError("Remote activation failed; local tmux was not changed.")
    )
    monkeypatch.setattr(cli, "default_instance_activator", lambda store: activator, raising=False)

    result = CliRunner().invoke(cli.app, ["activate", "alpha"])

    assert result.exit_code == 1
    assert "Remote activation failed" in result.stderr
