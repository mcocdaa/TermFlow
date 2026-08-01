"""In-memory registry for independently connected TermFlow Instances."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from termflow_protocol import (
    CommandResultPayload,
    MessageType,
    TerminalInputPayload,
    TermRenameResultPayload,
    TopologySnapshot,
    WireMessage,
)


class InstanceOffline(LookupError):
    pass


class ConnectionBackpressure(RuntimeError):
    pass


def _queued_terminal_bytes(message: WireMessage) -> int:
    if message.type is not MessageType.TERMINAL_INPUT:
        return 0
    return len(TerminalInputPayload.model_validate(message.payload).to_bytes())


class BoundedWireQueue:
    def __init__(self, *, max_messages: int, max_bytes: int) -> None:
        self._queue: asyncio.Queue[tuple[WireMessage, int]] = asyncio.Queue(
            maxsize=max_messages
        )
        self._max_bytes = max_bytes
        self._queued_bytes = 0

    def put_nowait(self, message: WireMessage) -> None:
        byte_count = _queued_terminal_bytes(message)
        if self._queued_bytes + byte_count > self._max_bytes:
            raise asyncio.QueueFull
        self._queue.put_nowait((message, byte_count))
        self._queued_bytes += byte_count

    async def get(self) -> WireMessage:
        message, byte_count = await self._queue.get()
        self._queued_bytes -= byte_count
        return message

    def empty(self) -> bool:
        return self._queue.empty()


@dataclass(eq=False, slots=True)
class LiveConnection:
    instance_id: UUID
    outbound: BoundedWireQueue
    connection_id: UUID = field(default_factory=uuid4)
    topology: TopologySnapshot | None = None
    topology_ready: asyncio.Event = field(default_factory=asyncio.Event)
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(UTC))
    pending: dict[UUID, asyncio.Future[CommandResultPayload]] = field(default_factory=dict)
    pending_renames: dict[UUID, asyncio.Future[TermRenameResultPayload]] = field(
        default_factory=dict
    )
    replaced: asyncio.Event = field(default_factory=asyncio.Event)


class LiveInstanceRegistry:
    def __init__(self, *, queue_size: int, queue_max_bytes: int = 1024 * 1024) -> None:
        self._queue_size = queue_size
        self._queue_max_bytes = queue_max_bytes
        self._connections: dict[UUID, LiveConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, instance_id: UUID) -> LiveConnection:
        connection = LiveConnection(
            instance_id=instance_id,
            outbound=BoundedWireQueue(
                max_messages=self._queue_size,
                max_bytes=self._queue_max_bytes,
            ),
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

    def enqueue_current_nowait(self, instance_id: UUID, message: WireMessage) -> bool:
        """Best-effort cancellation cleanup; normal routing uses :meth:`enqueue`."""

        connection = self._connections.get(instance_id)
        if connection is None:
            return False
        try:
            connection.outbound.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True

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
        for rename_future in tuple(connection.pending_renames.values()):
            if not rename_future.done():
                rename_future.set_exception(exc)
        connection.pending_renames.clear()
