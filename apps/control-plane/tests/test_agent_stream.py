"""Agent live stream API tests (plan §13.1, task M6.2).

Covers the distinct Agent SSE stream at ``/api/v1/agent/stream``:

- SSE happy path: subscribe, then a canonical event append is received live
  with an opaque ``{epoch}-{seq}`` cursor.
- Replay from an opaque cursor preserves ``database_seq`` order.
- Binding revocation closes the stream with an in-band ``closed`` frame.

The TestClient portal runs the app; the endpoint's SSE generator is driven
directly inside that loop (``client.stream`` cannot read an endless SSE body
because this starlette transport waits for the app task to complete).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from termflow_control_plane.plugins.agent_broker.agent.agui_projection import (
    AgentEventProjector,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import (
    AgentEventCursor,
    AgentStreamHub,
)

#: In-band close code produced by the stream on binding revocation.
CLOSE_BINDING_REVOKED = 4412


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
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
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
    payload_digest: str | None = None,
) -> int:
    """Append a canonical event through the publish hook; returns database_seq."""
    cursor = _event_cursor(client)
    event, _inserted = client.portal.call(
        lambda: cursor.append(
            conversation_id=conversation_id,
            event_kind=kind,
            dedup_key=dedup_key or f"dedup-{uuid4()}",
            payload_digest=payload_digest or "test-digest",
            run_id=None,
            payload_json=None,
        )
    )
    return event.database_seq


def _current_auth_epoch(client: TestClient) -> int:
    return client.portal.call(client.app.state.repositories.auth_state.get).epoch


def _set_binding_status(client: TestClient, binding_id: UUID, status: str) -> None:
    client.portal.call(
        client.app.state.repositories.agent_bindings.set_status,
        binding_id,
        status,
    )


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


def test_happy_path_subscribe_append_receive(client, admin_headers, provision_term) -> None:
    binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
    conversation_id = UUID(str(conversation["conversation_id"]))
    epoch = _current_auth_epoch(client)

    with _GeneratorStream(client, conversation_id=conversation_id) as stream:
        seq = _append_event(client, conversation_id, kind="message_delta", dedup_key="live-1")
        frames = stream.wait_for(lambda f: any(name == "agent_event" for name, _d in f))

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


def test_replay_from_cursor_preserves_order(client, admin_headers, provision_term) -> None:
    binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
    conversation_id = UUID(str(conversation["conversation_id"]))
    epoch = _current_auth_epoch(client)
    for index in (1, 2, 3):
        _append_event(
            client,
            conversation_id,
            kind="message_delta",
            dedup_key=f"pre-{index}",
        )

    with _GeneratorStream(client, conversation_id=conversation_id, cursor=(epoch, 1)) as stream:
        seq_4 = _append_event(client, conversation_id, kind="run_completed", dedup_key="live-4")
        frames = stream.wait_for(lambda f: sum(1 for name, _d in f if name == "agent_event") >= 3)

    events = [data["event"] for name, data in frames if name == "agent_event"]
    assert [event["database_seq"] for event in events] == [2, 3, 4]
    assert [event["event_kind"] for event in events] == [
        "message_delta",
        "message_delta",
        "run_completed",
    ]
    assert seq_4 == 4  # the live append is the fourth event


def test_binding_revocation_closes_stream_in_band(client, admin_headers, provision_term) -> None:
    binding_id = _seed_binding(client, admin_headers, provision_term)
    conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
    conversation_id = UUID(str(conversation["conversation_id"]))

    with _GeneratorStream(client, conversation_id=conversation_id, binding_id=binding_id) as stream:
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
