from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from termflow_node.instances.manager import InstanceManager
from termflow_node.instances.models import InstanceLifecycle, LocalInstance
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

    assert migrated.schema_version == 2
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
