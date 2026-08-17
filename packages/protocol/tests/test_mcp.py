from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import (
    PaneReadParams,
    PaneSendKeysParams,
    WatchCondition,
    WatchConditionKind,
    WatchCreateParams,
)
from termflow_protocol.mcp import MAX_PANE_READ_BYTES

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


def test_pane_send_keys_params_accepts_closed_vocabulary() -> None:
    params = PaneSendKeysParams(
        pane_id="%1",
        request_key="req-1",
        conversation_id=uuid4(),
        keys=("enter", "ctrl-c", "f12", "shift-tab"),
    )
    assert params.keys == ("enter", "ctrl-c", "f12", "shift-tab")


def test_pane_send_keys_params_rejects_unknown_named_keys() -> None:
    with pytest.raises(ValidationError, match="unknown named keys"):
        PaneSendKeysParams(
            pane_id="%1",
            request_key="req-1",
            conversation_id=uuid4(),
            keys=("enter", "ctrl-shift-c"),
        )
