from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigNotFound
from termflow_node.instances.activation import (
    ActivationError,
    InstanceActivator,
)
from termflow_node.instances.manager import (
    AmbiguousInstance,
    BridgeStartError,
    InstanceResolutionError,
)
from termflow_node.instances.models import (
    InstanceLifecycle,
    LocalInstance,
    RemoteAccessState,
)
from termflow_node.instances.store import InstanceStore

FRESH_SECRET = "fresh-private-instance-token"


def _installation() -> InstallationConfig:
    return InstallationConfig(
        server_url="https://termflow.example.com",
        installation_id=uuid4(),
        installation_token="installation-private-token",
    )


def _required_record(root: Path, *, name: str = "alpha", bridge_pid: int | None = None):
    instance_id = uuid4()
    return LocalInstance(
        schema_version=3,
        instance_id=instance_id,
        name=name,
        session_id="$3",
        session_name=name,
        socket_path=root / str(instance_id) / "tmux.sock",
        created_at=datetime.now(UTC),
        bridge_pid=bridge_pid,
        lifecycle=InstanceLifecycle.RUNNING,
        remote_access=RemoteAccessState.ACTIVATION_REQUIRED,
    )


class FakeConfigStore:
    def __init__(self, config: InstallationConfig | Exception) -> None:
        self.config = config
        self.loads = 0

    def load(self) -> InstallationConfig:
        self.loads += 1
        if isinstance(self.config, Exception):
            raise self.config
        return self.config


class FakeManager:
    def __init__(
        self,
        record: LocalInstance,
        store: InstanceStore,
        *,
        resolve_error: Exception | None = None,
        tmux_error: Exception | None = None,
        stop_error: Exception | None = None,
        start_error: Exception | None = None,
    ) -> None:
        self.record = record
        self.store = store
        self.resolve_error = resolve_error
        self.tmux_error = tmux_error
        self.stop_error = stop_error
        self.start_error = start_error
        self.calls: list[tuple[str, object]] = []

    def resolve(self, identifier: str) -> LocalInstance:
        self.calls.append(("resolve", identifier))
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.record

    def require_running_tmux(self, record: LocalInstance) -> None:
        self.calls.append(("require_running_tmux", record.instance_id))
        if self.tmux_error is not None:
            raise self.tmux_error

    def stop_bridge(self, record: LocalInstance) -> LocalInstance:
        self.calls.append(("stop_bridge", record.bridge_pid))
        if self.stop_error is not None:
            raise self.stop_error
        stopped = record.model_copy(update={"bridge_pid": None})
        self.store.save(stopped)
        return stopped

    def start_bridge(self, record: LocalInstance) -> LocalInstance:
        self.calls.append(("start_bridge", record.instance_id))
        if self.start_error is not None:
            raise self.start_error
        started = record.model_copy(update={"bridge_pid": 9876})
        self.store.save(started)
        return started


class FakeRegistrationClient:
    def __init__(
        self,
        store: InstanceStore,
        *,
        error: Exception | None = None,
    ) -> None:
        self.store = store
        self.error = error
        self.registered_ids: list[UUID] = []

    async def register_instance(
        self,
        installation: InstallationConfig,
        instance: LocalInstance,
        store: InstanceStore,
    ) -> LocalInstance:
        assert installation.installation_token.get_secret_value() == (
            "installation-private-token"
        )
        assert store is self.store
        self.registered_ids.append(instance.instance_id)
        if self.error is not None:
            raise self.error
        registered = instance.model_copy(
            update={"instance_token": SecretStr(FRESH_SECRET)}
        )
        store.save(registered)
        return registered


def _activator(
    store: InstanceStore,
    record: LocalInstance,
    *,
    config: InstallationConfig | Exception | None = None,
    manager: FakeManager | None = None,
    client: FakeRegistrationClient | None = None,
) -> tuple[InstanceActivator, FakeConfigStore, FakeManager, FakeRegistrationClient]:
    config_store = FakeConfigStore(config or _installation())
    active_manager = manager or FakeManager(record, store)
    active_client = client or FakeRegistrationClient(store)
    return (
        InstanceActivator(
            config_store=config_store,
            instance_store=store,
            manager=active_manager,
            control_plane=active_client,
        ),
        config_store,
        active_manager,
        active_client,
    )


