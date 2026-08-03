from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
import termflow_node.instances.manager as manager_module
from termflow_node.instances.manager import InstanceManager
from termflow_node.instances.models import InstanceLifecycle, LocalInstance, RemoteAccessState
from termflow_node.instances.store import InstanceStore
from termflow_node.tmux.runner import TmuxSessionIdentity


class FakeRunner:
    def __init__(self, socket_path: Path, *, session_name: str) -> None:
        self.socket_path = socket_path
        self.identity = TmuxSessionIdentity("$3", session_name)
        self.calls: list[tuple[str, ...]] = []

    def session_identity(self, target: str | None = None) -> TmuxSessionIdentity:
        self.calls.append(("session_identity", *((target,) if target else ())))
        return self.identity

    def rename_session(self, target: str, name: str) -> None:
        self.calls.append(("rename_session", target, name))
        self.identity = TmuxSessionIdentity(self.identity.session_id, name)

    def is_alive(self, target: str) -> bool:
        self.calls.append(("is_alive", target))
        return True

    def attach_argv(self, target: str) -> list[str]:
        self.calls.append(("attach_argv", target))
        return ["tmux", "attach-session", "-t", target]

    def kill_session(self, target: str) -> None:
        self.calls.append(("kill_session", target))


def _legacy(store: InstanceStore, name: str) -> LocalInstance:
    instance_id = uuid4()
    record = LocalInstance(
        instance_id=instance_id,
        name=name,
        session_name="main",
        socket_path=(store.instance_dir(instance_id) / "tmux.sock").absolute(),
        created_at=datetime.now(UTC),
        bridge_pid=123,
        lifecycle=InstanceLifecycle.RUNNING,
    )
    store.save(record)
    return record


