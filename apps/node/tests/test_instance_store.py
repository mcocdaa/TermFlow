import json
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


def test_v1_metadata_loads_as_an_unmigrated_record(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    directory = store.instance_dir(instance_id)
    directory.mkdir(parents=True, mode=0o700)
    path = store.metadata_path(instance_id)
    path.write_text(
        json.dumps(
            {
                "instance_id": str(instance_id),
                "name": "legacy-display",
                "session_name": "main",
                "socket_path": str(directory / "tmux.sock"),
                "created_at": datetime.now(UTC).isoformat(),
                "bridge_pid": None,
                "instance_token": None,
                "lifecycle": "running",
            }
        )
    )
    path.chmod(0o600)

    record = store.load(instance_id)

    assert record.schema_version == 1
    assert record.session_id is None
    assert record.session_name == "main"


def test_v2_metadata_persists_stable_session_identity(tmp_path) -> None:
    store = InstanceStore(tmp_path / "instances")
    instance_id = uuid4()
    record = LocalInstance(
        schema_version=2,
        instance_id=instance_id,
        name="renamed",
        session_id="$7",
        session_name="renamed",
        socket_path=store.instance_dir(instance_id) / "tmux.sock",
        created_at=datetime.now(UTC),
        lifecycle=InstanceLifecycle.RUNNING,
    )
    store.save(record)
    dumped = json.loads(store.metadata_path(instance_id).read_text())
    assert dumped["schema_version"] == 2
    assert dumped["session_id"] == "$7"
    assert store.load(instance_id) == record
