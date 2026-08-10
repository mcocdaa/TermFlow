from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from termflow_protocol import (
    AgentEvent,
    AgentInput,
    AgentInputDeliveryState,
    MessageDeltaEvent,
    RunFailedEvent,
    RunStartedEvent,
    UserMessageInput,
    parse_agent_event,
    parse_agent_input,
)

_INPUT_VARIANTS: list[tuple[str, str, str, dict[str, object]]] = [
    (
        "user_message",
        "user_session",
        "user",
        {"text": "hello agent", "draft_ref": "draft-7"},
    ),
    (
        "watch_triggered",
        "watch_engine",
        "system",
        {
            "watch_id": uuid4(),
            "watch_generation": 2,
            "trigger_event_id": uuid4(),
            "continuation": "the pane now contains the ready prompt",
            "observation_ref": "pane:%1 seq=12",
        },
    ),
    (
        "timer_triggered",
        "timer",
        "system",
        {"timer_id": uuid4(), "scheduled_for": datetime.now(UTC)},
    ),
    (
        "permission_resolved",
        "user_session",
        "user",
        {
            "approval_request_id": uuid4(),
            "decision": "approved",
            "resolved_by_actor_id": "user-session-1",
        },
    ),
    (
        "system_notification",
        "backend",
        "backend",
        {"notification_type": "backend_unavailable", "text": "backend temporarily unavailable"},
    ),
]

_EVENT_VARIANTS: list[tuple[str, dict[str, object]]] = [
    ("run_started", {"input_id": uuid4()}),
    (
        "message_delta",
        {
            "message_id": uuid4(),
            "part_id": "part-0",
            "assembly_revision": 3,
            "text": "hel",
        },
    ),
    (
        "message_completed",
        {"message_id": uuid4(), "assembly_revision": 4, "final": True},
    ),
    (
        "tool_started",
        {"tool_name": "termflow_pane_read", "tool_call_id": "call-1"},
    ),
    (
        "tool_completed",
        {
            "tool_name": "termflow_pane_read",
            "tool_call_id": "call-1",
            "status": "success",
            "input_bytes": 0,
            "output_bytes": 42,
            "output_hash": "abc123",
            "truncated": True,
        },
    ),
    (
        "permission_requested",
        {
            "approval_request_id": uuid4(),
            "tool_name": "termflow_pane_send_text",
            "evidence": "type 'yes' and submit",
            "expires_at": datetime.now(UTC),
        },
    ),
    ("run_completed", {"input_id": uuid4()}),
    (
        "run_failed",
        {"error_code": "context_lost", "error_message": "backend lost context", "retryable": True},
    ),
    ("backend_state_changed", {"state": "ready", "epoch": 4}),
]


def _input_dict(
    kind: str, actor_kind: str, source: str, payload: dict[str, object]
) -> dict[str, object]:
    return {
        "kind": kind,
        "input_id": uuid4(),
        "conversation_id": uuid4(),
        "actor_id": f"actor-{kind}",
        "actor_kind": actor_kind,
        "auth_epoch": 3,
        "admission_seq": 41,
        "idempotency_key": f"idem-{kind}-1",
        "source": source,
        "causation_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "created_at": datetime.now(UTC),
        "delivery_state": "pending",
        "attempt_count": 0,
        "payload": payload,
    }


def _event_dict(kind: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "kind": kind,
        "event_id": uuid4(),
        "conversation_id": uuid4(),
        "run_id": uuid4(),
        "dedup_key": f"dedup-{kind}-1",
        "database_seq": 1,
        "created_at": datetime.now(UTC),
        "payload": payload,
    }


@pytest.mark.parametrize(("kind", "actor_kind", "source", "payload"), _INPUT_VARIANTS)
def test_agent_input_variants_round_trip(
    kind: str,
    actor_kind: str,
    source: str,
    payload: dict[str, object],
) -> None:
    data = _input_dict(kind, actor_kind, source, payload)
    parsed = parse_agent_input(data)
    assert parsed.kind == kind
    assert parsed.actor_kind.value == actor_kind
    assert parsed.source.value == source
    assert parsed.delivery_state is AgentInputDeliveryState.PENDING
    assert parsed.attempt_count == 0
    assert parsed.causation_id and parsed.correlation_id
    reparsed = parse_agent_input(parsed.model_dump())
    assert reparsed == parsed


@pytest.mark.parametrize(("kind", "payload"), _EVENT_VARIANTS)
def test_agent_event_variants_round_trip(kind: str, payload: dict[str, object]) -> None:
    data = _event_dict(kind, payload)
    parsed = parse_agent_event(data)
    assert parsed.kind == kind
    assert parsed.dedup_key == f"dedup-{kind}-1"
    assert parsed.database_seq == 1
    reparsed = parse_agent_event(parsed.model_dump())
    assert reparsed == parsed


def test_typed_union_round_trips_via_type_adapter() -> None:
    data = _input_dict("user_message", "user_session", "user", {"text": "hello"})
    adapter = TypeAdapter(AgentInput)
    parsed = adapter.validate_python(data)
    assert isinstance(parsed, UserMessageInput)
    assert adapter.validate_python(parsed.model_dump()) == parsed
    event_adapter = TypeAdapter(AgentEvent)
    event = event_adapter.validate_python(_event_dict("run_started", {"input_id": uuid4()}))
    assert isinstance(event, RunStartedEvent)
    assert event_adapter.validate_python(event.model_dump()) == event


