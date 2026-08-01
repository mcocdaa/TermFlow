from uuid import uuid4

import pytest
from termflow_control_plane.connections.terminal_hub import (
    LocalTerminalClose,
    TerminalHub,
)
from termflow_protocol import (
    MessageType,
    TerminalOpenedPayload,
    TerminalOutputPayload,
    WireMessage,
)


def _message(instance_id, message_type: MessageType, payload) -> WireMessage:
    return WireMessage(
        type=message_type,
        instance_id=instance_id,
        payload=payload.model_dump(mode="json"),
    )


@pytest.mark.asyncio
async def test_one_terminal_owner_is_replaced_deterministically() -> None:
    hub = TerminalHub(queue_max_messages=4, queue_max_bytes=1024)
    instance_id = uuid4()
    first = await hub.register(instance_id, session_key="first-session")

    second = await hub.register(instance_id, session_key="second-session")

    replacement = await first.next_event()
    assert isinstance(replacement, LocalTerminalClose)
    assert replacement.reason == "replaced"
    assert await hub.current(instance_id) is second


@pytest.mark.asyncio
async def test_bridge_output_tracks_exact_resume_cursor_and_is_memory_bounded() -> None:
    hub = TerminalHub(queue_max_messages=2, queue_max_bytes=8)
    instance_id = uuid4()
    terminal = await hub.register(instance_id, session_key=None)
    stream_id = uuid4()
    opened = _message(
        instance_id,
        MessageType.TERMINAL_OPENED,
        TerminalOpenedPayload(
            terminal_id=terminal.terminal_id,
            stream_id=stream_id,
            rows=24,
            cols=80,
        ),
    )
    assert await hub.forward(opened)
    assert await terminal.next_event() == opened

    first = _message(
        instance_id,
        MessageType.TERMINAL_OUTPUT,
        TerminalOutputPayload.from_bytes(
            terminal.terminal_id,
            stream_id,
            1,
            b"12345678",
        ),
    )
    assert await hub.forward(first)
    assert terminal.resume_cursor == (stream_id, 1)

    overflow = _message(
        instance_id,
        MessageType.TERMINAL_OUTPUT,
        TerminalOutputPayload.from_bytes(
            terminal.terminal_id,
            stream_id,
            2,
            b"x",
        ),
    )
    assert not await hub.forward(overflow)
    closed = await terminal.next_event()
    assert isinstance(closed, LocalTerminalClose)
    assert closed.reason == "internal_error"
    assert closed.error_code == "backpressure"


@pytest.mark.asyncio
async def test_logout_terminates_only_terminals_owned_by_that_hashed_session() -> None:
    hub = TerminalHub(queue_max_messages=4, queue_max_bytes=1024)
    first = await hub.register(uuid4(), session_key="session-a")
    second = await hub.register(uuid4(), session_key="session-b")

    assert await hub.terminate_session("session-a") == 1
    event = await first.next_event()
    assert isinstance(event, LocalTerminalClose)
    assert event.reason == "client_closed"
    assert not second.terminated


@pytest.mark.asyncio
async def test_exact_browser_cursor_resumes_the_same_terminal_and_rejects_mismatch() -> None:
    hub = TerminalHub(queue_max_messages=8, queue_max_bytes=1024)
    instance_id = uuid4()
    terminal = await hub.register(instance_id, session_key="browser-session")
    stream_id = uuid4()
    terminal.observe_opened(
        TerminalOpenedPayload(
            terminal_id=terminal.terminal_id,
            stream_id=stream_id,
            rows=24,
            cols=80,
        )
    )
    terminal.observe_output(
        TerminalOutputPayload.from_bytes(
            terminal.terminal_id,
            stream_id,
            1,
            b"already delivered",
        )
    )

    resumed = await hub.resume(
        instance_id,
        session_key="browser-session",
        terminal_id=terminal.terminal_id,
        stream_id=stream_id,
        after_seq=1,
    )

    assert resumed is terminal
    assert resumed.resume_cursor == (stream_id, 1)
    assert await hub.resume(
        instance_id,
        session_key="different-session",
        terminal_id=terminal.terminal_id,
        stream_id=stream_id,
        after_seq=1,
    ) is None
