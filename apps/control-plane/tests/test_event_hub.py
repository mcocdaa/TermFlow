from uuid import uuid4

import pytest
from termflow_control_plane.connections.event_hub import EventHub
from termflow_protocol import MessageType, WireMessage


def _event(instance_id):
    return WireMessage(
        type=MessageType.TOPOLOGY_CHANGED,
        instance_id=instance_id,
        payload={"topology": {}},
    )


@pytest.mark.asyncio
async def test_slow_subscriber_is_removed_without_blocking_publish() -> None:
    hub = EventHub(queue_size=1)
    subscriber = await hub.subscribe(instance_id=None)
    await hub.publish(_event(uuid4()))
    dropped = await hub.publish(_event(uuid4()))
    assert dropped == [subscriber.id]
    assert subscriber.closed.is_set()
    assert await hub.unsubscribe(subscriber) is False


@pytest.mark.asyncio
async def test_publish_honors_instance_filter() -> None:
    hub = EventHub(queue_size=2)
    first_id = uuid4()
    first = await hub.subscribe(instance_id=first_id)
    second = await hub.subscribe(instance_id=uuid4())
    event = _event(first_id)
    assert await hub.publish(event) == []
    assert await first.queue.get() == event
    assert second.queue.empty()


@pytest.mark.asyncio
async def test_epoch_synchronization_closes_current_and_rejects_stale_subscription() -> None:
    hub = EventHub(queue_size=2)
    current = await hub.subscribe(instance_id=None, auth_epoch=1)

    assert await hub.synchronize_epoch(2) == 1
    assert current.closed.is_set()
    assert current.close_code == 4401
    stale = await hub.subscribe(instance_id=None, auth_epoch=1)
    assert stale.closed.is_set()
    assert stale.close_code == 4401
    replacement = await hub.subscribe(instance_id=None, auth_epoch=2)
    assert not replacement.closed.is_set()