@pytest.mark.asyncio
async def test_activate_registers_same_uuid_and_starts_fresh_bridge(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    required = _required_record(store.root, bridge_pid=4321)
    store.save(required)
    activator, _, manager, client = _activator(store, required)

    result = await activator.activate(str(required.instance_id))

    assert result.activated is True
    assert result.instance.instance_id == required.instance_id
    assert result.instance.remote_access is RemoteAccessState.ACTIVE
    assert result.instance.bridge_pid == 9876
    assert result.instance.instance_token is not None
    assert client.registered_ids == [required.instance_id]
    assert ("stop_bridge", 4321) in manager.calls
    saved = store.load(required.instance_id)
    assert saved.instance_id == required.instance_id
    assert saved.remote_access is RemoteAccessState.ACTIVE
    assert saved.bridge_pid == 9876


@pytest.mark.asyncio
async def test_activate_is_idempotent_when_remote_access_is_already_active(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    active = _required_record(store.root).model_copy(
        update={
            "remote_access": RemoteAccessState.ACTIVE,
            "instance_token": SecretStr("existing-token"),
        }
    )
    store.save(active)
    activator, config, manager, client = _activator(store, active)

    result = await activator.activate("alpha")

    assert result.activated is False
    assert result.instance == active
    assert config.loads == 0
    assert manager.calls == [("resolve", "alpha")]
    assert client.registered_ids == []


@pytest.mark.asyncio
async def test_activate_preserves_safe_ambiguous_instance_error(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    required = _required_record(store.root)
    first, second = uuid4(), uuid4()
    manager = FakeManager(
        required,
        store,
        resolve_error=AmbiguousInstance(
            f"Instance name 'alpha' is ambiguous; candidates: {first}, {second}"
        ),
    )
    activator, _, _, _ = _activator(store, required, manager=manager)

    with pytest.raises(ActivationError) as caught:
        await activator.activate("alpha")

    assert str(first) in str(caught.value)
    assert str(second) in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "tmux_error"),
    [
        (ConfigNotFound("missing"), None),
        (_installation(), InstanceResolutionError("tmux server is not running")),
    ],
)
async def test_activate_prerequisite_failure_retains_activation_required(
    tmp_path,
    config: InstallationConfig | Exception,
    tmux_error: Exception | None,
) -> None:
    store = InstanceStore(tmp_path / "instances")
    required = _required_record(store.root)
    store.save(required)
    manager = FakeManager(required, store, tmux_error=tmux_error)
    activator, _, _, client = _activator(
        store, required, config=config, manager=manager
    )

    with pytest.raises(ActivationError, match="local tmux was not changed"):
        await activator.activate(str(required.instance_id))

    saved = store.load(required.instance_id)
    assert saved.remote_access is RemoteAccessState.ACTIVATION_REQUIRED
    assert saved.instance_token is None
    assert client.registered_ids == []


@pytest.mark.asyncio
async def test_registration_failure_keeps_required_state_without_secret(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    required = _required_record(store.root, bridge_pid=4321)
    store.save(required)
    client = FakeRegistrationClient(
        store,
        error=RuntimeError("server included installation-private-token"),
    )
    activator, _, _, _ = _activator(store, required, client=client)

    with pytest.raises(ActivationError) as caught:
        await activator.activate(str(required.instance_id))

    assert "installation-private-token" not in str(caught.value)
    saved = store.load(required.instance_id)
    assert saved.remote_access is RemoteAccessState.ACTIVATION_REQUIRED
    assert saved.instance_token is None
    assert saved.bridge_pid is None


@pytest.mark.asyncio
async def test_bridge_launch_failure_clears_fresh_token_and_rolls_back(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    required = _required_record(store.root)
    store.save(required)
    manager = FakeManager(
        required,
        store,
        start_error=BridgeStartError(f"could not use {FRESH_SECRET}"),
    )
    activator, _, _, _ = _activator(store, required, manager=manager)

    with pytest.raises(ActivationError) as caught:
        await activator.activate(str(required.instance_id))

    assert str(caught.value) == "Bridge failed to start after registration."
    assert FRESH_SECRET not in str(caught.value)
    saved = store.load(required.instance_id)
    assert saved.remote_access is RemoteAccessState.ACTIVATION_REQUIRED
    assert saved.instance_token is None
    assert saved.bridge_pid is None
