from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from termflow_control_plane.connections.registry import LiveInstanceRegistry
from termflow_control_plane.connections.terminal_hub import TerminalHub
from termflow_control_plane.routing.terminal_router import (
    TerminalRouteError,
    TerminalRouter,
)
from termflow_protocol import (
    MessageType,
    TerminalActionFrame,
    TerminalActionResultPayload,
    TerminalOpenedPayload,
    WireMessage,
)


@dataclass
class FakeTerminalAudit:
    records: list[dict[str, object]] = field(default_factory=list)
    fail_flush: bool = False

    def record_nowait(
        self,
        operation: str,
        instance_id: UUID | None,
        pane_id: str | None,
        input_bytes: int | None,
        result: str,
        error_code: str | None,
    ) -> None:
        self.records.append(
            {
                "operation": operation,
                "instance_id": instance_id,
                "pane_id": pane_id,
                "input_bytes": input_bytes,
                "result": result,
                "error_code": error_code,
            }
        )

    async def flush(self) -> None:
        if self.fail_flush:
            raise RuntimeError("audit persistence failed")
        return None


async def _subject(*, queue_size: int = 4):
    registry = LiveInstanceRegistry(queue_size=queue_size)
    connection = await registry.register(uuid4())
    connection.capabilities = frozenset({"full_terminal"})
    connection.hello_ready.set()
    hub = TerminalHub(queue_max_messages=8, queue_max_bytes=1024)
    audit = FakeTerminalAudit()
    router = TerminalRouter(registry=registry, hub=hub, audit=audit)
    return router, connection, audit


@pytest.mark.asyncio
async def test_open_rejects_bridge_without_full_terminal_capability() -> None:
    router, connection, audit = await _subject()
    connection.capabilities = frozenset({"topology"})

    with pytest.raises(TerminalRouteError, match="capability_unavailable"):
        await router.open(connection.instance_id, session_key=None)

    assert connection.outbound.empty()
    assert len(audit.records) == 1
    assert audit.records[0]["result"] == "rejected"
    assert audit.records[0]["error_code"] == "capability_unavailable"


@pytest.mark.asyncio
async def test_open_and_action_audit_only_the_confirmed_final_outcome() -> None:
    router, connection, audit = await _subject()
    terminal = await router.open(connection.instance_id, session_key=None)
    open_request = await connection.outbound.get()
    assert audit.records == []

    opened = WireMessage(
        type=MessageType.TERMINAL_OPENED,
        instance_id=connection.instance_id,
        payload=TerminalOpenedPayload(
            terminal_id=terminal.terminal_id,
            stream_id=uuid4(),
            rows=24,
            cols=80,
        ).model_dump(mode="json"),
    )
    assert await router.forward_from_bridge(opened)
    assert [row["result"] for row in audit.records] == ["ok"]
    assert open_request.type is MessageType.TERMINAL_OPEN

    action_id = uuid4()
    await router.action(
        terminal,
        TerminalActionFrame(action_id=action_id, action="new_window"),
    )
    await connection.outbound.get()
    assert len(audit.records) == 1

    failed = WireMessage(
        type=MessageType.TERMINAL_ACTION_RESULT,
        instance_id=connection.instance_id,
        payload=TerminalActionResultPayload(
            terminal_id=terminal.terminal_id,
            action_id=action_id,
            ok=False,
            error_code="target_not_found",
        ).model_dump(mode="json"),
    )
    assert await router.forward_from_bridge(failed)
    assert [row["result"] for row in audit.records] == ["ok", "failed"]
    assert audit.records[-1]["error_code"] == "target_not_found"


@pytest.mark.asyncio
async def test_rejected_open_and_action_each_write_one_audit_row() -> None:
    router, connection, audit = await _subject(queue_size=1)
    connection.outbound.put_nowait(
        WireMessage(
            type=MessageType.BRIDGE_HELLO,
            instance_id=connection.instance_id,
            payload={},
        )
    )
    with pytest.raises(TerminalRouteError, match="backpressure"):
        await router.open(connection.instance_id, session_key=None)
    assert [row["result"] for row in audit.records] == ["rejected"]

    await connection.outbound.get()
    terminal = await router.open(connection.instance_id, session_key=None)
    with pytest.raises(TerminalRouteError, match="backpressure"):
        await router.action(
            terminal,
            TerminalActionFrame(action_id=uuid4(), action="new_window"),
        )
    assert [row["result"] for row in audit.records] == ["rejected", "rejected"]


@pytest.mark.asyncio
async def test_abandon_synchronously_preserves_aggregate_and_unknown_outcomes() -> None:
    router, connection, audit = await _subject()
    terminal = await router.open(connection.instance_id, session_key=None)
    await connection.outbound.get()
    await router.input(terminal, b"secret bytes")
    await connection.outbound.get()
    await router.action(
        terminal,
        TerminalActionFrame(action_id=uuid4(), action="new_window"),
    )
    await connection.outbound.get()

    router.abandon(terminal)

    by_operation = {row["operation"]: row for row in audit.records}
    assert by_operation["terminal.open"]["result"] == "unknown"
    assert by_operation["terminal.action"]["result"] == "unknown"
    assert by_operation["terminal.input"]["input_bytes"] == len(b"secret bytes")
    assert by_operation["terminal.close"]["result"] == "ok"


@pytest.mark.asyncio
async def test_abandon_still_sends_teardown_when_close_audit_flush_fails() -> None:
    router, connection, audit = await _subject()
    terminal = await router.open(connection.instance_id, session_key=None)
    await connection.outbound.get()
    audit.fail_flush = True

    with pytest.raises(RuntimeError, match="audit persistence failed"):
        await router.request_close(terminal, "client_closed")
    assert connection.outbound.empty()

    router.abandon(terminal)

    assert (await connection.outbound.get()).type is MessageType.TERMINAL_CLOSE
    assert not connection.replaced.is_set()
