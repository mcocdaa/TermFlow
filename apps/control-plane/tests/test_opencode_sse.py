"""OpenCode SSE event-stream tests (M4.2).

Drives :meth:`OpenCodeAdapter.events` through an ``httpx.MockTransport`` that
streams SSE bodies built from the REAL captured OpenCode spec shapes
(``tests/fixtures/opencode/opencode-openapi.json``): ``GlobalEvent`` envelopes
with ``{directory, payload}`` and ``Event`` payloads with
``{id: ^evt_, type, properties}``.  The pinned SSE mode is ``GET /global/event``
(pin §1) and envelope filtering follows pin §3 (``GlobalEvent.directory`` must
match the adapter's persisted directory).

These tests freeze: the event-type → ``AgentEventKind`` mapping, the bounded
visible payload rules (reasoning/attachments/private parts dropped and
counted), pump resilience (malformed/unknown events never break the iterator),
scope filtering, dedup keys, and clean cancellation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from termflow_control_plane.plugins.agent_broker.agent.opencode import (
    BACKEND_KIND,
    OpenCodeAdapter,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendEventScope,
    NotificationPayload,
)
from termflow_protocol.agent import AgentEventKind

BASE_URL = "https://opencode.test"
DIRECTORY = "/srv/termflow/workspace-1"
BACKEND_VERSION = "0.1.0"
RUNTIME_ID = "runtime://opencode-1"
CAPABILITY_EPOCH = 3
SESSION_ID = "ses_opencode_1"
ASSISTANT_MESSAGE_ID = "msg_asst_1"

#: Real spec shapes: ``Event`` = {id ^evt_, type, properties} (with
#: ``additionalProperties: false``); ``GlobalEvent`` = {directory, payload}.
_EVENT_TYPES = (
    "session.next.step.started",
    "session.next.step.ended",
    "session.next.step.failed",
    "session.next.text.delta",
    "session.next.text.ended",
    "message.updated",
    "message.part.updated",
    "session.next.tool.called",
    "session.next.tool.success",
    "session.next.tool.failed",
    "permission.asked",
    "permission.v2.asked",
    "session.status",
    "session.idle",
    "session.error",
    "session.next.reasoning.delta",
    "session.created",
)


def _event(
    event_type: str, properties: dict[str, Any], event_id: str | None = None
) -> dict[str, Any]:
    """Build one spec-shaped ``Event`` payload (id/type/properties)."""
    return {
        "id": event_id or f"evt_{uuid4().hex}",
        "type": event_type,
        "properties": properties,
    }


def _envelope(payload: dict[str, Any], directory: str = DIRECTORY) -> str:
    """Wrap an ``Event`` in a spec-shaped ``GlobalEvent`` envelope JSON."""
    return json.dumps({"directory": directory, "payload": payload})


def _sse(*payloads: str) -> str:
    """Render GlobalEvent envelope JSON strings as a framed SSE body."""
    return "".join(f"data: {payload}\n\n" for payload in payloads)


def _steps_properties(**overrides: Any) -> dict[str, Any]:
    """Spec-shaped ``session.next.*`` properties (required fields per spec)."""
    values: dict[str, Any] = {
        "timestamp": 0,
        "sessionID": SESSION_ID,
        "assistantMessageID": ASSISTANT_MESSAGE_ID,
    }
    values.update(overrides)
    return values


def _scope(**overrides: Any) -> BackendEventScope:
    values: dict[str, Any] = {
        "binding_id": "binding-1",
        "runtime_epoch": CAPABILITY_EPOCH,
        "conversation_id": uuid4(),
    }
    values.update(overrides)
    return BackendEventScope(**values)


def _stream_handler(
    body: str, requests: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    return handler


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[OpenCodeAdapter, list[httpx.Request]]:
    """Build an adapter over a recording MockTransport handler."""
    requests: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_record(handler, requests)))
    adapter = OpenCodeAdapter(
        base_url=BASE_URL,
        directory=DIRECTORY,
        backend_version=BACKEND_VERSION,
        client=client,
        runtime_id=RUNTIME_ID,
        binding_capability_epoch=CAPABILITY_EPOCH,
    )
    return adapter, requests


def _adapter_for_event(
    event: dict[str, Any], directory: str = DIRECTORY
) -> tuple[OpenCodeAdapter, list[httpx.Request]]:
    """Build an adapter streaming one GlobalEvent envelope."""
    return _adapter(_stream_handler(_sse(_envelope(event, directory))))


def _record(
    handler: Callable[[httpx.Request], httpx.Response], requests: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return record


async def _collect(adapter: OpenCodeAdapter, scope: BackendEventScope) -> list[Any]:
    """Consume the events iterator into a list of notifications."""
    notifications: list[Any] = []
    async for notification in adapter.events(scope):
        notifications.append(notification)
    return notifications


class TestSseStream:
    async def test_connects_to_pinned_global_event_endpoint(self) -> None:
        body = _sse(_envelope(_event("session.idle", {"sessionID": SESSION_ID})))
        adapter, requests = _adapter(_stream_handler(body))
        notifications = await _collect(adapter, _scope())
        request = requests[0]
        assert request.method == "GET"
        assert request.url.path == "/global/event"
        # The pinned mode (pin §1) takes no parameters; directory filtering
        # happens on the envelope, not the request.
        assert dict(request.url.params) == {}
        assert len(notifications) == 1

    async def test_non_200_response_ends_stream_cleanly_with_diagnostic(self) -> None:
        def failed(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        adapter, _ = _adapter(failed)
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert any("HTTP 500" in record.note for record in adapter.diagnostics)

    async def test_stream_end_ends_iterator_cleanly(self) -> None:
        body = _sse(
            _envelope(_event("session.idle", {"sessionID": SESSION_ID})),
            _envelope(_event("session.idle", {"sessionID": SESSION_ID})),
        )
        adapter, _ = _adapter(_stream_handler(body))
        notifications = await _collect(adapter, _scope())
        assert len(notifications) == 2


class TestEventMapping:
    async def test_run_started_from_step_started(self) -> None:
        event = _event("session.next.step.started", _steps_properties())
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.RUN_STARTED
        assert notification.conversation_ref.provider_ref == SESSION_ID
        assert notification.dedup_key == event["id"]
        assert notification.run_id is not None
        assert notification.message_id is not None
        assert notification.payload == NotificationPayload()

    async def test_run_completed_from_step_ended(self) -> None:
        properties = _steps_properties(
            finish="done",
            cost=0.1,
            tokens={"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        )
        adapter, _ = _adapter_for_event(_event("session.next.step.ended", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.RUN_COMPLETED
        # Usage/cost is provider-private in this milestone; never surfaced.
        assert notification.payload == NotificationPayload()

    async def test_run_failed_from_step_failed(self) -> None:
        properties = _steps_properties(error={"type": "unknown", "message": "step blew up"})
        adapter, _ = _adapter_for_event(_event("session.next.step.failed", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.RUN_FAILED
        assert notification.payload.error_code == "unknown"
        assert notification.payload.error_message == "step blew up"

    async def test_message_delta_from_text_delta(self) -> None:
        properties = _steps_properties(textID="text_1", delta="hel")
        adapter, _ = _adapter_for_event(_event("session.next.text.delta", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.MESSAGE_DELTA
        assert notification.message_id is not None
        assert notification.run_id is not None
        assert notification.payload.text == "hel"

    async def test_message_delta_from_text_part_update(self) -> None:
        part = {
            "id": "prt_1",
            "sessionID": SESSION_ID,
            "messageID": ASSISTANT_MESSAGE_ID,
            "type": "text",
            "text": "hi there",
            "time": {"start": 1},
        }
        event = _event("message.part.updated", {"sessionID": SESSION_ID, "part": part, "time": 1})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.MESSAGE_DELTA
        assert notification.part_id == "prt_1"
        assert notification.message_id is not None
        assert notification.payload.text == "hi there"

    async def test_message_completed_from_text_ended(self) -> None:
        properties = _steps_properties(textID="text_1", text="final answer")
        adapter, _ = _adapter_for_event(_event("session.next.text.ended", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.MESSAGE_COMPLETED
        assert notification.payload.text == "final answer"

    async def test_message_completed_from_updated_assistant_message(self) -> None:
        info = {
            "id": ASSISTANT_MESSAGE_ID,
            "sessionID": SESSION_ID,
            "role": "assistant",
            "time": {"created": 1, "completed": 2},
        }
        event = _event("message.updated", {"sessionID": SESSION_ID, "info": info})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.MESSAGE_COMPLETED
        assert notification.message_id is not None

    async def test_updated_message_without_completion_is_delta(self) -> None:
        info = {
            "id": ASSISTANT_MESSAGE_ID,
            "sessionID": SESSION_ID,
            "role": "assistant",
            "time": {"created": 1},
        }
        event = _event("message.updated", {"sessionID": SESSION_ID, "info": info})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.MESSAGE_DELTA
        assert notification.payload.text is None

    async def test_tool_started_from_tool_called(self) -> None:
        properties = _steps_properties(
            callID="call_1",
            tool="bash",
            input={"command": "ls"},
            provider={"executed": False},
        )
        adapter, _ = _adapter_for_event(_event("session.next.tool.called", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.TOOL_STARTED
        assert notification.tool_call_id == "call_1"
        # Raw tool input is never surfaced; only the bounded tool name.
        assert notification.payload.summary == "bash"
        assert notification.payload.text is None

    async def test_tool_completed_from_tool_success(self) -> None:
        properties = _steps_properties(
            callID="call_1",
            structured={},
            content=[],
            outputPaths=["/tmp/out"],
            provider={"executed": True},
        )
        adapter, _ = _adapter_for_event(_event("session.next.tool.success", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.TOOL_COMPLETED
        assert notification.tool_call_id == "call_1"
        # Raw tool results/content are dropped in this milestone.
        assert notification.payload == NotificationPayload()

    async def test_tool_completed_from_tool_failed(self) -> None:
        properties = _steps_properties(
            callID="call_1",
            error={"type": "unknown", "message": "tool broke"},
            provider={"executed": True},
        )
        adapter, _ = _adapter_for_event(_event("session.next.tool.failed", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.TOOL_COMPLETED
        assert notification.tool_call_id == "call_1"
        assert notification.payload.error_code == "unknown"
        assert notification.payload.error_message == "tool broke"

    async def test_permission_requested_from_permission_asked(self) -> None:
        properties = {
            "id": "per_1",
            "sessionID": SESSION_ID,
            "permission": "bash",
            "patterns": [],
            "metadata": {},
            "always": [],
            "tool": {"messageID": "msg_1", "callID": "call_1"},
        }
        adapter, _ = _adapter_for_event(_event("permission.asked", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.PERMISSION_REQUESTED
        assert notification.tool_call_id == "call_1"
        assert notification.payload.summary == "bash"

    async def test_permission_requested_from_permission_v2_asked(self) -> None:
        properties = {
            "id": "per_2",
            "sessionID": SESSION_ID,
            "action": "read",
            "resources": ["/srv/termflow/workspace-1/README.md"],
            "save": [],
        }
        adapter, _ = _adapter_for_event(_event("permission.v2.asked", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.PERMISSION_REQUESTED
        assert notification.payload.summary == "read"
        # Provider-private metadata/resources never leak.
        assert notification.payload.text is None

    async def test_backend_state_changed_from_session_status(self) -> None:
        # Spec-shaped SessionStatus: an object discriminated by ``type``.
        event = _event("session.status", {"sessionID": SESSION_ID, "status": {"type": "busy"}})
        adapter, _ = _adapter_for_event(event)
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.BACKEND_STATE_CHANGED
        assert notification.payload.summary == "busy"

    async def test_backend_state_changed_from_retry_status(self) -> None:
        event = _event(
            "session.status",
            {
                "sessionID": SESSION_ID,
                "status": {"type": "retry", "attempt": 2, "message": "x", "next": 5},
            },
        )
        adapter, _ = _adapter_for_event(event)
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.BACKEND_STATE_CHANGED
        assert notification.payload.summary == "retry attempt 2"

    async def test_backend_state_changed_from_string_status(self) -> None:
        # Backward compatibility: string statuses seen in the wild.
        event = _event("session.status", {"sessionID": SESSION_ID, "status": "busy"})
        adapter, _ = _adapter_for_event(event)
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.BACKEND_STATE_CHANGED
        assert notification.payload.summary == "busy"

    async def test_backend_state_changed_from_session_idle(self) -> None:
        event = _event("session.idle", {"sessionID": SESSION_ID})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        (notification,) = await _collect(adapter, _scope())
        assert notification.kind is AgentEventKind.BACKEND_STATE_CHANGED
        assert notification.payload.summary == "idle"

    async def test_bounded_text_is_truncated(self) -> None:
        huge = "x" * 20000
        properties = _steps_properties(textID="text_1", text=huge)
        adapter, _ = _adapter_for_event(_event("session.next.text.ended", properties))
        (notification,) = await _collect(adapter, _scope())
        assert notification.payload.text is not None
        assert len(notification.payload.text) <= 8192

    async def test_stable_ids_for_same_backend_message(self) -> None:
        delta = _steps_properties(textID="text_1", delta="a")
        ended = _steps_properties(textID="text_1", text="ab")
        body = _sse(
            _envelope(_event("session.next.text.delta", delta, event_id="evt_delta_1")),
            _envelope(_event("session.next.text.ended", ended, event_id="evt_ended_1")),
        )
        adapter, _ = _adapter(_stream_handler(body))
        first, second = await _collect(adapter, _scope())
        # The same backend assistant message maps to the same run/message UUIDs.
        assert first.run_id == second.run_id
        assert first.message_id == second.message_id

    async def test_notification_carries_subscription_scope(self) -> None:
        scope = _scope(conversation_id=uuid4())
        event = _event("session.idle", {"sessionID": SESSION_ID})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        (notification,) = await _collect(adapter, scope)
        assert notification.scope == scope
        assert notification.scope.binding_id == "binding-1"
        assert notification.scope.runtime_epoch == CAPABILITY_EPOCH
        assert notification.conversation_ref.backend_kind == BACKEND_KIND


class TestDroppedContent:
    async def test_reasoning_part_dropped_and_counted(self) -> None:
        part = {
            "id": "prt_reason",
            "sessionID": SESSION_ID,
            "messageID": ASSISTANT_MESSAGE_ID,
            "type": "reasoning",
            "text": "secret chain of thought",
            "time": {"start": 1},
        }
        event = _event("message.part.updated", {"sessionID": SESSION_ID, "part": part, "time": 1})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert adapter.stats.dropped_reasoning_events == 1

    async def test_reasoning_delta_events_dropped_and_counted(self) -> None:
        properties = _steps_properties(reasoningID="reason_1", delta="secret")
        adapter, _ = _adapter(
            _stream_handler(_sse(_envelope(_event("session.next.reasoning.delta", properties))))
        )
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert adapter.stats.dropped_reasoning_events == 1

    async def test_attachment_part_dropped_and_counted(self) -> None:
        part = {
            "id": "prt_file",
            "sessionID": SESSION_ID,
            "messageID": ASSISTANT_MESSAGE_ID,
            "type": "file",
            "mime": "image/png",
            "url": "file:///srv/secret.png",
        }
        event = _event("message.part.updated", {"sessionID": SESSION_ID, "part": part, "time": 1})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert adapter.stats.dropped_attachment_events == 1

    async def test_private_part_dropped_and_counted(self) -> None:
        part = {
            "id": "prt_tool",
            "sessionID": SESSION_ID,
            "messageID": ASSISTANT_MESSAGE_ID,
            "type": "tool",
            "callID": "call_1",
            "tool": "bash",
            "state": "completed",
        }
        event = _event("message.part.updated", {"sessionID": SESSION_ID, "part": part, "time": 1})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert adapter.stats.dropped_private_events == 1


class TestPumpResilience:
    async def test_malformed_json_skipped_and_stream_continues(self) -> None:
        good = _envelope(_event("session.idle", {"sessionID": SESSION_ID}))
        body = f"data: {good}\n\ndata: {{not json\n\n"
        adapter, _ = _adapter(_stream_handler(body))
        notifications = await _collect(adapter, _scope())
        assert len(notifications) == 1
        assert notifications[0].kind is AgentEventKind.BACKEND_STATE_CHANGED
        assert adapter.stats.malformed_events == 1

    async def test_unknown_event_type_skipped_and_stream_continues(self) -> None:
        unknown = _envelope(_event("session.next.shell.started", _steps_properties()))
        idle = _envelope(_event("session.idle", {"sessionID": SESSION_ID}))
        adapter, _ = _adapter(_stream_handler(_sse(unknown, idle)))
        notifications = await _collect(adapter, _scope())
        assert len(notifications) == 1
        assert notifications[0].kind is AgentEventKind.BACKEND_STATE_CHANGED
        assert adapter.stats.unmapped_events == 1
        # The diagnostic record carries only identifiers, never payload content.
        (record,) = adapter.diagnostics
        assert record.event_type == "session.next.shell.started"
        assert record.event_id.startswith("evt_")
        assert record.session_id == SESSION_ID

    async def test_non_object_envelope_skipped(self) -> None:
        adapter, _ = _adapter(_stream_handler("data: [1, 2, 3]\n\n"))
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert adapter.stats.malformed_events == 1

    async def test_missing_session_id_skipped(self) -> None:
        event = _event("session.idle", {})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert adapter.stats.malformed_events == 1

    async def test_overlong_event_id_skipped_never_truncated(self) -> None:
        # A clipped dedup key would collide across events; the event must be
        # skipped instead.
        event = _event("session.idle", {"sessionID": SESSION_ID}, event_id="evt_" + "x" * 300)
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert adapter.stats.malformed_events == 1

    async def test_diagnostics_record_is_size_bounded(self) -> None:
        unmapped = _envelope(_event("session.created", {"sessionID": SESSION_ID}))
        events = [unmapped for _ in range(40)]
        adapter, _ = _adapter(_stream_handler(_sse(*events)))
        await _collect(adapter, _scope())
        assert len(adapter.diagnostics) <= 32


class TestScopeFiltering:
    async def test_directory_mismatch_not_emitted(self) -> None:
        event = _event("session.idle", {"sessionID": SESSION_ID})
        adapter, _ = _adapter(
            _stream_handler(_sse(_envelope(event, directory="/srv/other-workspace")))
        )
        notifications = await _collect(adapter, _scope())
        assert notifications == []
        assert adapter.stats.scope_mismatch_events == 1
        # The bounded diagnostic never embeds the mismatched provider path
        # (plan §4.3): only identifiers and an adapter-generated reason.
        (record,) = adapter.diagnostics
        assert record.event_type == "<scope>"
        assert "/srv/other-workspace" not in record.note

    async def test_matching_directory_emitted(self) -> None:
        event = _event("session.idle", {"sessionID": SESSION_ID})
        adapter, _ = _adapter(_stream_handler(_sse(_envelope(event))))
        notifications = await _collect(adapter, _scope())
        assert len(notifications) == 1
        assert adapter.stats.scope_mismatch_events == 0


class TestDedup:
    async def test_same_backend_event_id_yields_same_dedup_key(self) -> None:
        event = _event("session.next.text.delta", _steps_properties(textID="t", delta="x"))
        body = _sse(_envelope(event), _envelope(event))
        adapter, _ = _adapter(_stream_handler(body))
        first, second = await _collect(adapter, _scope())
        assert first.dedup_key == second.dedup_key == event["id"]
        assert first.notification_id != second.notification_id

    async def test_distinct_event_ids_yield_distinct_dedup_keys(self) -> None:
        first = _envelope(_event("session.idle", {"sessionID": SESSION_ID}, event_id="evt_a"))
        second = _envelope(_event("session.idle", {"sessionID": SESSION_ID}, event_id="evt_b"))
        adapter, _ = _adapter(_stream_handler(_sse(first, second)))
        first_n, second_n = await _collect(adapter, _scope())
        assert first_n.dedup_key == "evt_a"
        assert second_n.dedup_key == "evt_b"


class TestCancellation:
    async def test_cancel_closes_stream_cleanly(self) -> None:
        closed = asyncio.Event()

        async def body() -> AsyncIterator[bytes]:
            try:
                payload = _envelope(_event("session.idle", {"sessionID": SESSION_ID}))
                yield f"data: {payload}\n\n".encode()
                await asyncio.sleep(3600)
            finally:
                closed.set()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=body(),
                headers={"content-type": "text/event-stream"},
            )

        adapter, _ = _adapter(handler)

        async def consume() -> list[Any]:
            return [n async for n in adapter.events(_scope())]

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        # The adapter stays usable and its counters remain readable.
        assert adapter.stats.malformed_events == 0