def test_agent_wire_models_survive_json_mode_round_trip() -> None:
    parsed = parse_agent_input(
        _input_dict("user_message", "user_session", "user", {"text": "hello"})
    )
    assert parse_agent_input(parsed.model_dump(mode="json")) == parsed
    event = parse_agent_event(
        _event_dict(
            "message_delta",
            {"message_id": uuid4(), "part_id": "part-0", "assembly_revision": 1},
        )
    )
    assert parse_agent_event(event.model_dump(mode="json")) == event


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("user_message", {"text": "hello"}),
        (
            "watch_triggered",
            {"watch_id": uuid4(), "watch_generation": 1, "trigger_event_id": uuid4()},
        ),
        ("timer_triggered", {"timer_id": uuid4(), "scheduled_for": datetime.now(UTC)}),
        (
            "permission_resolved",
            {"approval_request_id": uuid4(), "decision": "denied"},
        ),
        ("system_notification", {"notification_type": "notice", "text": "hello"}),
    ],
)
def test_agent_input_parses_to_concrete_variant(kind: str, payload: dict[str, object]) -> None:
    parsed = parse_agent_input(_input_dict(kind, "system", "system", payload))
    assert parsed.kind == kind
    assert parsed.payload is not None


def test_agent_input_rejects_unknown_kind() -> None:
    data = _input_dict("bogus_kind", "user_session", "user", {"text": "hello"})
    with pytest.raises(ValidationError, match="bogus_kind"):
        parse_agent_input(data)


def test_agent_event_rejects_unknown_kind() -> None:
    data = _event_dict("bogus_kind", {})
    with pytest.raises(ValidationError, match="bogus_kind"):
        parse_agent_event(data)


@pytest.mark.parametrize(
    "field", ["admission_seq", "idempotency_key", "causation_id", "correlation_id"]
)
def test_agent_input_requires_core_delivery_fields(field: str) -> None:
    data = _input_dict("user_message", "user_session", "user", {"text": "hello"})
    del data[field]
    with pytest.raises(ValidationError, match=field):
        parse_agent_input(data)


def test_agent_input_requires_payload() -> None:
    data = _input_dict("user_message", "user_session", "user", {"text": "hello"})
    del data["payload"]
    with pytest.raises(ValidationError):
        parse_agent_input(data)


def test_payload_is_discriminated_by_variant() -> None:
    # A user_message cannot carry a permission-resolved payload.
    data = _input_dict(
        "user_message",
        "user_session",
        "user",
        {"approval_request_id": uuid4(), "decision": "approved"},
    )
    with pytest.raises(ValidationError):
        parse_agent_input(data)


def test_user_message_rejects_oversized_text() -> None:
    data = _input_dict(
        "user_message",
        "user_session",
        "user",
        {"text": "x" * (64 * 1024 + 1)},
    )
    with pytest.raises(ValidationError, match="65536"):
        parse_agent_input(data)


def test_watch_triggered_observation_ref_is_bounded() -> None:
    data = _input_dict(
        "watch_triggered",
        "watch_engine",
        "system",
        {
            "watch_id": uuid4(),
            "watch_generation": 1,
            "trigger_event_id": uuid4(),
            "observation_ref": "x" * 2049,
        },
    )
    with pytest.raises(ValidationError):
        parse_agent_input(data)


def test_system_notification_text_is_bounded() -> None:
    data = _input_dict(
        "system_notification",
        "backend",
        "backend",
        {"notification_type": "notice", "text": "x" * 2049},
    )
    with pytest.raises(ValidationError):
        parse_agent_input(data)


def test_message_delta_is_ephemeral_by_default() -> None:
    parsed = parse_agent_event(
        _event_dict(
            "message_delta",
            {"message_id": uuid4(), "part_id": "part-0", "assembly_revision": 1},
        )
    )
    assert isinstance(parsed, MessageDeltaEvent)
    assert parsed.payload.ephemeral is True
    assert parsed.payload.assembly_revision == 1


def test_agent_input_rejects_unknown_fields() -> None:
    data = _input_dict("user_message", "user_session", "user", {"text": "hello"})
    data["surprise"] = "extra"
    with pytest.raises(ValidationError):
        parse_agent_input(data)


def test_actor_kind_is_a_closed_set() -> None:
    data = _input_dict("user_message", "random_actor", "user", {"text": "hello"})
    with pytest.raises(ValidationError):
        parse_agent_input(data)


def test_actor_kind_enum_members_match_contract() -> None:
    from termflow_protocol import AgentActorKind

    assert {member.value for member in AgentActorKind} == {
        "user_session",
        "client",
        "watch_engine",
        "timer",
        "backend",
        "system",
    }


def test_input_kind_enum_members_match_contract() -> None:
    from termflow_protocol import AgentInputKind

    assert {member.value for member in AgentInputKind} == {
        "user_message",
        "watch_triggered",
        "timer_triggered",
        "permission_resolved",
        "system_notification",
    }


def test_event_kind_enum_members_match_contract() -> None:
    from termflow_protocol import AgentEventKind

    assert {member.value for member in AgentEventKind} == {
        "run_started",
        "message_delta",
        "message_completed",
        "tool_started",
        "tool_completed",
        "permission_requested",
        "run_completed",
        "run_failed",
        "backend_state_changed",
    }


def test_run_failed_carries_bounded_error() -> None:
    parsed = parse_agent_event(
        _event_dict(
            "run_failed",
            {"error_code": "context_lost", "error_message": "x" * 2049},
        )
    )
    with pytest.raises(ValidationError):
        parse_agent_event(
            _event_dict(
                "run_failed",
                {"error_code": "context_lost", "error_message": "x" * 4097},
            )
        )
    assert isinstance(parsed, RunFailedEvent)
