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
    close_code: int = 4410
    close_reason: str = "Subscriber too slow"


class EventHub:
    def __init__(self, *, queue_size: int) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[UUID, EventSubscriber] = {}
        self._lock = asyncio.Lock()
        self._auth_epoch = 1

    async def subscribe(
        self,
        instance_id: UUID | None,
        *,
        auth_epoch: int = 1,
    ) -> EventSubscriber:
        subscriber = EventSubscriber(
            instance_id=instance_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        async with self._lock:
            if auth_epoch == self._auth_epoch:
                self._subscribers[subscriber.id] = subscriber
            else:
                subscriber.close_code = 4401
                subscriber.close_reason = "Authentication epoch changed"
                subscriber.closed.set()
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

    async def synchronize_epoch(self, epoch: int) -> int:
        """Atomically reject stale subscriptions and close current subscribers."""

        if epoch < 1:
            raise ValueError("authentication epoch must be positive")
        async with self._lock:
            if epoch == self._auth_epoch:
                return 0
            self._auth_epoch = epoch
            subscribers = tuple(self._subscribers.values())
            self._subscribers.clear()
            for subscriber in subscribers:
                subscriber.close_code = 4401
                subscriber.close_reason = "Authentication epoch changed"
                subscriber.closed.set()
        return len(subscribers)
