from uuid import uuid4

import pytest
from termflow_control_plane.connections.event_hub import EventHub
from termflow_protocol import MessageType, WireMessage


def _event(instance_id=None, *, payload=None):
    return WireMessage(
        type=MessageType.TOPOLOGY_CHANGED,
        instance_id=instance_id if instance_id is not None else uuid4(),
        payload=payload if payload is not None else {"topology": {}},
    )


@pytest.mark.asyncio
async def test_slow_subscriber_is_removed_without_blocking_publish() -> None:
    hub = EventHub(queue_size=1, queue_max_bytes=1024 * 1024)
    subscriber = await hub.subscribe(instance_id=None)
    await hub.publish(_event(uuid4()))
    dropped = await hub.publish(_event(uuid4()))
    assert dropped == [subscriber.id]
    assert subscriber.closed.is_set()
    assert await hub.unsubscribe(subscriber) is False


@pytest.mark.asyncio
async def test_publish_honors_instance_filter() -> None:
    hub = EventHub(queue_size=2, queue_max_bytes=1024 * 1024)
    first_id = uuid4()
    first = await hub.subscribe(instance_id=first_id)
    second = await hub.subscribe(instance_id=uuid4())
    event = _event(first_id)
    assert await hub.publish(event) == []
    assert await first.queue.get() == event
    assert second.queue.empty()


@pytest.mark.asyncio
async def test_epoch_synchronization_closes_current_and_rejects_stale_subscription() -> None:
    hub = EventHub(queue_size=2, queue_max_bytes=1024 * 1024)
    current = await hub.subscribe(instance_id=None, auth_epoch=1)

    assert await hub.synchronize_epoch(2) == 1
    assert current.closed.is_set()
    assert current.close_code == 4401
    stale = await hub.subscribe(instance_id=None, auth_epoch=1)
    assert stale.closed.is_set()
    assert stale.close_code == 4401
    replacement = await hub.subscribe(instance_id=None, auth_epoch=2)
    assert not replacement.closed.is_set()


@pytest.mark.asyncio
async def test_event_hub_drops_subscriber_when_byte_budget_is_exceeded() -> None:
    hub = EventHub(queue_size=10, queue_max_bytes=400)
    subscriber = await hub.subscribe(instance_id=None)
    first = _event(payload={"data": "a" * 160})
    second = _event(payload={"data": "b" * 160})

    assert await hub.publish(first) == []
    assert await hub.publish(second) == [subscriber.id]
    assert subscriber.closed.is_set()
    assert subscriber.close_code == 4410


@pytest.mark.asyncio
async def test_event_hub_reclaims_byte_budget_when_messages_are_consumed() -> None:
    hub = EventHub(queue_size=10, queue_max_bytes=400)
    subscriber = await hub.subscribe(instance_id=None)
    first = _event(payload={"data": "a" * 160})
    second = _event(payload={"data": "b" * 160})

    assert await hub.publish(first) == []
    assert await subscriber.queue.get() == first
    assert await hub.publish(second) == []
    assert not subscriber.closed.is_set()
