"""In-memory registry for independently connected TermFlow Instances."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from termflow_protocol import (
    CommandResultPayload,
    MessageType,
    PaneCaptureErrorPayload,
    PaneCaptureRequestPayload,
    PaneCaptureResultPayload,
    TerminalInputPayload,
    TermRenameResultPayload,
    TopologySnapshot,
    WireMessage,
)


class InstanceOffline(LookupError):
    pass


class InstanceOnline(RuntimeError):
    pass


class InstanceRetired(LookupError):
    pass


class ConnectionBackpressure(RuntimeError):
    pass


class CapabilityUnavailable(RuntimeError):
    """A request needs a capability the Instance never negotiated.

    Raised when the live connection does not carry a negotiated capability
    flag (e.g. ``typed_keys``); B fails closed instead of silently degrading
    the request against an old A.
    """


_CAPTURE_TIMEOUT_SECONDS = 5.0


def _queued_terminal_bytes(message: WireMessage) -> int:
    if message.type is not MessageType.TERMINAL_INPUT:
        return 0
    return len(TerminalInputPayload.model_validate(message.payload).to_bytes())


class BoundedWireQueue:
    def __init__(self, *, max_messages: int, max_bytes: int) -> None:
        self._max_messages = max_messages
        self._queue: asyncio.Queue[tuple[WireMessage, int]] = asyncio.Queue(
            maxsize=max_messages + 1
        )
        self._max_bytes = max_bytes
        self._queued_bytes = 0

    def put_nowait(self, message: WireMessage) -> None:
        byte_count = _queued_terminal_bytes(message)
        message_limit = self._max_messages + (
            1 if message.type is MessageType.TERMINAL_CLOSE else 0
        )
        if self._queue.qsize() >= message_limit:
            raise asyncio.QueueFull
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
    capabilities: frozenset[str] = frozenset()
    hello_ready: asyncio.Event = field(default_factory=asyncio.Event)
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(UTC))
    pending: dict[UUID, asyncio.Future[CommandResultPayload]] = field(default_factory=dict)
    pending_renames: dict[UUID, asyncio.Future[TermRenameResultPayload]] = field(
        default_factory=dict
    )
    pending_captures: dict[
        UUID, asyncio.Future[PaneCaptureResultPayload | PaneCaptureErrorPayload]
    ] = field(default_factory=dict)
    replaced: asyncio.Event = field(default_factory=asyncio.Event)
    #: Capabilities negotiated from A's bridge hello (M2.4). Both default to
    #: False so an old A that never sends the fields fails closed.
    bounded_capture: bool = False
    typed_keys: bool = False

    def require_capability(
        self,
        *,
        bounded_capture: bool = False,
        typed_keys: bool = False,
    ) -> None:
        """Fail closed when A did not negotiate a requested capability.

        The M5 command path checks ``typed_keys=True`` before submitting
        typed key input; capture capability is enforced inside
        :meth:`submit_capture`.
        """
        if bounded_capture and not self.bounded_capture:
            raise CapabilityUnavailable(
                f"instance {self.instance_id} did not negotiate bounded pane capture"
            )
        if typed_keys and not self.typed_keys:
            raise CapabilityUnavailable(
                f"instance {self.instance_id} did not negotiate typed key input"
            )

    async def submit_capture(
        self,
        request: PaneCaptureRequestPayload,
        *,
        timeout_seconds: float = _CAPTURE_TIMEOUT_SECONDS,
    ) -> PaneCaptureResultPayload | PaneCaptureErrorPayload:
        """Correlate and forward one bounded pane capture request to A.

        The request is enqueued on the outbound wire queue and awaited until A
        answers with the matching :class:`PaneCaptureResultPayload` or
        :class:`PaneCaptureErrorPayload` for the same ``request_id``. If A does
        not answer within ``timeout_seconds`` the awaitable resolves with a
        synthetic :class:`PaneCaptureErrorPayload` (``capture_timeout``).
        Connection loss while waiting raises :class:`InstanceOffline`.

        An A that never negotiated ``bounded_capture`` (an old A) fails closed
        with a ``capture_unsupported`` error payload instead of silently
        degrading the request.
        """
        if not self.bounded_capture:
            return PaneCaptureErrorPayload(
                request_id=request.request_id,
                instance_id=self.instance_id,
                pane_id=request.pane_id,
                error_code="capture_unsupported",
                message=(
                    "bounded pane capture is not supported by this Instance: "
                    "the capability was not negotiated in its bridge hello"
                ),
            )
        if request.instance_id != self.instance_id:
            raise ValueError(
                f"capture request for instance {request.instance_id} submitted "
                f"to connection {self.instance_id}"
            )
        future: asyncio.Future[
            PaneCaptureResultPayload | PaneCaptureErrorPayload
        ] = asyncio.get_running_loop().create_future()
        self.pending_captures[request.request_id] = future
        message = WireMessage(
            type=MessageType.PANE_CAPTURE_REQUEST,
            instance_id=self.instance_id,
            payload=request.model_dump(mode="json"),
        )
        try:
            try:
                self.outbound.put_nowait(message)
            except asyncio.QueueFull as exc:
                raise ConnectionBackpressure(str(self.instance_id)) from exc
            try:
                async with asyncio.timeout(timeout_seconds):
                    return await future
            except TimeoutError:
                return PaneCaptureErrorPayload(
                    request_id=request.request_id,
                    instance_id=self.instance_id,
                    pane_id=request.pane_id,
                    error_code="capture_timeout",
                    message="The Instance did not answer the capture request in time.",
                )
        finally:
            self.pending_captures.pop(request.request_id, None)

    def resolve_capture(
        self,
        request_id: UUID,
        outcome: PaneCaptureResultPayload | PaneCaptureErrorPayload,
    ) -> bool:
        """Complete the pending capture future addressed by ``request_id``.

        Unknown or already-completed request ids are ignored and ``False`` is
        returned, so stale or duplicated replies never overwrite a resolved
        future.
        """
        future = self.pending_captures.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(outcome)
        return True


class LiveInstanceRegistry:
    def __init__(self, *, queue_size: int, queue_max_bytes: int = 1024 * 1024) -> None:
        self._queue_size = queue_size
        self._queue_max_bytes = queue_max_bytes
        self._connections: dict[UUID, LiveConnection] = {}
        self._retired: set[UUID] = set()
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
            if instance_id in self._retired:
                raise InstanceRetired(str(instance_id))
            previous = self._connections.get(instance_id)
            self._connections[instance_id] = connection
            if previous is not None:
                previous.replaced.set()
        return connection

    async def begin_retirement(self, instance_id: UUID) -> None:
        async with self._lock:
            if instance_id in self._connections:
                raise InstanceOnline(str(instance_id))
            if instance_id in self._retired:
                raise InstanceRetired(str(instance_id))
            self._retired.add(instance_id)

    async def cancel_retirement(self, instance_id: UUID) -> None:
        async with self._lock:
            self._retired.discard(instance_id)

    async def reactivate(self, instance_id: UUID) -> None:
        async with self._lock:
            self._retired.discard(instance_id)

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

    def force_disconnect_current_nowait(self, instance_id: UUID) -> bool:
        """Bound remote lifetime when even the reserved teardown slot is unavailable."""

        connection = self._connections.get(instance_id)
        if connection is None:
            return False
        connection.replaced.set()
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
        for capture_future in tuple(connection.pending_captures.values()):
            if not capture_future.done():
                capture_future.set_exception(exc)
        connection.pending_captures.clear()
