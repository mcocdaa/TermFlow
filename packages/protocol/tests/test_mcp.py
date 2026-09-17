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
from termflow_protocol.messages import MAX_TERMINAL_BYTES

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


def test_pane_read_params_max_bytes_respects_hard_bound() -> None:
    PaneReadParams(pane_id="%1", view="viewport", max_bytes=MAX_PANE_READ_BYTES)
    with pytest.raises(ValidationError, match="max_bytes"):
        PaneReadParams(
            pane_id="%1",
            view="viewport",
            max_bytes=MAX_PANE_READ_BYTES + 1,
        )


def test_pane_read_default_matches_the_terminal_capture_ceiling() -> None:
    # Computer A rejects captures above the terminal payload bound, so the MCP
    # read window (and its default) must never exceed MAX_TERMINAL_BYTES.
    assert MAX_PANE_READ_BYTES == MAX_TERMINAL_BYTES
    assert PaneReadParams(pane_id="%1", view="viewport").max_bytes == MAX_TERMINAL_BYTES
