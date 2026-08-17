"""AG-UI projection tests against canonical pipeline payloads (review fix).

The canonical serializer (``pipeline._canonical_payload_json``) writes the
kind-aware keys the projector reads (``tool_name``/``status``/``state``/
``epoch``) and omits data that is not observable at the adapter boundary
(byte counts, hashes, truncation, the opaque backend permission id).  These
tests pin that the projector maps those payloads to AG-UI wire events instead
of dropping them as ``malformed_payload``, and that legacy/pre-fix payloads
degrade gracefully without breaking the stream.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from termflow_control_plane.persistence.models import AgentEvent
from termflow_control_plane.plugins.agent_broker.agent.agui_projection import (
    AGUI_PERMISSION_CUSTOM,
    AGUI_STATE_PATH,
    AgentEventProjector,
    ProjectionDropCounts,
    project_agent_event,
)

CONVERSATION_ID = UUID("11111111-1111-1111-1111-111111111111")
RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
MESSAGE_ID = UUID("33333333-3333-3333-3333-333333333333")
APPROVAL_REQUEST_ID = UUID("44444444-4444-4444-4444-444444444444")
CREATED_AT = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


def _event(
    kind: str,
    *,
    run_id: UUID | None = RUN_ID,
    payload: str | None = None,
) -> AgentEvent:
    return AgentEvent(
        id=uuid4(),
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        event_kind=kind,
        dedup_key=f"k-{kind}",
        database_seq=1,
        payload_digest="d" * 64,
        payload=payload,
        ephemeral=False,
        created_at=CREATED_AT,
    )


def _payload_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Tool activity (review fix: the canonical serializer writes tool_name/status).
# ---------------------------------------------------------------------------


def test_tool_started_projects_with_canonical_tool_name() -> None:
    payload = {"tool_call_id": "call-1", "tool_name": "termflow_pane_read"}
    projected, drops = project_agent_event(_event("tool_started"), payload)
    assert drops == ProjectionDropCounts()
    assert projected == [
        {
            "type": "TOOL_CALL_START",
            "toolCallId": "call-1",
            "toolCallName": "termflow_pane_read",
            "timestamp": int(CREATED_AT.timestamp() * 1000),
        }
    ]


def test_tool_started_degrades_to_call_id_without_tool_name() -> None:
    # Legacy/pre-fix payload: no tool_name key.  The event must project
    # instead of being dropped as malformed.
    payload = {"tool_call_id": "call-9"}
    projected, drops = project_agent_event(_event("tool_started"), payload)
    assert drops == ProjectionDropCounts()
    assert projected[0]["type"] == "TOOL_CALL_START"
    assert projected[0]["toolCallName"] == "call-9"


def test_tool_completed_projects_status_without_byte_counts() -> None:
    payload = {"tool_call_id": "call-1", "status": "success"}
    projected, drops = project_agent_event(_event("tool_completed"), payload)
    assert drops == ProjectionDropCounts()
    assert projected[0]["type"] == "TOOL_CALL_RESULT"
    content = json.loads(projected[0]["content"])
    assert content == {"status": "success", "truncated": False}


def test_tool_completed_derives_status_from_error_for_legacy_payload() -> None:
    payload = {
        "tool_call_id": "call-2",
        "error_code": "E_TOOL",
        "error_message": "boom",
    }
    projected, drops = project_agent_event(_event("tool_completed"), payload)
    assert drops == ProjectionDropCounts()
    content = json.loads(projected[0]["content"])
    assert content["status"] == "error"
    assert content["error_code"] == "E_TOOL"


def test_tool_completed_legacy_success_derives_status() -> None:
    payload = {"tool_call_id": "call-3"}
    projected, drops = project_agent_event(_event("tool_completed"), payload)
    assert drops == ProjectionDropCounts()
    content = json.loads(projected[0]["content"])
    assert content["status"] == "success"


def test_tool_completed_projects_byte_counts_when_recorded() -> None:
    payload = {
        "tool_call_id": "call-4",
        "status": "success",
        "input_bytes": 10,
        "output_bytes": 512,
        "truncated": True,
    }
    projected, drops = project_agent_event(_event("tool_completed"), payload)
    assert drops == ProjectionDropCounts()
    content = json.loads(projected[0]["content"])
    assert content["input_bytes"] == 10
    assert content["output_bytes"] == 512
    assert content["truncated"] is True


# ---------------------------------------------------------------------------
# Permission requests (review fix: the opaque backend permission id has no
# neutral carrier; the tool call id is the correlation key).
# ---------------------------------------------------------------------------


def test_permission_requested_projects_tool_call_id() -> None:
    payload = {"tool_call_id": "call-write", "summary": "write pane"}
    projected, drops = project_agent_event(_event("permission_requested"), payload)
    assert drops == ProjectionDropCounts()
    assert projected == [
        {
            "type": "CUSTOM",
            "name": AGUI_PERMISSION_CUSTOM,
            "value": {"tool_call_id": "call-write"},
            "timestamp": int(CREATED_AT.timestamp() * 1000),
        }
    ]


def test_permission_requested_prefers_approval_request_id() -> None:
    payload = {
        "approval_request_id": str(APPROVAL_REQUEST_ID),
        "tool_call_id": "call-write",
    }
    projected, drops = project_agent_event(_event("permission_requested"), payload)
    assert drops == ProjectionDropCounts()
    assert projected[0]["value"] == {
        "approval_request_id": str(APPROVAL_REQUEST_ID)
    }


def test_permission_requested_without_any_id_is_malformed() -> None:
    payload = {"summary": "write pane"}
    projector = AgentEventProjector()
    event = _event(
        "permission_requested", payload=_payload_json(payload)
    )
    assert projector.project(event) == []
    assert projector.drops == ProjectionDropCounts(malformed_payload=1)


# ---------------------------------------------------------------------------
# Backend state (review fix: the canonical serializer writes state/epoch).
# ---------------------------------------------------------------------------


def test_backend_state_changed_projects_state_and_epoch() -> None:
    payload = {"state": "idle", "epoch": 7}
    projected, drops = project_agent_event(_event("backend_state_changed"), payload)
    assert drops == ProjectionDropCounts()
    assert projected == [
        {
            "type": "STATE_DELTA",
            "delta": [
                {
                    "op": "replace",
                    "path": AGUI_STATE_PATH,
                    "value": {"state": "idle", "epoch": 7},
                }
            ],
            "timestamp": int(CREATED_AT.timestamp() * 1000),
        }
    ]


def test_backend_state_changed_degrades_to_unknown_without_state() -> None:
    payload: dict[str, object] = {}
    projected, drops = project_agent_event(_event("backend_state_changed"), payload)
    assert drops == ProjectionDropCounts()
    assert projected[0]["delta"][0]["value"] == {"state": "unknown", "epoch": 0}


# ---------------------------------------------------------------------------
# Regression: message and run kinds keep their projection.
# ---------------------------------------------------------------------------


def test_message_delta_projection_unchanged() -> None:
    payload = {"message_id": str(MESSAGE_ID), "text": "hello"}
    projected, drops = project_agent_event(_event("message_delta"), payload)
    assert drops == ProjectionDropCounts()
    assert projected[0]["type"] == "TEXT_MESSAGE_CHUNK"
    assert projected[0]["messageId"] == str(MESSAGE_ID)
    assert projected[0]["delta"] == "hello"


def test_message_completed_projection_unchanged() -> None:
    payload = {"message_id": str(MESSAGE_ID)}
    projected, drops = project_agent_event(_event("message_completed"), payload)
    assert drops == ProjectionDropCounts()
    assert projected[0] == {
        "type": "TEXT_MESSAGE_END",
        "messageId": str(MESSAGE_ID),
        "timestamp": int(CREATED_AT.timestamp() * 1000),
    }


def test_run_boundaries_still_require_run_id() -> None:
    projected, drops = project_agent_event(
        _event("run_completed", run_id=None), {"input_id": None}
    )
    assert projected == []
    assert drops == ProjectionDropCounts(run_without_run_id=1)


def test_missing_payload_is_dropped_and_counted() -> None:
    projected, drops = project_agent_event(_event("tool_started"), None)
    assert projected == []
    assert drops == ProjectionDropCounts(missing_payload=1)
