"""OpenCode SSE event-stream tests (M4.2).

Drives :meth:`OpenCodeAdapter.events` through an ``httpx.MockTransport`` that
streams SSE bodies built from the REAL captured OpenCode spec shapes
(``tests/fixtures/opencode/opencode-openapi.json``): ``GlobalEvent`` envelopes
with ``{directory, payload}`` and ``Event`` payloads with
``{id: ^evt_, type, properties}``.  The pinned SSE mode is ``GET /global/event``
(pin §1) and envelope filtering follows pin §3 (``GlobalEvent.directory`` must
match the adapter's persisted directory).

These tests freeze: the event-type → ``AgentEventKind`` mapping, the bounded
visible payload rules (reasoning/private parts dropped and counted), and pump
resilience (malformed events never break the iterator).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
from termflow_control_plane.plugins.agent_broker.agent.opencode import (
    BACKEND_KIND,
    OpenCodeAdapter,
)
from termflow_control_plane.plugins.agent_broker.agent.runs import (
    AgentRunStateMachine,
    RunBoundary,
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
PROVIDER_ID = "deepseek"
MODEL_ID = "deepseek-v4-flash"
SESSION_ID = "ses_opencode_1"
ASSISTANT_MESSAGE_ID = "msg_asst_1"


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
        provider_id=PROVIDER_ID,
        model_id=MODEL_ID,
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


async def test_pinned_global_event_endpoint_streams_and_survives_bad_frames() -> None:
    # The pinned mode (pin §1) takes no parameters; directory filtering
    # happens on the envelope, not the request.
    body = _sse(
        _envelope(_event("session.idle", {"sessionID": SESSION_ID})),
        _envelope(_event("session.idle", {"sessionID": SESSION_ID})),
    )
    adapter, requests = _adapter(_stream_handler(body))
    notifications = await _collect(adapter, _scope())
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/global/event"
    assert dict(request.url.params) == {}
    assert len(notifications) == 2
    assert notifications[0].kind is AgentEventKind.BACKEND_STATE_CHANGED

    # A non-200 response ends the stream cleanly with a diagnostic.
    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    failed_adapter, _ = _adapter(failed)
    assert await _collect(failed_adapter, _scope()) == []
    assert any("HTTP 500" in record.note for record in failed_adapter.diagnostics)

    # Malformed frames are skipped and the stream continues.
    good = _envelope(_event("session.idle", {"sessionID": SESSION_ID}))
    resilient_adapter, _ = _adapter(_stream_handler(f"data: {good}\n\ndata: {{not json\n\n"))
    resilient = await _collect(resilient_adapter, _scope())
    assert len(resilient) == 1
    assert resilient[0].kind is AgentEventKind.BACKEND_STATE_CHANGED
    assert resilient_adapter.stats.malformed_events == 1


async def test_event_mapping_and_bounded_visibility_freeze() -> None:
    body = _sse(
        _envelope(_event("session.next.step.started", _steps_properties())),
        _envelope(_event("session.next.text.delta", _steps_properties(textID="t", delta="hel"))),
        _envelope(
            _event(
                "session.next.text.ended",
                _steps_properties(textID="t", text="final answer"),
            )
        ),
        _envelope(
            _event(
                "session.next.tool.called",
                _steps_properties(
                    callID="call_1",
                    tool="bash",
                    input={"command": "ls"},
                    provider={"executed": False},
                ),
            )
        ),
        _envelope(
            _event(
                "permission.asked",
                {
                    "id": "per_1",
                    "sessionID": SESSION_ID,
                    "permission": "bash",
                    "patterns": [],
                    "metadata": {},
                    "always": [],
                    "tool": {"messageID": "msg_1", "callID": "call_1"},
                },
            )
        ),
        _envelope(
            _event(
                "message.part.updated",
                {
                    "sessionID": SESSION_ID,
                    "part": {
                        "id": "prt_reason",
                        "sessionID": SESSION_ID,
                        "messageID": ASSISTANT_MESSAGE_ID,
                        "type": "reasoning",
                        "text": "secret chain of thought",
                        "time": {"start": 1},
                    },
                    "time": 1,
                },
            )
        ),
        _envelope(
            _event(
                "session.next.text.ended",
                _steps_properties(textID="text_2", text="x" * 20000),
            )
        ),
    )
    adapter, _ = _adapter(_stream_handler(body))
    notifications = await _collect(adapter, _scope())

    started, delta, completed, tool, permission, bounded = notifications
    assert started.kind is AgentEventKind.RUN_STARTED
    assert started.conversation_ref.provider_ref == SESSION_ID
    assert started.conversation_ref.backend_kind == BACKEND_KIND
    assert started.dedup_key.startswith("evt_")
    assert started.payload == NotificationPayload()

    assert delta.kind is AgentEventKind.MESSAGE_DELTA
    assert delta.payload.text == "hel"
    assert started.run_id == delta.run_id == completed.run_id
    assert started.message_id == delta.message_id == completed.message_id

    assert completed.kind is AgentEventKind.MESSAGE_COMPLETED
    assert completed.payload.text == "final answer"

    assert tool.kind is AgentEventKind.TOOL_STARTED
    assert tool.tool_call_id == "call_1"
    # Raw tool input is never surfaced; only the bounded tool name.
    assert tool.payload.summary == "bash"
    assert tool.payload.text is None

    assert permission.kind is AgentEventKind.PERMISSION_REQUESTED
    assert permission.tool_call_id == "call_1"
    assert permission.payload.summary == "bash"
    assert permission.payload.text is None

    # The reasoning part is dropped and counted, never surfaced.
    assert len(notifications) == 6
    assert adapter.stats.dropped_reasoning_events == 1

    # Bounded text is truncated.
    assert bounded.payload.text is not None
    assert len(bounded.payload.text) <= 8192


async def test_current_opencode_message_events_reconstruct_text_and_boundaries() -> None:
    """Pin the event shapes emitted by the current 1.18.x runtime.

    OpenCode emits ``session.status``/``session.idle`` around
    ``message.part.delta`` and a completed ``message.updated`` record.  The
    adapter must expose the text and inferred run boundaries so B can release
    its serialized inbox after a real turn.
    """
    body = _sse(
        _envelope(
            _event(
                "session.status",
                {"sessionID": SESSION_ID, "status": {"type": "busy"}},
            )
        ),
        _envelope(
            _event(
                "message.part.updated",
                {
                    "sessionID": SESSION_ID,
                    "part": {
                        "id": "prt_text",
                        "messageID": ASSISTANT_MESSAGE_ID,
                        "sessionID": SESSION_ID,
                        "type": "text",
                        "text": "hello",
                    },
                },
            )
        ),
        _envelope(
            _event(
                "message.part.delta",
                {
                    "sessionID": SESSION_ID,
                    "messageID": ASSISTANT_MESSAGE_ID,
                    "partID": "prt_text",
                    "field": "text",
                    "delta": " world",
                },
            )
        ),
        _envelope(
            _event(
                "message.updated",
                {
                    "sessionID": SESSION_ID,
                    "info": {
                        "id": ASSISTANT_MESSAGE_ID,
                        "role": "assistant",
                        "time": {"created": 1, "completed": 2},
                    },
                },
            )
        ),
        _envelope(_event("session.idle", {"sessionID": SESSION_ID})),
    )
    adapter, _ = _adapter(_stream_handler(body))
    notifications = await _collect(adapter, _scope())

    caps = await adapter.capabilities()
    assert not caps.explicit_run_boundaries
    assert AgentRunStateMachine.infer_run_boundary(caps, notifications[0]) is RunBoundary.START
    assert AgentRunStateMachine.infer_run_boundary(caps, notifications[4]) is RunBoundary.END
    assert [item.kind for item in notifications] == [
        AgentEventKind.BACKEND_STATE_CHANGED,
        AgentEventKind.MESSAGE_DELTA,
        AgentEventKind.MESSAGE_DELTA,
        AgentEventKind.MESSAGE_COMPLETED,
        AgentEventKind.BACKEND_STATE_CHANGED,
    ]
    assert notifications[0].payload.summary == "busy"
    assert notifications[1].payload.text == "hello"
    assert notifications[2].payload.text == " world"
    assert notifications[3].payload.text == "hello world"
    assert notifications[4].payload.summary == "idle"


async def test_current_opencode_tool_parts_are_lifecycle_events() -> None:
    """Normalize current tool parts without exposing input or output."""
    tool_part = {
        "id": "prt_tool",
        "messageID": ASSISTANT_MESSAGE_ID,
        "sessionID": SESSION_ID,
        "type": "tool",
        "tool": "termflow_termflow_list_panes",
        "callID": "call_live",
    }
    body = _sse(
        _envelope(_event("session.status", {"sessionID": SESSION_ID, "status": {"type": "busy"}})),
        _envelope(
            _event(
                "message.part.updated",
                {
                    "sessionID": SESSION_ID,
                    "part": {
                        **tool_part,
                        "state": {"status": "pending", "input": {"secret": "x"}},
                    },
                },
            )
        ),
        _envelope(
            _event(
                "message.part.updated",
                {
                    "sessionID": SESSION_ID,
                    "part": {
                        **tool_part,
                        "state": {"status": "running", "input": {"secret": "x"}},
                    },
                },
            )
        ),
        _envelope(
            _event(
                "message.part.updated",
                {
                    "sessionID": SESSION_ID,
                    "part": {
                        **tool_part,
                        "state": {
                            "status": "completed",
                            "input": {"secret": "x"},
                            "output": "sensitive pane data",
                        },
                    },
                },
            )
        ),
        _envelope(
            _event(
                "message.updated",
                {
                    "sessionID": SESSION_ID,
                    "info": {
                        "id": ASSISTANT_MESSAGE_ID,
                        "role": "assistant",
                        "time": {"created": 1, "completed": 2},
                        "finish": "tool-calls",
                    },
                },
            )
        ),
        _envelope(_event("session.idle", {"sessionID": SESSION_ID})),
    )
    adapter, _ = _adapter(_stream_handler(body))
    notifications = await _collect(adapter, _scope())

    assert [item.kind for item in notifications] == [
        AgentEventKind.BACKEND_STATE_CHANGED,
        AgentEventKind.TOOL_STARTED,
        AgentEventKind.TOOL_COMPLETED,
        AgentEventKind.BACKEND_STATE_CHANGED,
    ]
    started, completed = notifications[1:3]
    assert started.tool_call_id == completed.tool_call_id == "call_live"
    assert started.payload.summary == "termflow_termflow_list_panes"
    assert started.payload.text is None
    assert completed.payload.text is None
    assert completed.payload.error_code is None
    assert adapter.stats.unmapped_events == 0
