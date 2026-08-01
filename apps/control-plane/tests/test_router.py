import asyncio
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.registry import LiveInstanceRegistry
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.routing.router import CommandRouter
from termflow_protocol import (
    CommandResultPayload,
    PaneSnapshot,
    TopologySnapshot,
    WindowSnapshot,
)


@dataclass
class FakeAudit:
    records: list[dict[str, object]] = field(default_factory=list)

    async def record(self, **values):
        self.records.append(values)


@pytest.fixture
async def routing_subject():
    registry = LiveInstanceRegistry(queue_size=2)
    connection = await registry.register(uuid4())
    connection.topology = TopologySnapshot(
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
                        pane_id="%1",
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
    audit = FakeAudit()
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        command_timeout_seconds=0.1,
    )
    return CommandRouter(registry=registry, audit=audit, settings=settings), connection, audit


@pytest.mark.asyncio
async def test_input_waits_for_matching_bridge_confirmation(routing_subject) -> None:
    router, live_connection, audit = routing_subject
    idempotency_key = uuid4()
    task = asyncio.create_task(
        router.send_input(live_connection.instance_id, "%1", "继续", True, idempotency_key)
    )
    message = await live_connection.outbound.get()
    assert message.type == "pane.input"
    assert message.payload["pane_id"] == "%1"
    command_id = UUID(str(message.payload["command_id"]))
    router.resolve_result(
        live_connection,
        CommandResultPayload(
            command_id=command_id,
            idempotency_key=idempotency_key,
            ok=True,
        ),
    )
    assert (await task).ok is True
    assert audit.records == [
        {
            "operation": "pane.input",
            "instance_id": live_connection.instance_id,
            "pane_id": "%1",
            "input_bytes": len("继续".encode()),
            "result": "ok",
            "error_code": None,
        }
    ]


@pytest.mark.asyncio
async def test_unknown_pane_is_rejected_before_enqueue(routing_subject) -> None:
    router, live_connection, _ = routing_subject
    with pytest.raises(TermFlowError) as caught:
        await router.send_input(live_connection.instance_id, "%999", "x", False, uuid4())
    assert caught.value.code == "pane_not_found"
    assert live_connection.outbound.empty()


@pytest.mark.asyncio
async def test_offline_instance_is_rejected_without_queueing(routing_subject) -> None:
    router, _, audit = routing_subject
    with pytest.raises(TermFlowError) as caught:
        await router.send_input(uuid4(), "%1", "never stored", False, uuid4())
    assert caught.value.code == "instance_offline"
    assert audit.records[-1]["input_bytes"] == len(b"never stored")
    assert "text" not in audit.records[-1]
