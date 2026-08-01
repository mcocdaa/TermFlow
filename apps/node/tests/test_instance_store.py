import stat
from datetime import UTC, datetime
from uuid import uuid4

from termflow_node.instances.models import InstanceLifecycle, LocalInstance
from termflow_node.instances.store import InstanceStore


def test_instance_metadata_is_private_and_round_trips_secret(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    record = LocalInstance(
        instance_id=instance_id,
        name="project-a",
        socket_path=store.instance_dir(instance_id) / "tmux.sock",
        created_at=datetime.now(UTC),
        bridge_pid=123,
        instance_token="instance-secret",
        lifecycle=InstanceLifecycle.RUNNING,
    )
    store.save(record)
    assert store.load(instance_id) == record
    assert stat.S_IMODE(store.instance_dir(instance_id).stat().st_mode) == 0o700
    metadata_path = store.metadata_path(instance_id)
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
    assert b"instance-secret" in metadata_path.read_bytes()
    assert "instance-secret" not in repr(record)


def test_list_reports_malformed_records_without_deleting_them(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    malformed = store.root / str(uuid4())
    malformed.mkdir(parents=True)
    (malformed / "metadata.json").write_text("not json")
    result = store.list()
    assert result.instances == []
    assert result.diagnostics == [malformed / "metadata.json"]
    assert malformed.exists()
