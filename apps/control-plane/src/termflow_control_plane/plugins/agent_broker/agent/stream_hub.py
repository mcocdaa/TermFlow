"""In-process live fan-out for canonical Agent events (plan §13.1, task M6.2).

This module is the **distinct** Agent stream infrastructure: it must not be
confused with the terminal ``/api/v1/events`` path.  The terminal
:class:`~termflow_control_plane.connections.event_hub.EventHub` only carries
instance/auth-epoch metadata; the Agent stream tracks conversation and
binding identity and closes on binding revocation or auth epoch change.

Components
==========

:class:`AgentStreamHub`
    Bounded per-subscriber queues over the canonical event append stream.
    Subscribers are scoped by conversation (``None`` = global live stream)
    and binding.  A subscriber whose queue overflows is closed with
    ``4410 stream_too_slow``; the client then recovers through REST replay.
    ``synchronize_epoch`` closes every subscriber on an authentication epoch
    change (mirroring the terminal EventHub), and ``close_for_binding``
    closes the subscribers of a revoked binding.

:class:`AgentEventCursor`
    The canonical event timeline accessor with the live publish hook.
    Writers append events through :meth:`AgentEventCursor.append`, which
    delegates to the authoritative :class:`AgentEventRepository` and then
    publishes the committed event to the hub only when the row was actually
    inserted, so the durable insert and the live fan-out stay exactly-once
    (a redelivered dedup key returns the original row without re-publishing,
    review fix m3).  ``current_seq`` mints reset cursors.

Close codes
===========

``4401`` authentication epoch changed
``4410`` subscriber too slow (bounded queue overflow)
``4412`` binding revoked
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.persistence.models import AgentEvent
from termflow_control_plane.persistence.repositories import AgentEventRepository

#: Per-subscriber live queue bound; overflow closes the subscriber.
AGENT_STREAM_QUEUE_SIZE = 128

CLOSE_AUTH_EPOCH = 4401
CLOSE_TOO_SLOW = 4410
CLOSE_BINDING_REVOKED = 4412

#: Binding states that close an Agent stream (plan §13.1 revocation closure).
_CLOSED_BINDING_STATES = frozenset({"revoked", "disabled"})

PublishHook = Callable[[AgentEvent], Awaitable[None]]


@dataclass(eq=False, slots=True)
class AgentStreamSubscriber:
    """One SSE subscriber's bounded live queue.

    ``conversation_id`` scopes the fan-out (``None`` receives every
    conversation's events, the global live stream); ``binding_id`` is the
    revocation scope.  ``closed`` is set exactly once; the endpoint turns a
    closed subscriber into an in-band ``closed`` SSE frame.
    """

    conversation_id: UUID | None
    binding_id: UUID | None
    queue: asyncio.Queue[AgentEvent]
    id: UUID = field(default_factory=uuid4)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    close_code: int = CLOSE_TOO_SLOW
    close_reason: str = "stream_too_slow"


class AgentStreamHub:
    """Bounded, history-free fan-out for canonical Agent events.

    History lives in the database (``agent_events``); the hub only carries
    live deltas.  The stream endpoint subscribes **before** replaying from
    the cursor and deduplicates by ``database_seq``, so an append between the
    replay query and the subscription is delivered exactly once.
    """

    def __init__(self, *, queue_size: int = AGENT_STREAM_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[UUID, AgentStreamSubscriber] = {}
        self._lock = asyncio.Lock()
        self._auth_epoch = 1

    async def subscribe(
        self,
        *,
        conversation_id: UUID | None,
        binding_id: UUID | None,
        auth_epoch: int = 1,
    ) -> AgentStreamSubscriber:
        subscriber = AgentStreamSubscriber(
            conversation_id=conversation_id,
            binding_id=binding_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        async with self._lock:
            if auth_epoch == self._auth_epoch:
                self._subscribers[subscriber.id] = subscriber
            else:
                subscriber.close_code = CLOSE_AUTH_EPOCH
                subscriber.close_reason = "authentication_epoch_changed"
                subscriber.closed.set()
        return subscriber

    async def unsubscribe(self, subscriber: AgentStreamSubscriber) -> bool:
        async with self._lock:
            removed = self._subscribers.pop(subscriber.id, None)
        subscriber.closed.set()
        return removed is not None

    async def publish(self, event: AgentEvent) -> list[UUID]:
        """Fan a committed event out to matching subscribers.

        Returns the ids of subscribers dropped for being too slow.
        """
        dropped: list[UUID] = []
        async with self._lock:
            for subscriber_id, subscriber in tuple(self._subscribers.items()):
                if (
                    subscriber.conversation_id is not None
                    and subscriber.conversation_id != event.conversation_id
                ):
                    continue
                try:
                    subscriber.queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._subscribers.pop(subscriber_id, None)
                    subscriber.closed.set()
                    dropped.append(subscriber_id)
        return dropped

    async def synchronize_epoch(self, epoch: int) -> int:
        """Atomically reject stale subscriptions and close current ones.

        Mirrors the terminal EventHub auth-epoch closure so idle streams are
        closed even when no event ever arrives on them again.
        """
        if epoch < 1:
            raise ValueError("authentication epoch must be positive")
        async with self._lock:
            if epoch == self._auth_epoch:
                return 0
            self._auth_epoch = epoch
            subscribers = tuple(self._subscribers.values())
            self._subscribers.clear()
            for subscriber in subscribers:
                subscriber.close_code = CLOSE_AUTH_EPOCH
                subscriber.close_reason = "authentication_epoch_changed"
                subscriber.closed.set()
        return len(subscribers)

    async def close_for_binding(
        self,
        binding_id: UUID,
        *,
        code: int = CLOSE_BINDING_REVOKED,
        reason: str = "binding_revoked",
    ) -> int:
        """Close every subscriber whose binding was revoked."""
        async with self._lock:
            matching = [
                (subscriber_id, subscriber)
                for subscriber_id, subscriber in self._subscribers.items()
                if subscriber.binding_id == binding_id
            ]
            for subscriber_id, subscriber in matching:
                self._subscribers.pop(subscriber_id, None)
                subscriber.close_code = code
                subscriber.close_reason = reason
                subscriber.closed.set()
        return len(matching)

    async def subscriber_count(self) -> int:
        async with self._lock:
            return len(self._subscribers)


class AgentEventCursor:
    """Canonical event timeline with an in-process live publish hook.

    Wraps the authoritative :class:`AgentEventRepository` (which remains the
    single writer) and adds the M6.2 live path:

    - :meth:`append` returns ``(event, inserted)``: the inserted flag is the
      repository's dedup verdict.  The committed event is published to the
      hub only when it was actually inserted, so live subscribers see
      exactly the events the database records, in commit order, and a
      redelivered dedup key never fans out twice (review fix m3).
    - :meth:`current_seq` returns the conversation's maximum
      ``database_seq``, used to mint the ``reset`` cursor after a
      ``cursor_too_old``.
    """

    def __init__(
        self,
        repository: AgentEventRepository,
        sessions: async_sessionmaker[AsyncSession],
        *,
        publisher: PublishHook | None = None,
    ) -> None:
        self._repository = repository
        self._sessions = sessions
        self._publisher = publisher

    async def append(
        self,
        *,
        conversation_id: UUID,
        event_kind: str,
        dedup_key: str,
        payload_digest: str,
        run_id: UUID | None = None,
        ephemeral: bool = False,
        payload_json: str | None = None,
    ) -> tuple[AgentEvent, bool]:
        """Append one canonical event; return ``(event, inserted)``.

        ``inserted`` is False when the ``dedup_key`` was already persisted
        for the conversation: the row is returned unchanged and the live
        hub is not re-published.
        """
        event, inserted = await self._repository.append_checked(
            conversation_id=conversation_id,
            event_kind=event_kind,
            dedup_key=dedup_key,
            payload_digest=payload_digest,
            run_id=run_id,
            ephemeral=ephemeral,
            payload_json=payload_json,
        )
        if inserted and self._publisher is not None:
            await self._publisher(event)
        return event, inserted

    async def current_seq(self, conversation_id: UUID) -> int:
        """The conversation's maximum committed ``database_seq`` (0 if none)."""
        async with self._sessions() as session:
            value = await session.scalar(
                select(func.max(AgentEvent.database_seq)).where(
                    AgentEvent.conversation_id == conversation_id
                )
            )
            return int(value or 0)

    async def list_since_cursor(
        self,
        conversation_id: UUID,
        cursor: int,
        *,
        limit: int | None = None,
    ) -> list[AgentEvent]:
        return await self._repository.list_since_cursor(
            conversation_id, cursor, limit=limit
        )

    async def list_for_conversation(
        self,
        conversation_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentEvent]:
        return await self._repository.list_for_conversation(
            conversation_id, limit=limit, offset=offset
        )


def binding_is_closed(status: str) -> bool:
    """Whether a binding state must close Agent streams (revoked/disabled)."""
    return status in _CLOSED_BINDING_STATES
