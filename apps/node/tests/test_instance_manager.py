"""Unit tests for the idempotent serve lifecycle (ensure/recover/supervisor)."""

import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from termflow_node import cli
from termflow_node.instances.manager import InstanceManager
from termflow_node.instances.models import InstanceLifecycle, LocalInstance
from termflow_node.instances.store import InstanceStore
from termflow_node.tmux.runner import TmuxSessionIdentity


class FakeRunner:
    def __init__(self, socket_path: Path, alive: bool = True) -> None:
        self.socket_path = socket_path
        self.alive = alive
        self.name = "main"
        self.sessions_created = 0
        self.servers_killed = 0
        self.raise_alive = False

    def is_alive(self, target: str) -> bool:
        if self.raise_alive:
            raise RuntimeError("tmux unavailable")
        return self.alive

    def create_session(self, session_name: str, window_name: str) -> None:
        self.sessions_created += 1
        self.name = session_name
        self.alive = True

    def session_identity(self, target: str) -> TmuxSessionIdentity:
        if target and target.startswith("$"):
            return TmuxSessionIdentity(target, self.name)
        return TmuxSessionIdentity("$7", target or "main")

    def kill_server(self) -> None:
        self.servers_killed += 1
        self.alive = False

    def attach_argv(self, session_name: str) -> list[str]:
        return ["tmux", "-S", str(self.socket_path), "attach-session", "-t", session_name]


def _prepare_socket_path(self: InstanceManager, instance_id: UUID) -> Path:
    return Path("/tmp") / f"tf-test-{instance_id.hex}.sock"


@pytest.fixture
def manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[InstanceStore, FakeRunner, InstanceManager]:
    store = InstanceStore(tmp_path / "instances")
    runner = FakeRunner(tmp_path / "unused.sock")
    instance_manager = InstanceManager(
        store,
        bridge_launcher=lambda instance: 123,
        runner_factory=lambda socket_path: runner,
    )
    monkeypatch.setattr(InstanceManager, "_prepare_socket_path", _prepare_socket_path)
    return store, runner, instance_manager


def test_ensure_creates_a_new_instance_when_absent(
    manager: tuple[InstanceStore, FakeRunner, InstanceManager],
) -> None:
    store, runner, instance_manager = manager

    instance = instance_manager.ensure("alpha")

    assert instance.name == "alpha"
    assert instance.lifecycle is InstanceLifecycle.RUNNING
    assert runner.sessions_created == 1
    assert [record.instance_id for record in store.list().instances] == [instance.instance_id]


def test_ensure_reuses_a_live_instance_without_duplicates(
    manager: tuple[InstanceStore, FakeRunner, InstanceManager],
) -> None:
    store, runner, instance_manager = manager

    first = instance_manager.ensure("alpha")
    second = instance_manager.ensure("alpha")

    assert second.instance_id == first.instance_id
    assert runner.sessions_created == 1
    assert len(store.list().instances) == 1


def test_ensure_rebuilds_tmux_and_preserves_identity_after_death(
    manager: tuple[InstanceStore, FakeRunner, InstanceManager],
) -> None:
    store, runner, instance_manager = manager
    created = instance_manager.ensure("alpha")
    socket = created.socket_path
    socket.touch()

    runner.alive = False  # container restart: tmux server is gone
    recovered = instance_manager.ensure("alpha")

    assert recovered.instance_id == created.instance_id
    assert recovered.instance_token == created.instance_token
    assert recovered.lifecycle is InstanceLifecycle.RUNNING
    assert recovered.session_id == "$7"
    assert runner.sessions_created == 2
    assert not socket.exists(), "stale tmux socket must be removed before recreation"
    assert len(store.list().instances) == 1


def test_ensure_rebuilds_when_alive_check_itself_fails(
    manager: tuple[InstanceStore, FakeRunner, InstanceManager],
) -> None:
    store, runner, instance_manager = manager
    created = instance_manager.ensure("alpha")

    runner.raise_alive = True
    recovered = instance_manager.ensure("alpha")

    assert recovered.instance_id == created.instance_id
    assert runner.sessions_created == 2
    assert len(store.list().instances) == 1


class FakeServeManager:
    def __init__(self, event: threading.Event) -> None:
        self.event = event
        self.instance = LocalInstance(
            instance_id=uuid4(),
            name="alpha",
            socket_path=Path("/tmp/tf-serve-unused.sock"),
            created_at=datetime.now(UTC),
            instance_token="secret-token",
            lifecycle=InstanceLifecycle.RUNNING,
        )
        self.ensure_calls = 0
        self.start_bridge_calls = 0
        self.recover_calls = 0
        self.kill_calls = 0
        self.current_count = 0
        self.current_raises = 0

    def ensure(self, name: str) -> LocalInstance:
        self.ensure_calls += 1
        return self.instance

    def start_bridge(self, record: LocalInstance) -> LocalInstance:
        self.start_bridge_calls += 1
        return record

    def current(self, instance_id: UUID) -> LocalInstance:
        self.current_count += 1
        if self.current_count <= self.current_raises:
            raise RuntimeError("tmux dead")
        if self.current_count > 3:
            self.event.set()
        return self.instance

    def recover(self, instance_id: UUID) -> LocalInstance:
        self.recover_calls += 1
        return self.instance

    def kill(self, instance_id: UUID) -> None:
        self.kill_calls += 1


class FakeConfigStore:
    @classmethod
    def default(cls) -> "FakeConfigStore":
        return cls()

    def load(self) -> object:
        return object()


class FakeStore:
    @classmethod
    def default(cls) -> None:
        return None


def _patch_serve_globals(monkeypatch: pytest.MonkeyPatch, manager: FakeServeManager) -> None:
    monkeypatch.setattr(cli, "InstanceManager", lambda store: manager)
    monkeypatch.setattr(cli, "InstanceStore", FakeStore)
    monkeypatch.setattr(cli, "ConfigStore", FakeConfigStore)


def test_serve_stops_immediately_and_cleans_up_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = threading.Event()
    shutdown.set()
    manager = FakeServeManager(shutdown)
    _patch_serve_globals(monkeypatch, manager)

    cli._run_serve("alpha", interval=0.001, shutdown=shutdown)

    assert manager.ensure_calls == 1
    assert manager.start_bridge_calls == 1
    assert manager.kill_calls == 1
    assert manager.recover_calls == 0


def test_serve_restarts_a_dead_bridge_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = threading.Event()
    manager = FakeServeManager(shutdown)
    _patch_serve_globals(monkeypatch, manager)
    probe_calls: list[object] = []

    def probe(record: LocalInstance) -> tuple[bool, bool]:
        probe_calls.append(record)
        return (True, len(probe_calls) <= 2)  # bridge down for the first two probes

    monkeypatch.setattr(cli, "probe_instance_health", probe)

    cli._run_serve("alpha", interval=0.001, shutdown=shutdown)

    assert manager.start_bridge_calls >= 3  # initial launch + two restarts
    assert manager.recover_calls == 0
    assert manager.kill_calls == 1


def test_serve_rebuilds_tmux_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = threading.Event()
    manager = FakeServeManager(shutdown)
    manager.current_raises = 2
    _patch_serve_globals(monkeypatch, manager)

    cli._run_serve("alpha", interval=0.001, shutdown=shutdown)

    assert manager.recover_calls == 2
    assert manager.kill_calls == 1
