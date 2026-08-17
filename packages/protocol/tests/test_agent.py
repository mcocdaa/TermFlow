from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import (
    parse_agent_event,
    parse_agent_input,
)
from termflow_protocol.agent import MAX_AGENT_TEXT_BYTES


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


def test_agent_input_rejects_unknown_kind() -> None:
    data = _input_dict("bogus_kind", "user_session", "user", {"text": "hello"})
    with pytest.raises(ValidationError, match="bogus_kind"):
        parse_agent_input(data)


def test_system_notification_text_is_plain_text_with_byte_cap() -> None:
    payload = {"notification_type": "notice", "text": "maintenance soon"}
    parsed = parse_agent_input(
        _input_dict("system_notification", "system", "system", payload)
    )
    assert parsed.payload.text == "maintenance soon"

    with pytest.raises(ValidationError, match="control"):
        parse_agent_input(
            _input_dict(
                "system_notification",
                "system",
                "system",
                {"notification_type": "notice", "text": "bad\x00text"},
            )
        )

    with pytest.raises(ValidationError, match="exceeds"):
        parse_agent_input(
            _input_dict(
                "system_notification",
                "system",
                "system",
                {
                    "notification_type": "notice",
                    "text": "x" * (MAX_AGENT_TEXT_BYTES + 1),
                },
            )
        )