def test_frozen_node_launches_bridge_from_the_same_executable(tmp_path, monkeypatch) -> None:
    store = InstanceStore(tmp_path / "instances")
    record = _legacy(store, "frozen")
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(manager_module.subprocess, "Popen", popen)
    monkeypatch.setattr(manager_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(manager_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(manager_module.sys, "executable", "/opt/termflow/termflow")

    manager_module.launch_bridge(record, log_path=tmp_path / "bridge.log")

    assert popen.call_args.args[0] == [
        "/opt/termflow/termflow",
        "_bridge",
        "--instance-id",
        str(record.instance_id),
    ]


@pytest.mark.parametrize(
    ("actual_name", "expected_name", "renamed"),
    [
        ("main", "legacy-display", True),
        ("locally-renamed", "locally-renamed", False),
    ],
)
def test_legacy_identity_migration_preserves_the_authoritative_tmux_name(
    tmp_path,
    actual_name: str,
    expected_name: str,
    renamed: bool,
) -> None:
    store = InstanceStore(tmp_path / "instances")
    legacy = _legacy(store, "legacy-display")
    fake = FakeRunner(legacy.socket_path, session_name=actual_name)
    manager = InstanceManager(
        store,
        bridge_launcher=lambda instance: 123,
        runner_factory=lambda path: fake,
    )

    migrated, argv = manager.attach(str(legacy.instance_id))

    assert migrated.schema_version == 3
    assert migrated.session_id == "$3"
    assert migrated.name == expected_name
    assert migrated.session_name == expected_name
    assert argv[-1] == "$3"
    assert (("rename_session", "$3", "legacy-display") in fake.calls) is renamed
    assert store.load(legacy.instance_id) == migrated


def test_already_migrated_record_refreshes_local_tmux_rename_by_stable_id(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    record = LocalInstance(
        schema_version=2,
        instance_id=instance_id,
        name="before",
        session_id="$3",
        session_name="before",
        socket_path=(store.instance_dir(instance_id) / "tmux.sock").absolute(),
        created_at=datetime.now(UTC),
        bridge_pid=123,
        lifecycle=InstanceLifecycle.RUNNING,
    )
    store.save(record)
    fake = FakeRunner(record.socket_path, session_name="after-local-rename")
    manager = InstanceManager(
        store,
        bridge_launcher=lambda instance: 123,
        runner_factory=lambda path: fake,
    )

    refreshed, argv = manager.attach(str(instance_id))

    assert refreshed.name == "after-local-rename"
    assert refreshed.session_name == "after-local-rename"
    assert ("session_identity", "$3") in fake.calls
    assert argv[-1] == "$3"


def test_list_migrates_legacy_identity_before_display(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    legacy = _legacy(store, "legacy-display")
    fake = FakeRunner(legacy.socket_path, session_name="local-list-name")
    manager = InstanceManager(
        store,
        bridge_launcher=lambda instance: 123,
        runner_factory=lambda path: fake,
    )

    listing = manager.list_current()

    assert listing.instances[0].session_id == "$3"
    assert listing.instances[0].name == "local-list-name"
    assert listing.instances[0].schema_version == 3


def test_kill_targets_stable_session_id_not_display_name(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    record = LocalInstance(
        schema_version=2,
        instance_id=instance_id,
        name="display name",
        session_id="$3",
        session_name="display name",
        socket_path=(store.instance_dir(instance_id) / "tmux.sock").absolute(),
        created_at=datetime.now(UTC),
        lifecycle=InstanceLifecycle.RUNNING,
    )
    store.save(record)
    fake = FakeRunner(record.socket_path, session_name="display name")
    manager = InstanceManager(store, runner_factory=lambda path: fake)

    stopped = manager.kill(instance_id)

    assert stopped.lifecycle is InstanceLifecycle.STOPPED
    assert ("kill_session", "$3") in fake.calls


def test_attach_keeps_tmux_but_does_not_launch_required_bridge(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    record = LocalInstance(
        schema_version=3,
        instance_id=instance_id,
        name="local-only",
        session_id="$3",
        session_name="local-only",
        socket_path=(store.instance_dir(instance_id) / "tmux.sock").absolute(),
        created_at=datetime.now(UTC),
        bridge_pid=None,
        instance_token=None,
        lifecycle=InstanceLifecycle.RUNNING,
        remote_access=RemoteAccessState.ACTIVATION_REQUIRED,
    )
    store.save(record)
    fake = FakeRunner(record.socket_path, session_name=record.name)
    launcher = Mock(return_value=999)
    manager = InstanceManager(
        store,
        bridge_launcher=launcher,
        runner_factory=lambda path: fake,
    )

    attached, argv = manager.attach(str(record.instance_id))

    assert argv[-1] == record.session_id
    assert attached.remote_access is RemoteAccessState.ACTIVATION_REQUIRED
    launcher.assert_not_called()


def test_stop_bridge_terminates_matching_process_and_clears_pid(
    tmp_path, monkeypatch
) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    record = LocalInstance(
        schema_version=3,
        instance_id=instance_id,
        name="local-only",
        session_id="$3",
        session_name="local-only",
        socket_path=(store.instance_dir(instance_id) / "tmux.sock").absolute(),
        created_at=datetime.now(UTC),
        bridge_pid=4321,
        lifecycle=InstanceLifecycle.RUNNING,
        remote_access=RemoteAccessState.ACTIVATION_REQUIRED,
    )
    store.save(record)
    manager = InstanceManager(store)
    checks = iter([True, False, False])
    monkeypatch.setattr(manager, "_is_expected_bridge", lambda pid, instance_id: next(checks))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "termflow_node.instances.manager.os.kill",
        lambda pid, signal_number: killed.append((pid, signal_number)),
    )

    stopped = manager.stop_bridge(record)

    assert stopped.bridge_pid is None
    assert store.load(instance_id).bridge_pid is None
    assert killed == [(4321, 15)]


def test_require_running_tmux_rejects_dead_local_session(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    record = LocalInstance(
        schema_version=3,
        instance_id=instance_id,
        name="stopped",
        session_id="$3",
        session_name="stopped",
        socket_path=(store.instance_dir(instance_id) / "tmux.sock").absolute(),
        created_at=datetime.now(UTC),
        lifecycle=InstanceLifecycle.RUNNING,
        remote_access=RemoteAccessState.ACTIVATION_REQUIRED,
    )
    fake = FakeRunner(record.socket_path, session_name=record.name)
    fake.is_alive = lambda target: False  # type: ignore[method-assign]
    manager = InstanceManager(store, runner_factory=lambda path: fake)

    with pytest.raises(LookupError, match="tmux server is not running"):
        manager.require_running_tmux(record)
