import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from termflow_control_plane.app import expire_stale_connections
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.connections.registry import InstanceOffline, LiveInstanceRegistry
from termflow_protocol import MessageType


@pytest.mark.asyncio
async def test_expiry_publishes_offline_and_fails_pending_command() -> None:
    registry = LiveInstanceRegistry(queue_size=2)
    hub = EventHub(queue_size=2)
    instance_id = uuid4()
    connection = await registry.register(instance_id)
    connection.last_heartbeat = datetime.now(UTC) - timedelta(seconds=60)
    command_id = uuid4()
    future = asyncio.get_running_loop().create_future()
    connection.pending[command_id] = future
    subscriber = await hub.subscribe(instance_id=instance_id)

    expired = await expire_stale_connections(
        registry,
        hub,
        now=datetime.now(UTC),
        offline_after_seconds=45,
    )

    assert expired == [connection]
    with pytest.raises(InstanceOffline):
        await future
    event = await subscriber.queue.get()
    assert event.type is MessageType.INSTANCE_OFFLINE
    with pytest.raises(InstanceOffline):
        await registry.get(instance_id)
