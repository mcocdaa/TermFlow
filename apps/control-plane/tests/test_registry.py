from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from termflow_control_plane.connections.registry import (
    ConnectionBackpressure,
    InstanceOffline,
    LiveInstanceRegistry,
)
from termflow_protocol import MessageType, WireMessage


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
async def test_expire_removes_only_stale_connections() -> None:
    registry = LiveInstanceRegistry(queue_size=2)
    stale = await registry.register(uuid4())
    live = await registry.register(uuid4())
    stale.last_heartbeat = datetime.now(UTC) - timedelta(minutes=1)
    expired = await registry.expire_before(datetime.now(UTC) - timedelta(seconds=30))
    assert expired == [stale]
    assert await registry.get(live.instance_id) is live

