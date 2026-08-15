"""Agent live stream API tests (plan §13.1, task M6.2).

Covers the distinct Agent SSE stream at ``/api/v1/agent/stream``:

- SSE happy path: subscribe, then a canonical event append is received live.
- Replay from an opaque cursor preserves ``database_seq`` order.
- ``cursor_too_old`` (auth-epoch mismatch, cursor beyond the stored max, or
  retention deletion shrinking the available range) returns an in-band
  ``reset`` marker instead of silently skipping events.
- The opaque cursor format carries an epoch/reset component.
- Slow subscribers are closed by the in-process hub on queue overflow.
- Binding revocation and authentication epoch changes close the stream with
  an in-band ``closed`` frame.
- Duplicate ``database_seq`` deliveries are deduplicated.

The stream must not reuse the terminal ``/api/v1/events`` EventHub: it is
backed by the :class:`AgentStreamHub` which tracks conversation/binding
identity, and events are appended through :class:`AgentEventCursor` whose
publish hook feeds the hub.

The TestClient portal runs the app; the endpoint's SSE generator is driven
directly inside that loop (``client.stream`` cannot read an endless SSE body
because this starlette transport waits for the app task to complete).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete
from termflow_control_plane.persistence.models import AgentEvent
from termflow_control_plane.plugins.agent_broker.agent.agui_projection import (
    AgentEventProjector,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import (
    AgentEventCursor,
    AgentStreamHub,
)

#: In-band close codes produced by the stream (documented in agent_stream.py).
CLOSE_AUTH_EPOCH = 4401
CLOSE_BINDING_REVOKED = 4412
CLOSE_TOO_SLOW = 4410


def _create_profile(
    client: TestClient,
    admin_headers: dict[str, str],
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "opencode",
            "backend_kind": "opencode",
            "config": '{"model": "default"}',
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    profile_id: UUID,
    term_id: UUID,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/bindings",
        headers=admin_headers,
        json={"profile_id": str(profile_id), "term_id": str(term_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_conversation(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    binding_id: UUID,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/conversations",
        headers=admin_headers,
        json={"binding_id": str(binding_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> UUID:
    profile = _create_profile(client, admin_headers)
    term = provision_term(name="stream-term")
    binding = _create_binding(
        client,
        admin_headers,
        profile_id=UUID(str(profile["profile_id"])),
        term_id=term.instance_id,
    )
    return UUID(str(binding["binding_id"]))


def _event_cursor(client: TestClient) -> AgentEventCursor:
    """The canonical append path bound to the app's hub publish hook."""
    state = client.app.state
    return AgentEventCursor(
        state.repositories.agent_events,
        state.session_factory,
        publisher=state.agent_stream_hub.publish,
    )


def _append_event(
    client: TestClient,
    conversation_id: UUID,
    *,
    kind: str = "run_started",
    dedup_key: str | None = None,
    run_id: UUID | None = None,
    payload: str | None = None,
) -> int:
    """Append a canonical event through the publish hook; returns database_seq."""
    cursor = _event_cursor(client)
    if payload is not None:
        payload_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    else:
        payload_digest = "test-digest"
    event, _inserted = client.portal.call(
        lambda: cursor.append(
            conversation_id=conversation_id,
            event_kind=kind,
            dedup_key=dedup_key or f"dedup-{uuid4()}",
            payload_digest=payload_digest,
            run_id=run_id,
            payload_json=payload,
        )
    )
    return event.database_seq


def _seed_run(client: TestClient, conversation_id: UUID) -> UUID:
    """Create a run row so run events can carry a foreign-keyed run_id."""
    run = client.portal.call(
        lambda: client.app.state.repositories.agent_runs.create(
            conversation_id=conversation_id
        )
    )
    return run.id


def _message_delta_payload(message_id: UUID, text: str = "hello") -> str:
    return json.dumps(
        {
            "message_id": str(message_id),
            "part_id": "part-1",
            "assembly_revision": 1,
            "text": text,
            "ephemeral": True,
        },
        separators=(",", ":"),
    )


def _run_started_payload() -> str:
    return json.dumps({"input_id": None}, separators=(",", ":"))


