from datetime import UTC, datetime
from uuid import uuid4

import pytest
from termflow_node.bridge.input_handler import InputHandler
from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.instances.models import InstanceLifecycle, LocalInstance
from termflow_node.instances.store import InstanceStore
from termflow_protocol import PaneInputPayload, PaneSnapshot, TopologySnapshot, WindowSnapshot

SENTINEL = "SECRET_NODE_BODY_c8c9"
INSTALLATION_SECRET = "installation-private-value"
INSTANCE_SECRET = "instance-private-value"


class MemorySender:
    async def send_text(self, pane_id: str, text: str, submit: bool) -> None:
        assert text == SENTINEL


def _topology() -> TopologySnapshot:
    return TopologySnapshot(
        session_id="$0",
        session_name="main",
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="main",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%0",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=80,
                        height=24,
                        active=True,
                        dead=False,
                    )
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_terminal_body_never_reaches_local_state_or_logs(tmp_path, caplog) -> None:
    config_store = ConfigStore(tmp_path / "config" / "config.json")
    config_store.save(
        InstallationConfig(
            server_url="https://termflow.example.com",
            installation_id=uuid4(),
            installation_token=INSTALLATION_SECRET,
        )
    )
    instance_store = InstanceStore(tmp_path / "state" / "instances")
    instance_id = uuid4()
    instance = LocalInstance(
        instance_id=instance_id,
        name="private",
        socket_path=instance_store.instance_dir(instance_id) / "tmux.sock",
        created_at=datetime.now(UTC),
        instance_token=INSTANCE_SECRET,
        lifecycle=InstanceLifecycle.RUNNING,
    )
    instance_store.save(instance)
    log_path = instance_store.instance_dir(instance_id) / "bridge.log"
    log_path.write_text("Bridge started\n")
    log_path.chmod(0o600)

    handler = InputHandler(topology_provider=_topology, sender=MemorySender())
    result = await handler.handle(
        PaneInputPayload(
            command_id=uuid4(),
            idempotency_key=uuid4(),
            pane_id="%0",
            text=SENTINEL,
            submit=True,
        )
    )
    assert result.ok

    files = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert all(SENTINEL.encode() not in body for body in files.values())
    assert SENTINEL not in caplog.text
    assert INSTALLATION_SECRET.encode() in files[config_store.path]
    assert INSTANCE_SECRET.encode() in files[instance_store.metadata_path(instance_id)]
    assert INSTALLATION_SECRET.encode() not in files[log_path]
    assert INSTANCE_SECRET.encode() not in files[log_path]
