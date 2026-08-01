from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import (
    PaneOutputPayload,
    PaneSnapshot,
    TopologySnapshot,
    WindowSnapshot,
    WireMessage,
)


def test_output_bytes_round_trip_as_base64() -> None:
    raw = b"\xff\x1b[31mred"
    payload = PaneOutputPayload.from_bytes("%1", uuid4(), 7, raw)
    assert payload.to_bytes() == raw
    assert payload.seq == 7


def test_output_rejects_malformed_base64() -> None:
    with pytest.raises(ValidationError):
        PaneOutputPayload(
            pane_id="%1",
            stream_id=uuid4(),
            seq=1,
            data_base64="not base64!",
            captured_at=datetime.now(UTC),
        )


def test_unknown_protocol_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WireMessage(
            protocol_version=2,
            message_id=uuid4(),
            type="bridge.heartbeat",
            instance_id=uuid4(),
            payload={},
        )


def test_tmux_ids_are_validated() -> None:
    with pytest.raises(ValidationError):
        PaneSnapshot(
            pane_id="1",
            window_id="@0",
            index=0,
            title="bad",
            width=80,
            height=24,
            active=True,
            dead=False,
        )


def test_topology_round_trip_preserves_hierarchy() -> None:
    pane = PaneSnapshot(
        pane_id="%1",
        window_id="@2",
        index=0,
        title="shell",
        width=80,
        height=24,
        active=True,
        dead=False,
    )
    topology = TopologySnapshot(
        session_id="$3",
        session_name="main",
        revision=4,
        windows=[WindowSnapshot(window_id="@2", index=0, name="main", active=True, panes=[pane])],
    )
    assert topology.windows[0].panes[0].pane_id == "%1"

