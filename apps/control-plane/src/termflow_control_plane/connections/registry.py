"""In-memory registry for independently connected TermFlow Instances."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from termflow_protocol import CommandResultPayload, TopologySnapshot, WireMessage


class InstanceOffline(LookupError):
    pass


class ConnectionBackpressure(RuntimeError):
    pass


@dataclass(eq=False, slots=True)
class LiveConnection:
    instance_id: UUID
    outbound: asyncio.Queue[WireMessage]
    connection_id: UUID = field(default_factory=uuid4)
    topology: TopologySnapshot | None = None
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(UTC))
    pending: dict[UUID, asyncio.Future[CommandResultPayload]] = field(default_factory=dict)
    replaced: asyncio.Event = field(default_factory=asyncio.Event)


class LiveInstanceRegistry:
    def __init__(self, *, queue_size: int) -> None:
        self._queue_size = queue_size
        self._connections: dict[UUID, LiveConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, instance_id: UUID) -> LiveConnection:
        connection = LiveConnection(
            instance_id=instance_id,
            outbound=asyncio.Queue(maxsize=self._queue_size),
        )
        async with self._lock:
            previous = self._connections.get(instance_id)
            self._connections[instance_id] = connection
            if previous is not None:
                previous.replaced.set()
        return connection

    async def unregister(self, connection: LiveConnection) -> bool:
        async with self._lock:
            if self._connections.get(connection.instance_id) is not connection:
                return False
            del self._connections[connection.instance_id]
        self._fail_pending(connection, InstanceOffline(str(connection.instance_id)))
        return True

    async def get(self, instance_id: UUID) -> LiveConnection:
        async with self._lock:
            connection = self._connections.get(instance_id)
        if connection is None:
            raise InstanceOffline(str(instance_id))
        return connection

    async def maybe_get(self, instance_id: UUID) -> LiveConnection | None:
        async with self._lock:
            return self._connections.get(instance_id)

    async def enqueue(self, instance_id: UUID, message: WireMessage) -> LiveConnection:
        connection = await self.get(instance_id)
        try:
            connection.outbound.put_nowait(message)
        except asyncio.QueueFull as exc:
            raise ConnectionBackpressure(str(instance_id)) from exc
        return connection

    async def online_ids(self) -> set[UUID]:
        async with self._lock:
            return set(self._connections)

    async def expire_before(self, cutoff: datetime) -> list[LiveConnection]:
        async with self._lock:
            expired = [
                connection
                for connection in self._connections.values()
                if connection.last_heartbeat < cutoff
            ]
            for connection in expired:
                if self._connections.get(connection.instance_id) is connection:
                    del self._connections[connection.instance_id]
        for connection in expired:
            connection.replaced.set()
            self._fail_pending(connection, InstanceOffline(str(connection.instance_id)))
        return expired

    @staticmethod
    def _fail_pending(connection: LiveConnection, exc: BaseException) -> None:
        for future in tuple(connection.pending.values()):
            if not future.done():
                future.set_exception(exc)
        connection.pending.clear()
