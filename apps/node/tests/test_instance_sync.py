from datetime import UTC, datetime
from uuid import uuid4

from termflow_node.config.models import InstallationConfig
from termflow_node.instances.models import (
    InstanceLifecycle,
    LocalInstance,
    RemoteInstanceStatus,
)
from termflow_node.instances.store import InstanceStore
from termflow_node.instances.synchronization import InstanceSynchronizer, SyncResult
from termflow_protocol import InstanceListResponse, InstanceResponse


def _record(store: InstanceStore, name: str) -> LocalInstance:
    instance_id = uuid4()
    return LocalInstance(
        instance_id=instance_id,
        name=name,
        socket_path=store.instance_dir(instance_id) / "tmux.sock",
        created_at=datetime.now(UTC),
        bridge_pid=None,
        instance_token="instance-token-for-test",
        lifecycle=InstanceLifecycle.STOPPED,
    )


class FakeControlPlaneClient:
    def __init__(self, remote_instances: InstanceListResponse) -> None:
        self.remote_instances = remote_instances

    async def list_owned_instances(
        self,
        installation: InstallationConfig,
    ) -> InstanceListResponse:
        return self.remote_instances


async def test_sync_marks_local_records_missing_from_b(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    local = _record(store, "deleted-remotely")
    store.save(local)
    client = FakeControlPlaneClient(InstanceListResponse(instances=[]))

    result = await InstanceSynchronizer(
        store,
        client,
        InstallationConfig(
            server_url="https://relay.example.com",
            installation_id=uuid4(),
            installation_token="installation-token-for-test",
        ),
    ).sync()

    assert result.remote_deleted == [local.instance_id]
    saved = store.load(local.instance_id)
    assert saved.remote_status is RemoteInstanceStatus.REMOTE_DELETED
    assert saved.last_sync_error is None
    assert saved.last_synced_at is not None


async def test_sync_records_remote_presence_and_online_status(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    local = _record(store, "available-remotely")
    store.save(local)
    client = FakeControlPlaneClient(
        InstanceListResponse(
            instances=[
                InstanceResponse(
                    instance_id=local.instance_id,
                    name=local.name,
                    installation_id=uuid4(),
                    created_at=datetime.now(UTC),
                    online=True,
                )
            ]
        )
    )

    await InstanceSynchronizer(
        store,
        client,
        InstallationConfig(
            server_url="https://relay.example.com",
            installation_id=uuid4(),
            installation_token="installation-token-for-test",
        ),
    ).sync()

    assert store.load(local.instance_id).remote_status is RemoteInstanceStatus.ONLINE


async def test_sync_failure_preserves_remote_state_and_records_error(tmp_path) -> None:
    class FailingControlPlaneClient:
        async def list_owned_instances(
            self,
            installation: InstallationConfig,
        ) -> InstanceListResponse:
            raise RuntimeError("relay unavailable")

    store = InstanceStore(tmp_path / "instances")
    local = _record(store, "temporarily-unreachable").model_copy(
        update={"remote_status": RemoteInstanceStatus.OFFLINE}
    )
    store.save(local)

    result = await InstanceSynchronizer(
        store,
        FailingControlPlaneClient(),
        InstallationConfig(
            server_url="https://relay.example.com",
            installation_id=uuid4(),
            installation_token="installation-token-for-test",
        ),
    ).sync()

    assert result.error == "relay unavailable"
    saved = store.load(local.instance_id)
    assert saved.remote_status is RemoteInstanceStatus.OFFLINE
    assert saved.last_sync_error == "relay unavailable"
    assert saved.last_synced_at is not None


def test_prune_only_selects_instances_with_no_local_runtime(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    stopped = _record(store, "stopped").model_copy(
        update={"remote_status": RemoteInstanceStatus.REMOTE_DELETED}
    )
    still_running = _record(store, "still-running").model_copy(
        update={"remote_status": RemoteInstanceStatus.REMOTE_DELETED}
    )
    store.save(stopped)
    store.save(still_running)
    health = {
        stopped.instance_id: (False, False),
        still_running.instance_id: (True, False),
    }

    synchronizer = InstanceSynchronizer(
        store,
        FakeControlPlaneClient(InstanceListResponse(instances=[])),
        InstallationConfig(
            server_url="https://relay.example.com",
            installation_id=uuid4(),
            installation_token="installation-token-for-test",
        ),
        health_probe=lambda record: health[record.instance_id],
    )

    candidates = synchronizer.prune_candidates()

    assert [candidate.instance.instance_id for candidate in candidates] == [stopped.instance_id]
    assert candidates[0].tmux_alive is False
    assert candidates[0].bridge_alive is False


def test_prune_keeps_remote_instances_even_when_their_local_runtime_is_down(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    remote_offline = _record(store, "offline-on-relay").model_copy(
        update={"remote_status": RemoteInstanceStatus.OFFLINE}
    )
    store.save(remote_offline)
    synchronizer = InstanceSynchronizer(
        store,
        FakeControlPlaneClient(InstanceListResponse(instances=[])),
        InstallationConfig(
            server_url="https://relay.example.com",
            installation_id=uuid4(),
            installation_token="installation-token-for-test",
        ),
        health_probe=lambda _record: (False, False),
    )

    assert synchronizer.prune_candidates() == []


def test_remove_candidates_deletes_only_selected_metadata(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    selected = _record(store, "selected").model_copy(
        update={"remote_status": RemoteInstanceStatus.REMOTE_DELETED}
    )
    retained = _record(store, "retained")
    store.save(selected)
    store.save(retained)
    health = {
        selected.instance_id: (False, False),
        retained.instance_id: (True, False),
    }
    synchronizer = InstanceSynchronizer(
        store,
        FakeControlPlaneClient(InstanceListResponse(instances=[])),
        InstallationConfig(
            server_url="https://relay.example.com",
            installation_id=uuid4(),
            installation_token="installation-token-for-test",
        ),
        health_probe=lambda record: health[record.instance_id],
    )

    removed = synchronizer.remove_candidates(synchronizer.prune_candidates())

    assert removed == [selected.instance_id]
    assert not store.instance_dir(selected.instance_id).exists()
    assert store.instance_dir(retained.instance_id).exists()


def test_sync_result_summary_exposes_failure_without_secrets() -> None:
    assert SyncResult(remote_deleted=[], updated=[], error="relay unavailable").summary() == (
        "Sync failed: relay unavailable"
    )
