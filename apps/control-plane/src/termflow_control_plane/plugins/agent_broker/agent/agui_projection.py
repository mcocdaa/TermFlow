"""AG-UI 0.1.19 wire projection for canonical Agent events (plan M6a spec §4.3).

This module is the C-facing wire projection layer: it maps B's canonical
:class:`~termflow_control_plane.persistence.models.AgentEvent` kinds (plus the
bounded canonical payload JSON persisted on ``agent_events.payload``) to
AG-UI 0.1.19 wire event objects, hand-written against the pinned wire facts
(uppercase snake discriminators, camelCase field names, millisecond
``timestamp``) and pinned byte-exactly by the local fixture
``tests/fixtures/agui/agui-0.1.19-wire.json``.

Projection is **stateless and per-event**: a B event projects to at most one
AG-UI event (message text streams use the ``TEXT_MESSAGE_CHUNK`` convenience
event so a paginated REST replay never needs cross-event state to rebuild
START/CONTENT/END triples).  Unprojectable events are explicitly dropped with
a diagnostic counter (:class:`ProjectionDropCounts`) instead of inventing
AG-UI shapes; B's canonical events, opaque cursors, auth, retention, and
approval semantics remain the source of truth and are never leaked into the
AG-UI event objects (spec §4.3 "不泄露不变量").

``MAX_AGENT_EVENT_PAYLOAD_BYTES`` (defined in ``persistence.models`` next to
the ``AgentEvent.payload`` column) is the bounded-payload storage limit that
:class:`~termflow_control_plane.persistence.repositories.AgentEventRepository`
enforces on ``append(payload_json=...)`` (spec §4.2).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import (
    AgentEvent,
)

#: The pinned AG-UI protocol version this projection targets (spec §3).
AGUI_PROTOCOL_VERSION = "0.1.19"
#: RFC 6902 patch path used by ``STATE_DELTA`` (spec §4.3).
AGUI_STATE_PATH = "/backend"
#: ``CUSTOM`` event name for permission-request visibility (spec §4.3).
AGUI_PERMISSION_CUSTOM = "termflow.permission_requested"

#: Wire formats accepted by the Agent stream and REST replay endpoints
#: (spec §4.4/§4.5): canonical (default, byte-for-byte pre-M6a behaviour)
#: and agui (projected through :class:`AgentEventProjector`).
WIRE_CANONICAL = "canonical"
WIRE_AGUI = "agui"
_KNOWN_WIRES = frozenset({WIRE_CANONICAL, WIRE_AGUI})

_STABLE_ERROR_CODES = frozenset(
    {
        "provider_auth_failed",
        "provider_model_rejected",
        "run_failed",
        "tool_failed",
    }
)


def _safe_error_code(payload: dict[str, object], *, kind: str) -> str | None:
    """Collapse legacy/provider-controlled error fields to stable codes.

    Older rows can contain an adapter error string.  Replay must remain safe
    even for those rows, so neither ``error_message`` nor an arbitrary code is
    copied onto the C-facing AG-UI wire.
    """

    raw_code = payload.get("error_code")
    if raw_code in _STABLE_ERROR_CODES:
        return str(raw_code)
    if payload.get("error_code") is not None or payload.get("error_message") is not None:
        return "tool_failed" if kind == "tool_completed" else "run_failed"
    return None


def validate_wire(wire: str) -> None:
    """Fail closed on unknown wire values (spec §5: 400 ``invalid_wire``)."""
    if wire not in _KNOWN_WIRES:
        raise TermFlowError(
            "invalid_wire", 400, "The Agent stream wire format is invalid."
        )


class ProjectionDropCounts(NamedTuple):
    """Diagnostic counters for explicitly dropped projections (spec §4.6).

    An unprojectable B event never breaks the stream: it is dropped with the
    relevant counter incremented and a bounded warning log (no payload
    content is ever logged).
    """

    unknown_kind: int = 0
    empty_delta: int = 0
    missing_payload: int = 0
    malformed_payload: int = 0
    run_without_run_id: int = 0


def _epoch_ms(created_at: datetime) -> int:
    # SQLite round-trips DateTime(timezone=True) values as naive datetimes:
    # interpret them as UTC so the wire timestamp is host-timezone agnostic
    # (same pattern as api/dashboard.py `_as_utc`).
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return int(created_at.timestamp() * 1000)


def _required(payload: dict[str, object], key: str) -> object:
    if key not in payload:
        raise ValueError(f"payload missing required field: {key!r}")
    return payload[key]


def _project_run_boundary(
    event: AgentEvent, payload: dict[str, object] | None, wire_type: str
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    if event.run_id is None:
        return [], ProjectionDropCounts(run_without_run_id=1)
    return [
        {
            "type": wire_type,
            "threadId": str(event.conversation_id),
            "runId": str(event.run_id),
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


def _project_run_started(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    return _project_run_boundary(event, payload, "RUN_STARTED")


def _project_run_completed(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    return _project_run_boundary(event, payload, "RUN_FINISHED")


def _project_thinking_delta(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    """Model reasoning rides a CUSTOM frame so clients can collapse it."""
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    text = payload.get("text")
    if not text:
        return [], ProjectionDropCounts(empty_delta=1)
    message_id = str(_required(payload, "message_id"))
    return [
        {
            "type": "CUSTOM",
            "name": "termflow.thinking_delta",
            "value": {"id": message_id, "delta": text},
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


def _project_thinking_completed(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    message_id = str(_required(payload, "message_id"))
    return [
        {
            "type": "CUSTOM",
            "name": "termflow.thinking_completed",
            "value": {"id": message_id},
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


def _project_message_delta(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    text = payload.get("text")
    if not text:
        return [], ProjectionDropCounts(empty_delta=1)
    message_id = str(_required(payload, "message_id"))
    return [
        {
            "type": "TEXT_MESSAGE_CHUNK",
            "messageId": message_id,
            "role": "assistant",
            "delta": text,
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


def _project_message_completed(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    message_id = str(_required(payload, "message_id"))
    return [
        {
            "type": "TEXT_MESSAGE_END",
            "messageId": message_id,
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


def _project_tool_started(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    tool_call_id = str(_required(payload, "tool_call_id"))
    # The canonical serializer writes ``tool_name`` when the adapter surfaced
    # the tool name; pre-fix payloads (and adapters that sent none) degrade
    # to the tool call id instead of dropping the whole event.
    tool_name = payload.get("tool_name") or tool_call_id
    return [
        {
            "type": "TOOL_CALL_START",
            "toolCallId": tool_call_id,
            "toolCallName": tool_name,
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


def _project_tool_completed(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    tool_call_id = str(_required(payload, "tool_call_id"))
    # Bounded summary only: B does not store raw tool results (spec §4.3);
    # hashes stay in B.  ``status`` is written by the canonical serializer;
    # pre-fix payloads derive it from the error fields the same way.  Byte
    # counts are only projected when the canonical payload recorded them
    # (they are not observable at the adapter boundary).
    if "status" not in payload:
        status = (
            "error"
            if payload.get("error_code") or payload.get("error_message")
            else "success"
        )
    else:
        status = str(payload["status"])
    summary: dict[str, object] = {
        "status": status,
        "truncated": bool(payload.get("truncated", False)),
    }
    for key in ("input_bytes", "output_bytes"):
        value = payload.get(key)
        if isinstance(value, int):
            summary[key] = value
    error_code = _safe_error_code(payload, kind="tool_completed")
    if error_code is not None:
        summary["error_code"] = error_code
    content = json.dumps(summary, separators=(",", ":"))
    return [
        {
            "type": "TOOL_CALL_RESULT",
            # messageId is synthesized from tool_call_id: B canonical has no
            # message<->tool association (documented; C correlates by
            # toolCallId, spec §4.3).
            "messageId": tool_call_id,
            "toolCallId": tool_call_id,
            "role": "tool",
            "content": content,
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


def _project_permission_requested(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    # The opaque backend permission id has no neutral carrier in the
    # canonical payload (adapter-internal, plan §6); the tool call id is the
    # correlation key C can join on, and ``approval_request_id`` appears when
    # a later milestone writes one.
    if payload.get("approval_request_id") is not None:
        value: dict[str, object] = {
            "approval_request_id": str(payload["approval_request_id"])
        }
    elif payload.get("tool_call_id") is not None:
        value = {"tool_call_id": str(payload["tool_call_id"])}
    else:
        raise ValueError(
            "permission_requested payload requires approval_request_id or tool_call_id"
        )
    for optional in ("tool_name", "evidence", "expires_at"):
        item = payload.get(optional)
        if item is not None:
            value[optional] = item
    return [
        {
            "type": "CUSTOM",
            "name": AGUI_PERMISSION_CUSTOM,
            "value": value,
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


def _project_run_failed(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    error_code = _safe_error_code(payload, kind="run_failed")
    if error_code is None:
        raise ValueError("run_failed payload requires error_code or error_message")
    message = {
        "provider_auth_failed": "Provider authentication failed.",
        "provider_model_rejected": "Provider model rejected.",
        "run_failed": "Agent run failed.",
    }.get(error_code, "Agent run failed.")
    out: dict[str, object] = {
        "type": "RUN_ERROR",
        "message": message,
    }
    if error_code is not None:
        out["code"] = error_code
    out["timestamp"] = _epoch_ms(event.created_at)
    return [out], ProjectionDropCounts()


def _project_backend_state_changed(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    if payload is None:
        return [], ProjectionDropCounts(missing_payload=1)
    # The canonical serializer writes ``state`` (the adapter's bounded state
    # summary) and ``epoch`` (the binding/runtime epoch); pre-fix payloads
    # degrade to an explicit unknown state instead of dropping the event.
    state = payload.get("state") or "unknown"
    epoch_value = payload.get("epoch")
    epoch = epoch_value if isinstance(epoch_value, int) else 0
    return [
        {
            "type": "STATE_DELTA",
            "delta": [
                {
                    "op": "replace",
                    "path": AGUI_STATE_PATH,
                    "value": {"state": state, "epoch": epoch},
                }
            ],
            "timestamp": _epoch_ms(event.created_at),
        }
    ], ProjectionDropCounts()


_Projector = Callable[
    [AgentEvent, dict[str, object] | None],
    tuple[list[dict[str, object]], ProjectionDropCounts],
]

_PROJECTORS: dict[str, _Projector] = {
    "run_started": _project_run_started,
    "message_delta": _project_message_delta,
    "message_completed": _project_message_completed,
    "thinking_delta": _project_thinking_delta,
    "thinking_completed": _project_thinking_completed,
    "tool_started": _project_tool_started,
    "tool_completed": _project_tool_completed,
    "permission_requested": _project_permission_requested,
    "run_completed": _project_run_completed,
    "run_failed": _project_run_failed,
    "backend_state_changed": _project_backend_state_changed,
}


def project_agent_event(
    event: AgentEvent, payload: dict[str, object] | None
) -> tuple[list[dict[str, object]], ProjectionDropCounts]:
    """Stateless, pure mapping of one canonical B event to AG-UI wire events.

    Returns zero or more AG-UI event dicts (at most one per B event) plus the
    drop counters for explicitly discarded events.  Unknown kinds fail open
    for the canonical stream: they are dropped and counted, never mapped to
    invented AG-UI shapes (spec §4.3).  A structurally broken payload raises
    ``ValueError``/``KeyError``; the endpoint's :class:`AgentEventProjector`
    turns that into a ``malformed_payload`` drop instead of a broken stream.
    """
    handler = _PROJECTORS.get(event.event_kind)
    if handler is None:
        return [], ProjectionDropCounts(unknown_kind=1)
    return handler(event, payload)


class AgentEventProjector:
    """Per-connection thin wrapper: drop counters plus payload parsing.

    Holds the connection-scoped drop counters (spec §4.6) and delegates to
    the stateless :func:`project_agent_event`; it introduces no cross-event
    state, so a replay path can build a fresh instance per request.
    """

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._drops = ProjectionDropCounts()

    @property
    def drops(self) -> ProjectionDropCounts:
        return self._drops

    def project(self, event: AgentEvent) -> list[dict[str, object]]:
        """Project one ORM event (parsing its raw payload), dropping silently."""
        if event.payload is None:
            return self._delegate(event, None)
        try:
            payload = json.loads(event.payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._record_drop("malformed_payload", event, str(exc))
            return []
        if not isinstance(payload, dict):
            self._record_drop("malformed_payload", event, "payload is not a JSON object")
            return []
        return self._delegate(event, payload)

    def _delegate(
        self, event: AgentEvent, payload: dict[str, object] | None
    ) -> list[dict[str, object]]:
        try:
            projected, drops = project_agent_event(event, payload)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            self._record_drop("malformed_payload", event, str(exc))
            return []
        for field in ProjectionDropCounts._fields:
            delta = getattr(drops, field)
            if delta:
                self._record_drop(field, event, f"count={delta}")
        return projected

    def _record_drop(self, field: str, event: AgentEvent, detail: str) -> None:
        self._drops = self._drops._replace(**{field: getattr(self._drops, field) + 1})
        # Bounded diagnostics: never log payload content (spec §4.6).
        self._logger.warning(
            "AG-UI projection dropped %s (kind=%s, conversation=%s, seq=%s): %s",
            field,
            event.event_kind,
            event.conversation_id,
            event.database_seq,
            detail,
        )