def _current_auth_epoch(client: TestClient) -> int:
    return client.portal.call(client.app.state.repositories.auth_state.get).epoch


def _rotate_auth_epoch(client: TestClient) -> int:
    """Bump the persisted authentication epoch exactly like reset/rotation."""
    return client.portal.call(
        client.app.state.repositories.auth_state.rotate_credentials
    )


def _set_binding_status(client: TestClient, binding_id: UUID, status: str) -> None:
    client.portal.call(
        client.app.state.repositories.agent_bindings.set_status,
        binding_id,
        status,
    )


def _delete_events_before(client: TestClient, conversation_id: UUID, seq: int) -> None:
    """Simulate retention deletion: remove events below ``seq``."""

    async def delete_rows(sessions, conversation_id: UUID, seq: int) -> None:
        async with sessions() as session:
            await session.execute(
                delete(AgentEvent).where(
                    AgentEvent.conversation_id == conversation_id,
                    AgentEvent.database_seq < seq,
                )
            )
            await session.commit()

    client.portal.call(delete_rows, client.app.state.session_factory, conversation_id, seq)


def _wait_for(
    client: TestClient,
    poll: Callable[[], bool],
    timeout: float = 10.0,
    message: str = "condition never became true",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if poll():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def _wait_subscribed(client: TestClient, hub: AgentStreamHub) -> None:
    _wait_for(
        client,
        lambda: client.portal.call(hub.subscriber_count) > 0,
        message="stream never subscribed",
    )


def _parse_frame(frame: str) -> tuple[str, dict[str, object]]:
    name: str | None = None
    data: list[str] = []
    for line in frame.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data.append(line[len("data: ") :])
    assert name is not None
    return name, json.loads("".join(data))


class _GeneratorStream:
    """Drive the endpoint's SSE generator inside the app's portal loop.

    The consumer task runs in the portal alongside the app, so the test can
    append events and revoke bindings while the stream is open.
    """

    def __init__(
        self,
        client: TestClient,
        *,
        conversation_id: UUID | None = None,
        binding_id: UUID | None = None,
        cursor: tuple[int, int] | None = None,
        wire: str = "canonical",
    ) -> None:
        self._client = client
        state = client.app.state
        self.hub = state.agent_stream_hub
        self.repositories = state.repositories
        self.event_cursor = _event_cursor(client)
        self.conversation_id = conversation_id
        self.binding_id = binding_id
        self.cursor = cursor
        self.wire = wire
        self.auth_epoch = _current_auth_epoch(client)
        self.frames: list[str] = []
        self._task: asyncio.Task[None] | None = None
        self._frames_holder: list[str] | None = None

    def __enter__(self) -> _GeneratorStream:
        from termflow_control_plane.api.agent_stream import stream_events_generator

        client = self._client
        holder = client.portal.call(list)
        self._frames_holder = holder
        projector = AgentEventProjector() if self.wire == "agui" else None
        generator = stream_events_generator(
            hub=self.hub,
            repositories=self.repositories,
            event_cursor=self.event_cursor,
            conversation_id=self.conversation_id,
            binding_id=self.binding_id,
            cursor=self.cursor,
            auth_epoch=self.auth_epoch,
            projector=projector,
        )

        async def consume() -> None:
            async for frame in generator:
                holder.append(frame)

        # Schedule the endless consumer inside the app's loop without
        # awaiting it: portal.call awaits anything awaitable, and an
        # ``asyncio.Task`` is awaitable, so returning the task directly would
        # block until the SSE stream ends (never).  The lambda appends the
        # task to a list and returns None, so the call returns immediately.
        tasks = client.portal.call(list)
        client.portal.call(lambda: tasks.append(asyncio.create_task(consume())))
        self._task = tasks[0]
        _wait_subscribed(client, self.hub)
        return self

    def __exit__(self, *_exc: object) -> None:
        client = self._client
        if self._task is not None and not client.portal.call(self._task.done):
            client.portal.call(self._task.cancel)
        self.frames = client.portal.call(lambda: list(self._frames_holder or ()))

    def parsed_frames(self) -> list[tuple[str, dict[str, object]]]:
        raw = self._client.portal.call(lambda: list(self._frames_holder or ()))
        return [_parse_frame(frame) for frame in raw]

    def raw_frames(self) -> list[str]:
        return self._client.portal.call(lambda: list(self._frames_holder or ()))

    def wait_for(
        self,
        predicate: Callable[[list[tuple[str, dict[str, object]]]], bool],
        timeout: float = 10.0,
    ) -> list[tuple[str, dict[str, object]]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self.parsed_frames()
            if predicate(frames):
                return frames
            time.sleep(0.02)
        raise AssertionError(f"frames never matched predicate: {self.parsed_frames()}")

    def wait_done(self, timeout: float = 10.0) -> None:
        client = self._client
        assert self._task is not None

        def finished() -> bool:
            return bool(client.portal.call(self._task.done))

        _wait_for(client, finished, timeout=timeout, message="stream never closed")
        client.portal.call(self._task.result)


class TestAgentStreamAuthentication:
    def test_unauthenticated_stream_is_rejected(self, client) -> None:
        response = client.get("/api/v1/agent/stream")
        assert response.status_code == 401

    def test_malformed_cursor_is_rejected(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        for bad in ("abc", "1-2-3", "-5", "1-", "1-x", "0-1", "x-1"):
            response = client.get(
                "/api/v1/agent/stream",
                headers=admin_headers,
                params={"conversation_id": str(conversation_id), "cursor": bad},
            )
            assert response.status_code == 400, bad

    def test_unknown_conversation_is_rejected(self, client, admin_headers) -> None:
        response = client.get(
            "/api/v1/agent/stream",
            headers=admin_headers,
            params={"conversation_id": str(uuid4())},
        )
        assert response.status_code == 404

    def test_revoked_binding_rejects_subscription(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        _set_binding_status(client, binding_id, "revoked")
        response = client.get(
            "/api/v1/agent/stream",
            headers=admin_headers,
            params={"conversation_id": str(conversation["conversation_id"])},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "binding_revoked"

    def test_endpoint_serves_an_sse_streaming_response(
        self, client, admin_headers, provision_term
    ) -> None:
        from termflow_control_plane.api.agent_stream import stream_agent_events

        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        request = SimpleNamespace(app=client.app)
        response = client.portal.call(
            lambda: stream_agent_events(
                client.app.state.repositories,
                request,
                conversation_id=conversation_id,
            )
        )
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        # Release the lazy generator and its hub subscription.
        client.portal.call(response.body_iterator.aclose)


class TestAgentStreamLive:
    def test_happy_path_subscribe_append_receive(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        epoch = _current_auth_epoch(client)

        with _GeneratorStream(client, conversation_id=conversation_id) as stream:
            seq = _append_event(
                client, conversation_id, kind="message_delta", dedup_key="live-1"
            )
            frames = stream.wait_for(
                lambda f: any(name == "agent_event" for name, _d in f)
            )

        assert seq == 1
        name, data = frames[0]
        assert name == "agent_event"
        assert data["type"] == "event"
        event = data["event"]
        assert isinstance(event, dict)
        assert event["conversation_id"] == str(conversation_id)
        assert event["event_kind"] == "message_delta"
        assert event["database_seq"] == 1
        # The opaque cursor carries the epoch/reset component plus the seq.
        assert data["cursor"] == f"{epoch}-1"
        parts = str(data["cursor"]).split("-")
        assert int(parts[0]) == epoch
        assert int(parts[1]) == 1

    def test_global_stream_forwards_all_conversations(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        first = UUID(
            str(
                _create_conversation(client, admin_headers, binding_id=binding_id)[
                    "conversation_id"
                ]
            )
        )
        second = UUID(
            str(
                _create_conversation(client, admin_headers, binding_id=binding_id)[
                    "conversation_id"
                ]
            )
        )
        epoch = _current_auth_epoch(client)

        with _GeneratorStream(client) as stream:
            _append_event(client, first, dedup_key="global-1")
            _append_event(client, second, dedup_key="global-2")
            frames = stream.wait_for(
                lambda f: sum(1 for name, _d in f if name == "agent_event") >= 2
            )

        events = [data["event"] for name, data in frames if name == "agent_event"]
        assert {event["conversation_id"] for event in events} == {
            str(first),
            str(second),
        }
        # Global streams are live-only: the cursor seq component stays 0.
        assert all(data["cursor"] == f"{epoch}-0" for name, data in frames)


class TestAgentStreamReplay:
    def test_replay_from_cursor_preserves_order(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        epoch = _current_auth_epoch(client)
        for index in (1, 2, 3):
            _append_event(
                client,
                conversation_id,
                kind="message_delta",
                dedup_key=f"pre-{index}",
            )

        with _GeneratorStream(
            client, conversation_id=conversation_id, cursor=(epoch, 1)
        ) as stream:
            seq_4 = _append_event(
                client, conversation_id, kind="run_completed", dedup_key="live-4"
            )
            frames = stream.wait_for(
                lambda f: sum(1 for name, _d in f if name == "agent_event") >= 3
            )

        events = [data["event"] for name, data in frames if name == "agent_event"]
        assert [event["database_seq"] for event in events] == [2, 3, 4]
        assert [event["event_kind"] for event in events] == [
            "message_delta",
            "message_delta",
            "run_completed",
        ]
        assert seq_4 == 4  # the live append is the fourth event

    def test_duplicate_database_seq_is_deduplicated(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))

        with _GeneratorStream(client, conversation_id=conversation_id) as stream:
            # The repository dedupes by dedup_key: both appends return the
            # same event/database_seq, and the publish hook fires only for
            # the inserted row (review fix m3), so the endpoint's own
            # database_seq dedup is the second line of defense.
            _append_event(client, conversation_id, dedup_key="same-key")
            _append_event(client, conversation_id, dedup_key="same-key")
            stream.wait_for(
                lambda f: any(name == "agent_event" for name, _d in f)
            )
            time.sleep(0.3)

        events = [
            data["event"]
            for name, data in stream.parsed_frames()
            if name == "agent_event"
        ]
        assert [event["database_seq"] for event in events] == [1]

    def test_dedup_hit_does_not_republish(
        self, client, admin_headers, provision_term
    ) -> None:
        """Review fix m3: a deduped append must not fan out a second time."""
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        hub = client.app.state.agent_stream_hub
        subscriber = client.portal.call(
            lambda: hub.subscribe(
                conversation_id=conversation_id, binding_id=binding_id
            )
        )
        try:
            _append_event(client, conversation_id, dedup_key="same-key")
            # The inserted event is published exactly once.
            event = client.portal.call(subscriber.queue.get)
            assert event.database_seq == 1
            # A dedup replay of the same key must not re-publish.
            _append_event(client, conversation_id, dedup_key="same-key")
            assert subscriber.queue.empty() is True
        finally:
            client.portal.call(lambda: hub.unsubscribe(subscriber))


class TestAgentStreamCursorTooOld:
    def test_epoch_mismatched_cursor_returns_reset(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        _append_event(client, conversation_id, dedup_key="pre-1")
        epoch = _current_auth_epoch(client)

        with _GeneratorStream(
            client, conversation_id=conversation_id, cursor=(epoch + 1, 1)
        ) as stream:
            frames = stream.wait_for(
                lambda f: any(name == "reset" for name, _d in f)
            )

        name, data = frames[0]
        assert name == "reset"
        assert data["type"] == "reset"
        assert data["reason"] == "cursor_too_old"
        cursor = str(data["cursor"])
        parts = cursor.split("-")
        assert int(parts[0]) == epoch
        assert int(parts[1]) == 1  # current max seq

    def test_cursor_beyond_max_seq_returns_reset(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        _append_event(client, conversation_id, dedup_key="pre-1")
        epoch = _current_auth_epoch(client)

        with _GeneratorStream(
            client, conversation_id=conversation_id, cursor=(epoch, 99)
        ) as stream:
            frames = stream.wait_for(
                lambda f: any(name == "reset" for name, _d in f)
            )

        assert frames[0][1]["reason"] == "cursor_too_old"
        assert str(frames[0][1]["cursor"]) == f"{epoch}-1"

    def test_retention_deletion_makes_cursor_too_old(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        for index in (1, 2, 3):
            _append_event(client, conversation_id, dedup_key=f"pre-{index}")
        epoch = _current_auth_epoch(client)
        # Retention removed events 1 and 2; a cursor at 2 can no longer prove
        # continuity, so the server must reset rather than silently skip.
        _delete_events_before(client, conversation_id, seq=3)

        with _GeneratorStream(
            client, conversation_id=conversation_id, cursor=(epoch, 2)
        ) as stream:
            frames = stream.wait_for(
                lambda f: any(name == "reset" for name, _d in f)
            )

        assert frames[0][1]["reason"] == "cursor_too_old"
        assert str(frames[0][1]["cursor"]) == f"{epoch}-3"

    def test_global_stream_cursor_must_be_live_only(
        self, client, admin_headers
    ) -> None:
        epoch = _current_auth_epoch(client)
        with _GeneratorStream(client, cursor=(epoch, 5)) as stream:
            frames = stream.wait_for(
                lambda f: any(name == "reset" for name, _d in f)
            )
        assert frames[0][1]["reason"] == "cursor_too_old"
        assert str(frames[0][1]["cursor"]) == f"{epoch}-0"


class TestAgentStreamClosure:
    def test_binding_revocation_closes_stream_in_band(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))

        with _GeneratorStream(
            client, conversation_id=conversation_id, binding_id=binding_id
        ) as stream:
            _set_binding_status(client, binding_id, "revoked")
            _append_event(client, conversation_id, dedup_key="after-revoke")
            stream.wait_for(lambda f: any(name == "closed" for name, _d in f))
            stream.wait_done()

        frames = stream.parsed_frames()
        name, data = frames[-1]
        assert name == "closed"
        assert data["type"] == "closed"
        assert data["code"] == CLOSE_BINDING_REVOKED
        assert data["reason"] == "binding_revoked"
        # No canonical events are delivered after revocation.
        assert not any(name == "agent_event" for name, _d in frames)

    def test_auth_epoch_change_closes_stream_in_band(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))

        with _GeneratorStream(client, conversation_id=conversation_id) as stream:
            _rotate_auth_epoch(client)
            _append_event(client, conversation_id, dedup_key="after-rotate")
            stream.wait_for(lambda f: any(name == "closed" for name, _d in f))
            stream.wait_done()

        frames = stream.parsed_frames()
        name, data = frames[-1]
        assert name == "closed"
        assert data["code"] == CLOSE_AUTH_EPOCH
        assert data["reason"] == "authentication_epoch_changed"
        assert not any(name == "agent_event" for name, _d in frames)

    def test_hub_closed_subscriber_emits_in_band_closed_frame(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))

        with _GeneratorStream(
            client, conversation_id=conversation_id, binding_id=binding_id
        ) as stream:
            # The hub closure path (queue overflow / close_for_binding /
            # synchronize_epoch) surfaces as an in-band closed frame.
            closed = client.portal.call(
                stream.hub.close_for_binding, binding_id
            )
            assert closed == 1
            _append_event(client, conversation_id, dedup_key="after-close")
            stream.wait_for(lambda f: any(name == "closed" for name, _d in f))
            stream.wait_done()

        frames = stream.parsed_frames()
        assert frames[-1][0] == "closed"
        assert frames[-1][1]["code"] == CLOSE_BINDING_REVOKED
        assert frames[-1][1]["reason"] == "binding_revoked"


class TestAgentStreamAGUI:
    """wire=agui projection over the same SSE stream (plan M6a spec §4.4, §8).

    The frame contract is unchanged: ``event: agent_event`` with
    ``{"type": "event", "event": <AG-UI event>, "cursor": "{epoch}-{seq}"}``;
    the AG-UI event object itself never carries B cursor/seq/digest fields.
    """

    def test_unknown_wire_value_is_rejected(self, client, admin_headers) -> None:
        response = client.get(
            "/api/v1/agent/stream",
            headers=admin_headers,
            params={"wire": "bogus"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_wire"

    def test_agui_happy_path_projects_canonical_event(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        epoch = _current_auth_epoch(client)
        message_id = uuid4()

        with _GeneratorStream(
            client, conversation_id=conversation_id, wire="agui"
        ) as stream:
            seq = _append_event(
                client,
                conversation_id,
                kind="message_delta",
                dedup_key="agui-live-1",
                payload=_message_delta_payload(message_id),
            )
            frames = stream.wait_for(
                lambda f: any(name == "agent_event" for name, _d in f)
            )

        assert seq == 1
        name, data = frames[0]
        assert name == "agent_event"
        assert data["type"] == "event"
        event = data["event"]
        # The AG-UI event shape: camelCase fields, no B storage identifiers.
        assert set(event) == {"type", "messageId", "role", "delta", "timestamp"}
        assert event["type"] == "TEXT_MESSAGE_CHUNK"
        assert event["messageId"] == str(message_id)
        assert event["role"] == "assistant"
        assert event["delta"] == "hello"
        assert isinstance(event["timestamp"], int)
        # The opaque cursor envelope is unchanged: epoch-seq, never inside the
        # AG-UI event object.
        assert data["cursor"] == f"{epoch}-1"

    def test_agui_replay_from_cursor_preserves_order(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        epoch = _current_auth_epoch(client)
        first_message = uuid4()
        second_message = uuid4()
        _append_event(
            client,
            conversation_id,
            kind="message_delta",
            dedup_key="agui-pre-1",
            payload=_message_delta_payload(first_message, "first"),
        )
        _append_event(
            client,
            conversation_id,
            kind="message_delta",
            dedup_key="agui-pre-2",
            payload=_message_delta_payload(second_message, "second"),
        )

        with _GeneratorStream(
            client, conversation_id=conversation_id, cursor=(epoch, 1), wire="agui"
        ) as stream:
            live_message = uuid4()
            _append_event(
                client,
                conversation_id,
                kind="message_delta",
                dedup_key="agui-live-2",
                payload=_message_delta_payload(live_message, "live"),
            )
            frames = stream.wait_for(
                lambda f: sum(1 for name, _d in f if name == "agent_event") >= 2
            )

        events = [data["event"] for name, data in frames if name == "agent_event"]
        assert [event["messageId"] for event in events] == [
            str(second_message),
            str(live_message),
        ]
        assert [data["cursor"] for name, data in frames] == [f"{epoch}-2", f"{epoch}-3"]

    def test_agui_unprojectable_events_emit_no_frame_but_stream_continues(
        self, client, admin_headers, provision_term
    ) -> None:
        """A payload-less event is dropped (no frame), yet the cursor
        bookkeeping advances past it and later events still arrive."""
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        run_id = _seed_run(client, conversation_id)
        epoch = _current_auth_epoch(client)

        with _GeneratorStream(
            client, conversation_id=conversation_id, wire="agui"
        ) as stream:
            # Unprojectable: no payload stored.
            _append_event(
                client, conversation_id, kind="message_delta", dedup_key="agui-skip-1"
            )
            # Projectable: run_started with a run_id and payload.
            _append_event(
                client,
                conversation_id,
                kind="run_started",
                dedup_key="agui-skip-2",
                run_id=run_id,
                payload=_run_started_payload(),
            )
            frames = stream.wait_for(
                lambda f: any(name == "agent_event" for name, _d in f)
            )

        assert len(frames) == 1
        name, data = frames[0]
        assert name == "agent_event"
        assert data["event"]["type"] == "RUN_STARTED"
        assert data["event"]["threadId"] == str(conversation_id)
        assert data["event"]["runId"] == str(run_id)
        # The cursor advanced past the skipped event (seq 2, not 1).
        assert data["cursor"] == f"{epoch}-2"

    def test_agui_dedup_still_projects_once(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        message_id = uuid4()
        payload = _message_delta_payload(message_id)

        with _GeneratorStream(
            client, conversation_id=conversation_id, wire="agui"
        ) as stream:
            _append_event(
                client, conversation_id, kind="message_delta", dedup_key="agui-same",
                payload=payload,
            )
            _append_event(
                client, conversation_id, kind="message_delta", dedup_key="agui-same",
                payload=payload,
            )
            stream.wait_for(
                lambda f: any(name == "agent_event" for name, _d in f)
            )
            time.sleep(0.3)

        events = [
            data["event"]
            for name, data in stream.parsed_frames()
            if name == "agent_event"
        ]
        assert [event["messageId"] for event in events] == [str(message_id)]

    def test_agui_global_stream_events_are_mergeable_by_thread_id(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        first = UUID(
            str(
                _create_conversation(client, admin_headers, binding_id=binding_id)[
                    "conversation_id"
                ]
            )
        )
        second = UUID(
            str(
                _create_conversation(client, admin_headers, binding_id=binding_id)[
                    "conversation_id"
                ]
            )
        )
        first_run = _seed_run(client, first)
        second_run = _seed_run(client, second)
        epoch = _current_auth_epoch(client)

        with _GeneratorStream(client, wire="agui") as stream:
            _append_event(
                client, first, dedup_key="agui-global-1", run_id=first_run,
                payload=_run_started_payload(),
            )
            _append_event(
                client, second, dedup_key="agui-global-2", run_id=second_run,
                payload=_run_started_payload(),
            )
            frames = stream.wait_for(
                lambda f: sum(1 for name, _d in f if name == "agent_event") >= 2
            )

        events = [data["event"] for name, data in frames if name == "agent_event"]
        assert {event["type"] for event in events} == {"RUN_STARTED"}
        # threadId is the conversation id: clients merge the global stream by it.
        assert {event["threadId"] for event in events} == {str(first), str(second)}
        assert {event["runId"] for event in events} == {str(first_run), str(second_run)}
        assert all(data["cursor"] == f"{epoch}-0" for name, data in frames)

    def test_agui_epoch_mismatched_cursor_returns_reset(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        _append_event(client, conversation_id, dedup_key="agui-reset-pre")
        epoch = _current_auth_epoch(client)

        with _GeneratorStream(
            client, conversation_id=conversation_id, cursor=(epoch + 1, 1), wire="agui"
        ) as stream:
            frames = stream.wait_for(
                lambda f: any(name == "reset" for name, _d in f)
            )

        name, data = frames[0]
        assert name == "reset"
        assert data["type"] == "reset"
        assert data["reason"] == "cursor_too_old"
        assert str(data["cursor"]) == f"{epoch}-1"

    def test_agui_binding_revocation_closes_stream_in_band(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))

        with _GeneratorStream(
            client,
            conversation_id=conversation_id,
            binding_id=binding_id,
            wire="agui",
        ) as stream:
            _set_binding_status(client, binding_id, "revoked")
            _append_event(client, conversation_id, dedup_key="agui-after-revoke")
            stream.wait_for(lambda f: any(name == "closed" for name, _d in f))
            stream.wait_done()

        frames = stream.parsed_frames()
        name, data = frames[-1]
        assert name == "closed"
        assert data["code"] == CLOSE_BINDING_REVOKED
        assert data["reason"] == "binding_revoked"
        assert not any(name == "agent_event" for name, _d in frames)

    def test_agui_hub_closed_subscriber_emits_in_band_closed_frame(
        self, client, admin_headers, provision_term
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))

        with _GeneratorStream(
            client,
            conversation_id=conversation_id,
            binding_id=binding_id,
            wire="agui",
        ) as stream:
            closed = client.portal.call(stream.hub.close_for_binding, binding_id)
            assert closed == 1
            _append_event(client, conversation_id, dedup_key="agui-after-close")
            stream.wait_for(lambda f: any(name == "closed" for name, _d in f))
            stream.wait_done()

        frames = stream.parsed_frames()
        assert frames[-1][0] == "closed"
        assert frames[-1][1]["code"] == CLOSE_BINDING_REVOKED
        assert frames[-1][1]["reason"] == "binding_revoked"

    def test_default_canonical_wire_frames_are_byte_identical(
        self, client, admin_headers, provision_term
    ) -> None:
        """wire defaults to canonical: the canonical SSE frame is byte-for-byte
        the same as before the wire parameter existed (M6a spec §4.4)."""
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(
            client, admin_headers, binding_id=binding_id
        )
        conversation_id = UUID(str(conversation["conversation_id"]))
        epoch = _current_auth_epoch(client)

        with _GeneratorStream(client, conversation_id=conversation_id) as stream:
            _append_event(
                client, conversation_id, kind="message_delta", dedup_key="agui-bytes"
            )
            stream.wait_for(
                lambda f: any(name == "agent_event" for name, _d in f)
            )

        event = client.portal.call(
            client.app.state.repositories.agent_events.list_for_conversation,
            conversation_id,
        )[0]
        expected_envelope = {
            "event_id": str(event.id),
            "conversation_id": str(event.conversation_id),
            "run_id": None,
            "event_kind": event.event_kind,
            "database_seq": event.database_seq,
            "payload_digest": event.payload_digest,
            "ephemeral": event.ephemeral,
            "created_at": event.created_at.isoformat(),
        }
        expected_data = json.dumps(
            {
                "type": "event",
                "event": expected_envelope,
                "cursor": f"{epoch}-1",
            },
            separators=(",", ":"),
        )
        expected = (
            "event: agent_event\n"
            f"data: {expected_data}\n\n"
        )
        assert stream.raw_frames()[0] == expected


class TestAgentStreamHub:
    """Hub-level slow-consumer and revocation-fanout semantics."""

    @staticmethod
    def _event(conversation_id: UUID, seq: int) -> AgentEvent:
        return AgentEvent(
            id=uuid4(),
            conversation_id=conversation_id,
            run_id=None,
            event_kind="message_delta",
            dedup_key=f"k-{seq}",
            database_seq=seq,
            payload_digest="digest",
            ephemeral=False,
        )

    async def test_slow_subscriber_is_closed_on_queue_overflow(self) -> None:
        hub = AgentStreamHub(queue_size=1)
        conversation_id = uuid4()
        subscriber = await hub.subscribe(
            conversation_id=conversation_id,
            binding_id=None,
            auth_epoch=1,
        )
        await hub.publish(self._event(conversation_id, 1))
        dropped = await hub.publish(self._event(conversation_id, 2))
        assert dropped == [subscriber.id]
        assert subscriber.closed.is_set()
        assert subscriber.close_code == CLOSE_TOO_SLOW
        assert await hub.unsubscribe(subscriber) is False

    async def test_publish_honors_conversation_scope(self) -> None:
        hub = AgentStreamHub(queue_size=4)
        first = uuid4()
        second = uuid4()
        scoped = await hub.subscribe(conversation_id=first, binding_id=None, auth_epoch=1)
        global_sub = await hub.subscribe(conversation_id=None, binding_id=None, auth_epoch=1)
        assert await hub.publish(self._event(first, 1)) == []
        assert await hub.publish(self._event(second, 1)) == []
        assert (await scoped.queue.get()).conversation_id == first
        assert scoped.queue.empty()
        # The global subscriber receives every conversation's events in order.
        assert (await global_sub.queue.get()).conversation_id == first
        assert (await global_sub.queue.get()).conversation_id == second
        assert global_sub.queue.empty()

    async def test_close_for_binding_closes_matching_subscribers(self) -> None:
        hub = AgentStreamHub(queue_size=4)
        binding = uuid4()
        other_binding = uuid4()
        revoked = await hub.subscribe(
            conversation_id=None, binding_id=binding, auth_epoch=1
        )
        other = await hub.subscribe(
            conversation_id=None, binding_id=other_binding, auth_epoch=1
        )
        closed_count = await hub.close_for_binding(binding)
        assert closed_count == 1
        assert revoked.closed.is_set()
        assert revoked.close_code == CLOSE_BINDING_REVOKED
        assert not other.closed.is_set()

    async def test_epoch_synchronization_closes_current_and_rejects_stale(
        self,
    ) -> None:
        hub = AgentStreamHub(queue_size=4)
        current = await hub.subscribe(
            conversation_id=None, binding_id=None, auth_epoch=1
        )
        assert await hub.synchronize_epoch(2) == 1
        assert current.closed.is_set()
        assert current.close_code == CLOSE_AUTH_EPOCH
        stale = await hub.subscribe(conversation_id=None, binding_id=None, auth_epoch=1)
        assert stale.closed.is_set()
        assert stale.close_code == CLOSE_AUTH_EPOCH
        replacement = await hub.subscribe(
            conversation_id=None, binding_id=None, auth_epoch=2
        )
        assert not replacement.closed.is_set()
