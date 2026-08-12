"""Agent live stream endpoint (plan §13.1, task M6.2).

``GET /api/v1/agent/stream`` is the **distinct** Agent SSE stream.  It is
not the terminal ``/api/v1/events`` WebSocket: it carries canonical Agent
events after an opaque B cursor, tracks conversation/binding identity, and
closes on binding revocation or authentication epoch change.

Wire format
===========

Every frame is ``event: <name>`` + ``data: <json>``:

``agent_event``
    ``{"type": "event", "event": {AgentEventResponse}, "cursor": "E-S"}``

``reset``
    ``{"type": "reset", "reason": "cursor_too_old", "cursor": "E-S"}``
    The cursor is too old (auth-epoch mismatch, retention deletion, or
    beyond the stored max): nothing was silently skipped, and the client
    must reload state from REST with the fresh cursor.

``closed``
    ``{"type": "closed", "code": C, "reason": R}``
    Codes: ``4401`` authentication epoch changed, ``4410`` subscriber too
    slow (recover through REST replay), ``4412`` binding revoked.

Opaque cursor
=============

The cursor format is ``{epoch}-{database_seq}``:

- ``epoch`` is B's persisted authentication epoch at subscription time.  It
  is the reset component: a cursor minted under an earlier epoch is invalid
  and yields an in-band ``reset`` marker.
- ``database_seq`` is the conversation's B-assigned commit-order sequence
  (SQLite ``agent_events`` order).  Storage details are never exposed to
  clients beyond this opaque string.

Delivery contract
=================

- The server subscribes on the :class:`AgentStreamHub` **before** replaying,
  then replays events after the cursor and emits live deltas; an append
  between the replay query and the subscription is deduplicated by
  ``database_seq``.
- A cursor the storage cannot prove continuity for (before the retained
  range, or ahead of the stored max) returns ``reset`` instead of silently
  skipping events.
- Each subscriber has a bounded queue; overflow closes it with ``4410`` and
  the client recovers from REST replay.
- The stream re-checks the persisted auth epoch and the binding state on
  every delivery and on an idle poll, so revocation closes idle streams.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.api.dependencies import get_repositories, require_admin
from termflow_control_plane.auth.epoch import persisted_authentication_epoch
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import AgentEvent
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import (
    CLOSE_AUTH_EPOCH,
    CLOSE_BINDING_REVOKED,
    AgentEventCursor,
    AgentStreamHub,
    AgentStreamSubscriber,
    binding_is_closed,
)

router = APIRouter(
    prefix="/api/v1/agent/stream",
    tags=["agent"],
    dependencies=[Depends(require_admin)],
)

#: How often an idle stream re-checks the persisted auth epoch and binding
#: state, so revocation closes the stream even when no event ever arrives.
_STREAM_POLL_SECONDS = 1.0
#: Replay page size: a large backlog is streamed in bounded pages.
_REPLAY_PAGE_SIZE = 200

SSE_CLOSED = "closed"
SSE_EVENT = "agent_event"
SSE_RESET = "reset"


def _sse_frame(name: str, payload: dict[str, object]) -> str:
    return (
        f"event: {name}\n"
        f"data: {json.dumps(payload, separators=(",", ":"), default=str)}\n\n"
    )


def _parse_cursor(cursor: str) -> tuple[int, int] | None:
    """Parse the opaque ``{epoch}-{seq}`` cursor; None when malformed."""
    epoch_text, separator, seq_text = cursor.partition("-")
    if not separator or not epoch_text.isdigit() or not seq_text.isdigit():
        return None
    epoch, seq = int(epoch_text), int(seq_text)
    if epoch < 1 or seq < 0:
        return None
    return epoch, seq


def _event_envelope(event: AgentEvent) -> dict[str, object]:
    return {
        "event_id": str(event.id),
        "conversation_id": str(event.conversation_id),
        "run_id": str(event.run_id) if event.run_id is not None else None,
        "event_kind": event.event_kind,
        "database_seq": event.database_seq,
        "payload_digest": event.payload_digest,
        "ephemeral": event.ephemeral,
        "created_at": event.created_at.isoformat(),
    }


def _closed_frame(code: int, reason: str) -> str:
    return _sse_frame(SSE_CLOSED, {"type": "closed", "code": code, "reason": reason})


async def _replay_events(
    *,
    conversation_id: UUID | None,
    cursor: tuple[int, int] | None,
    auth_epoch: int,
    event_cursor: AgentEventCursor,
) -> AsyncIterator[tuple[str, int]]:
    """Yield ``(frame, last_delivered_seq)`` after the cursor, or a reset first.

    A ``reset`` frame carries the fresh cursor and leaves ``last_seq`` at 0:
    the caller must not replay anything after it.
    """
    if conversation_id is None:
        # Global streams are live-only: there is no cross-conversation commit
        # order to replay, so the cursor must be the live-only cursor.
        if cursor is not None and cursor != (auth_epoch, 0):
            yield (
                _sse_frame(
                    SSE_RESET,
                    {
                        "type": "reset",
                        "reason": "cursor_too_old",
                        "cursor": f"{auth_epoch}-0",
                    },
                ),
                0,
            )
        return

    max_seq = await event_cursor.current_seq(conversation_id)
    too_old = cursor is not None and (
        cursor[0] != auth_epoch or cursor[1] > max_seq
    )
    if not too_old and cursor is not None and cursor[1] > 0 and max_seq > 0:
        # Retention may have removed events before the cursor: if the earliest
        # retained event is after the cursor, continuity cannot be proven.
        earliest = await event_cursor.list_for_conversation(
            conversation_id, limit=1, offset=0
        )
        if earliest and cursor[1] < earliest[0].database_seq:
            too_old = True
    if too_old:
        yield (
            _sse_frame(
                SSE_RESET,
                {
                    "type": "reset",
                    "reason": "cursor_too_old",
                    "cursor": f"{auth_epoch}-{max_seq}",
                },
            ),
            0,
        )
        return

    if cursor is None:
        return
    last_seq = cursor[1]
    while True:
        page = await event_cursor.list_since_cursor(
            conversation_id, last_seq, limit=_REPLAY_PAGE_SIZE
        )
        for event in page:
            last_seq = event.database_seq
            yield (
                _sse_frame(
                    SSE_EVENT,
                    {
                        "type": "event",
                        "event": _event_envelope(event),
                        "cursor": f"{auth_epoch}-{last_seq}",
                    },
                ),
                last_seq,
            )
        if len(page) < _REPLAY_PAGE_SIZE:
            return


async def _live_loop(
    *,
    subscriber: AgentStreamSubscriber,
    conversation_id: UUID | None,
    binding_id: UUID | None,
    auth_epoch: int,
    repositories: RepositoryBundle,
    last_seq: int,
) -> AsyncIterator[str]:
    """Emit live deltas, deduplicating by ``database_seq``.

    The persisted auth epoch and binding state are re-checked on every
    delivery and on the idle poll, closing the stream on revocation.
    """
    seen_event_ids: set[UUID] = set()
    while True:
        try:
            event = await asyncio.wait_for(
                subscriber.queue.get(), timeout=_STREAM_POLL_SECONDS
            )
        except TimeoutError:
            event = None
        if await persisted_authentication_epoch(repositories) != auth_epoch:
            yield _closed_frame(CLOSE_AUTH_EPOCH, "authentication_epoch_changed")
            return
        if binding_id is not None:
            binding = await repositories.agent_bindings.get_by_id(binding_id)
            if binding is None or binding_is_closed(binding.status):
                yield _closed_frame(CLOSE_BINDING_REVOKED, "binding_revoked")
                return
        if subscriber.closed.is_set():
            yield _closed_frame(subscriber.close_code, subscriber.close_reason)
            return
        if event is None:
            continue
        if conversation_id is not None:
            if event.database_seq <= last_seq:
                continue  # duplicate or already replayed
            last_seq = event.database_seq
            cursor = f"{auth_epoch}-{last_seq}"
        else:
            # Global streams are live-only with no cross-conversation commit
            # order, so the cursor's sequence component stays 0 and duplicates
            # are deduplicated by event id.
            if event.id in seen_event_ids:
                continue
            seen_event_ids.add(event.id)
            cursor = f"{auth_epoch}-0"
        yield _sse_frame(
            SSE_EVENT,
            {
                "type": "event",
                "event": _event_envelope(event),
                "cursor": cursor,
            },
        )


async def stream_events_generator(
    *,
    hub: AgentStreamHub,
    repositories: RepositoryBundle,
    event_cursor: AgentEventCursor,
    conversation_id: UUID | None,
    binding_id: UUID | None,
    cursor: tuple[int, int] | None,
    auth_epoch: int,
) -> AsyncIterator[str]:
    """The SSE body: subscribe, replay from the cursor, then live deltas.

    Split from the HTTP endpoint so tests can drive the exact generator the
    endpoint serves.  The hub subscription happens before any replay, and
    every frame the subscriber delivers is deduplicated by ``database_seq``.
    """
    subscriber = await hub.subscribe(
        conversation_id=conversation_id,
        binding_id=binding_id,
        auth_epoch=auth_epoch,
    )
    try:
        if subscriber.closed.is_set():
            yield _closed_frame(subscriber.close_code, subscriber.close_reason)
            return
        last_seq = 0
        async for frame, after_seq in _replay_events(
            conversation_id=conversation_id,
            cursor=cursor,
            auth_epoch=auth_epoch,
            event_cursor=event_cursor,
        ):
            last_seq = after_seq
            yield frame
        async for frame in _live_loop(
            subscriber=subscriber,
            conversation_id=conversation_id,
            binding_id=binding_id,
            auth_epoch=auth_epoch,
            repositories=repositories,
            last_seq=last_seq,
        ):
            yield frame
    finally:
        await hub.unsubscribe(subscriber)


@router.get("")
async def stream_agent_events(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    request: Request,
    conversation_id: Annotated[UUID | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    hub = cast(AgentStreamHub, request.app.state.agent_stream_hub)
    sessions = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    state = await repositories.auth_state.get()
    auth_epoch = state.epoch

    parsed_cursor = _parse_cursor(cursor) if cursor is not None else None
    if cursor is not None and parsed_cursor is None:
        raise TermFlowError(
            "invalid_cursor", 400, "The Agent stream cursor is invalid."
        )

    binding_id: UUID | None = None
    if conversation_id is not None:
        conversation = await repositories.agent_conversations.get_by_id(conversation_id)
        if conversation is None:
            raise TermFlowError(
                "conversation_not_found",
                404,
                "The Agent Conversation does not exist.",
            )
        binding = await repositories.agent_bindings.get_by_id(conversation.binding_id)
        if binding is None or binding_is_closed(binding.status):
            raise TermFlowError(
                "binding_revoked",
                403,
                "The Agent Binding is revoked or disabled.",
            )
        binding_id = binding.id

    event_cursor = AgentEventCursor(
        repositories.agent_events,
        sessions,
        publisher=hub.publish,
    )
    return StreamingResponse(
        stream_events_generator(
            hub=hub,
            repositories=repositories,
            event_cursor=event_cursor,
            conversation_id=conversation_id,
            binding_id=binding_id,
            cursor=parsed_cursor,
            auth_epoch=auth_epoch,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
