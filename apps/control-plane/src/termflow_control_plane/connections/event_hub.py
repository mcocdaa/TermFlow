"""Bounded, history-free fan-out for ephemeral terminal events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from termflow_protocol import WireMessage


@dataclass(eq=False, slots=True)
class EventSubscriber:
    instance_id: UUID | None
    queue: asyncio.Queue[WireMessage]
    id: UUID = field(default_factory=uuid4)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class EventHub:
    def __init__(self, *, queue_size: int) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[UUID, EventSubscriber] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, instance_id: UUID | None) -> EventSubscriber:
        subscriber = EventSubscriber(
            instance_id=instance_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        async with self._lock:
            self._subscribers[subscriber.id] = subscriber
        return subscriber

    async def unsubscribe(self, subscriber: EventSubscriber) -> bool:
        async with self._lock:
            removed = self._subscribers.pop(subscriber.id, None)
        subscriber.closed.set()
        return removed is not None

    async def publish(self, message: WireMessage) -> list[UUID]:
        dropped: list[UUID] = []
        async with self._lock:
            for subscriber_id, subscriber in tuple(self._subscribers.items()):
                if (
                    subscriber.instance_id is not None
                    and subscriber.instance_id != message.instance_id
                ):
                    continue
                try:
                    subscriber.queue.put_nowait(message)
                except asyncio.QueueFull:
                    self._subscribers.pop(subscriber_id, None)
                    subscriber.closed.set()
                    dropped.append(subscriber_id)
        return dropped
