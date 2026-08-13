"""AG-UI wire projection tests (plan M6a spec §4.3/§4.6, test matrix §8).

The local pinned fixture ``fixtures/agui/agui-0.1.19-wire.json`` is the
anti-drift contract for every projected kind: each projection must equal the
fixture entry exactly (uppercase snake discriminator, camelCase field names,
millisecond ``timestamp``, no extra fields), byte-for-byte in compact JSON
serialization.  Explicit drop cases pin the diagnostic counters, and the
non-leakage invariant is scanned across every projected kind.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
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

WIRE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "agui" / "agui-0.1.19-wire.json"
    ).read_text()
)

ALL_KINDS = (
    "run_started",
    "message_delta",
    "message_completed",
    "tool_started",
    "tool_completed",
    "permission_requested",
    "run_completed",
    "run_failed",
    "backend_state_changed",
)

#: B-internal identifiers that must never appear in AG-UI wire objects
#: (spec §4.3 non-leakage invariant).
FORBIDDEN_TOKENS = (
    "database_seq",
    "event_id",
    "payload_digest",
    "dedup_key",
    "ephemeral",
    "auth_epoch",
    "cursor",
)


def _payload(kind: str) -> dict[str, object]:
    return {
        "run_started": {"input_id": None},
        "message_delta": {
            "message_id": str(MESSAGE_ID),
            "part_id": "part-1",
            "assembly_revision": 3,
            "text": "Hello, world!",
            "ephemeral": True,
        },
        "message_completed": {
            "message_id": str(MESSAGE_ID),
            "assembly_revision": 3,
            "final": True,
        },
        "tool_started": {"tool_name": "read_file", "tool_call_id": "tool-call-123"},
        "tool_completed": {
            "tool_name": "read_file",
            "tool_call_id": "tool-call-123",
            "status": "error",
            "input_bytes": 0,
            "output_bytes": 0,
            "input_hash": "a" * 64,
            "output_hash": "b" * 64,
            "truncated": False,
            "error_code": "E_READ",
            "error_message": "file not found",
        },
        "permission_requested": {
            "approval_request_id": str(APPROVAL_REQUEST_ID),
            "tool_name": "write_file",
            "evidence": "trusted by policy",
            "expires_at": "2026-08-13T13:00:00+00:00",
        },
        "run_completed": {"input_id": None},
        "run_failed": {
            "error_code": "E_RUN",
            "error_message": "provider failed",
            "retryable": True,
        },
        "backend_state_changed": {"state": "running", "epoch": 7},
    }[kind]


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


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_projection_matches_pinned_wire_fixture(kind: str) -> None:
    projected, drops = project_agent_event(_event(kind), _payload(kind))
    expected = WIRE[kind]
    assert drops == ProjectionDropCounts()
    assert projected == [expected]
    # Byte-pinned: compact serialization is identical (key order, camelCase
    # names, uppercase discriminator, no extra fields).
    assert json.dumps(projected[0], separators=(",", ":")) == json.dumps(
        expected, separators=(",", ":")
    )
    assert projected[0]["timestamp"] == int(CREATED_AT.timestamp() * 1000)


def test_projection_never_leaks_b_internal_identifiers() -> None:
    for kind in ALL_KINDS:
        projected, drops = project_agent_event(_event(kind), _payload(kind))
        assert drops == ProjectionDropCounts()
        assert projected
        serialized = json.dumps(projected[0], separators=(",", ":"))
        for token in FORBIDDEN_TOKENS:
            assert token not in serialized, f"{kind} leaked {token!r}"


def test_naive_created_at_is_interpreted_as_utc() -> None:
    """SQLite round-trips ``created_at`` as a naive datetime; the projection
    must read it as UTC so the AG-UI millisecond timestamp stays correct on
    non-UTC hosts (review fix: naive -> UTC before ``.timestamp()``)."""
    import os
    import time as time_module

    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time_module.tzset()
    try:
        naive = datetime(2026, 8, 13, 12, 0, 0)  # no tzinfo, like a DB read
        event = _event("message_delta")
        event.created_at = naive
        projected, drops = project_agent_event(event, _payload("message_delta"))
        assert drops == ProjectionDropCounts()
        # The wire timestamp is the UTC reading of the naive value, never the
        # local (UTC+8 here) reading.
        assert projected[0]["timestamp"] == int(
            naive.replace(tzinfo=UTC).timestamp() * 1000
        )
        assert projected[0]["timestamp"] != int(naive.timestamp() * 1000)
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time_module.tzset()


# ---------------------------------------------------------------------------
# Explicit drops and diagnostic counters (spec §4.6)
# ---------------------------------------------------------------------------


def test_unknown_kind_is_dropped_and_counted() -> None:
    projected, drops = project_agent_event(_event("future_kind"), {})
    assert projected == []
    assert drops == ProjectionDropCounts(unknown_kind=1)


@pytest.mark.parametrize("text", ["", None])
def test_empty_or_missing_delta_is_dropped_and_counted(text: str | None) -> None:
    payload = {"message_id": str(MESSAGE_ID), "text": text}
    projected, drops = project_agent_event(_event("message_delta"), payload)
    assert projected == []
    assert drops == ProjectionDropCounts(empty_delta=1)


@pytest.mark.parametrize("kind", ["run_started", "run_completed"])
def test_run_events_without_run_id_are_dropped_and_counted(kind: str) -> None:
    projected, drops = project_agent_event(_event(kind, run_id=None), {})
    assert projected == []
    assert drops == ProjectionDropCounts(run_without_run_id=1)


def test_missing_payload_is_dropped_and_counted() -> None:
    projected, drops = project_agent_event(_event("run_started"), None)
    assert projected == []
    assert drops == ProjectionDropCounts(missing_payload=1)


# ---------------------------------------------------------------------------
# Synthesized and documented shapes (spec §4.3)
# ---------------------------------------------------------------------------


def test_tool_call_result_is_synthesized_and_hash_free() -> None:
    projected, _ = project_agent_event(_event("tool_completed"), _payload("tool_completed"))
    out = projected[0]
    # messageId is synthesized from tool_call_id (B canonical has no
    # message<->tool association; C correlates by toolCallId).
    assert out["messageId"] == out["toolCallId"] == "tool-call-123"
    assert out["role"] == "tool"
    content = json.loads(out["content"])
    assert set(content) == {
        "status",
        "input_bytes",
        "output_bytes",
        "truncated",
        "error_code",
        "error_message",
    }
    assert content["status"] == "error"
    # Raw result hashes are never projected.
    assert "input_hash" not in content
    assert "output_hash" not in content


def test_custom_event_omits_absent_optional_fields() -> None:
    payload = {"approval_request_id": str(APPROVAL_REQUEST_ID)}
    projected, drops = project_agent_event(_event("permission_requested"), payload)
    assert drops == ProjectionDropCounts()
    assert projected[0]["name"] == AGUI_PERMISSION_CUSTOM
    assert projected[0]["value"] == {"approval_request_id": str(APPROVAL_REQUEST_ID)}


def test_state_delta_uses_backend_path_and_value_shape() -> None:
    projected, drops = project_agent_event(
        _event("backend_state_changed"), _payload("backend_state_changed")
    )
    assert drops == ProjectionDropCounts()
    assert projected[0]["delta"] == [
        {"op": "replace", "path": AGUI_STATE_PATH, "value": {"state": "running", "epoch": 7}}
    ]


# ---------------------------------------------------------------------------
# AgentEventProjector: per-connection wrapper, parsing, drop counters
# ---------------------------------------------------------------------------


def test_projector_drops_corrupt_payload_without_raising() -> None:
    projector = AgentEventProjector()
    for raw in ("not-json{", json.dumps([1, 2])):
        assert projector.project(_event("message_delta", payload=raw)) == []
    assert projector.drops == ProjectionDropCounts(malformed_payload=2)


def test_projector_drops_payload_missing_required_fields() -> None:
    projector = AgentEventProjector()
    payload = json.dumps({"text": "no message id"}, separators=(",", ":"))
    assert projector.project(_event("message_delta", payload=payload)) == []
    assert projector.drops == ProjectionDropCounts(malformed_payload=1)


def test_projector_counts_accumulate_per_connection() -> None:
    projector = AgentEventProjector()
    assert projector.project(_event("future_kind", payload="{}")) == []
    assert projector.project(_event("message_delta", payload="not-json{")) == []
    assert projector.project(_event("run_started", run_id=None, payload="{}")) == []
    assert projector.drops == ProjectionDropCounts(
        unknown_kind=1, malformed_payload=1, run_without_run_id=1
    )


def test_projector_counts_missing_payload_and_projects_healthy_events() -> None:
    projector = AgentEventProjector()
    assert projector.project(_event("message_delta", payload=None)) == []
    healthy = projector.project(
        _event("message_delta", payload=json.dumps(_payload("message_delta")))
    )
    assert healthy == [WIRE["message_delta"]]
    assert projector.drops == ProjectionDropCounts(missing_payload=1)


def test_projection_is_stateless_across_events() -> None:
    first, first_drops = project_agent_event(
        _event("message_delta"), _payload("message_delta")
    )
    second, second_drops = project_agent_event(
        _event("run_completed"), _payload("run_completed")
    )
    alone, alone_drops = project_agent_event(
        _event("run_completed"), _payload("run_completed")
    )
    assert second == alone
    assert second_drops == alone_drops
    assert first_drops == ProjectionDropCounts()
