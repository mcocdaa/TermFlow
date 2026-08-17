from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from termflow_protocol import (
    WatchCondition,
    WatchConditionKind,
    WatchCreateParams,
)

_CURSOR: dict[str, object] = {
    "instance_id": uuid4(),
    "pane_id": "%1",
    "pane_incarnation": 0,
    "stream_id": uuid4(),
    "seq": 12,
}


def test_watch_create_params_round_trip() -> None:
    params = WatchCreateParams(
        pane_id="%1",
        instance_id=uuid4(),
        conversation_id=uuid4(),
        condition=WatchCondition(kind="output_contains", match="ready"),
        start_cursor=_CURSOR,
        expires_at=datetime.now(UTC),
        one_shot=True,
        intent="wait for the ready prompt",
    )
    dumped = params.model_dump()
    reparsed = WatchCreateParams.model_validate(dumped)
    assert reparsed == params
    assert reparsed.condition.kind is WatchConditionKind.OUTPUT_CONTAINS
