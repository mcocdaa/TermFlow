from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from termflow_control_plane.connections.registry import (
    ConnectionBackpressure,
    InstanceOffline,
    LiveInstanceRegistry,
)
from termflow_protocol import (
    MessageType,
    TerminalClosePayload,
    TerminalInputPayload,
    WireMessage,
)


@pytest.mark.asyncio
async def test_register_replaces_old_connection_without_cross_unregister() -> None:
    registry = LiveInstanceRegistry(queue_size=2)
    instance_id = uuid4()
    first = await registry.register(instance_id)
    second = await registry.register(instance_id)
    assert first.replaced.is_set()
    assert await registry.get(instance_id) is second
    await registry.unregister(first)
    assert await registry.get(instance_id) is second


@pytest.mark.asyncio
async def test_offline_instance_fails_without_queueing() -> None:
    registry = LiveInstanceRegistry(queue_size=1)
    with pytest.raises(InstanceOffline):
        await registry.enqueue(
            uuid4(),
            WireMessage(type=MessageType.BRIDGE_HEARTBEAT, instance_id=uuid4(), payload={}),
        )


@pytest.mark.asyncio
async def test_full_connection_queue_fails_fast() -> None:
    registry = LiveInstanceRegistry(queue_size=1)
    instance_id = uuid4()
    await registry.register(instance_id)
    message = WireMessage(type=MessageType.BRIDGE_HEARTBEAT, instance_id=instance_id, payload={})
    await registry.enqueue(instance_id, message)
    with pytest.raises(ConnectionBackpressure):
        await registry.enqueue(instance_id, message)


@pytest.mark.asyncio
async def test_terminal_close_uses_reserved_connection_queue_slot() -> None:
    registry = LiveInstanceRegistry(queue_size=1)
    instance_id = uuid4()
    connection = await registry.register(instance_id)
    heartbeat = WireMessage(
        type=MessageType.BRIDGE_HEARTBEAT,
        instance_id=instance_id,
        payload={},
    )
    await registry.enqueue(instance_id, heartbeat)
    close = WireMessage(
        type=MessageType.TERMINAL_CLOSE,
        instance_id=instance_id,
        payload=TerminalClosePayload(
            terminal_id=uuid4(), reason="client_closed"
        ).model_dump(mode="json"),
    )

    assert registry.enqueue_current_nowait(instance_id, close)
    assert (await connection.outbound.get()).type is MessageType.BRIDGE_HEARTBEAT
    assert (await connection.outbound.get()).type is MessageType.TERMINAL_CLOSE


@pytest.mark.asyncio
async def test_terminal_connection_queue_is_also_bounded_by_decoded_bytes() -> None:
    registry = LiveInstanceRegistry(queue_size=4, queue_max_bytes=4)
    instance_id = uuid4()
    await registry.register(instance_id)
    message = WireMessage(
        type=MessageType.TERMINAL_INPUT,
        instance_id=instance_id,
        payload=TerminalInputPayload.from_bytes(instance_id, b"12345").model_dump(
            mode="json"
        ),
    )

    with pytest.raises(ConnectionBackpressure):
        await registry.enqueue(instance_id, message)


@pytest.mark.asyncio
async def test_expire_removes_only_stale_connections() -> None:
    registry = LiveInstanceRegistry(queue_size=2)
    stale = await registry.register(uuid4())
    live = await registry.register(uuid4())
    stale.last_heartbeat = datetime.now(UTC) - timedelta(minutes=1)
    expired = await registry.expire_before(datetime.now(UTC) - timedelta(seconds=30))
    assert expired == [stale]
    assert await registry.get(live.instance_id) is live
