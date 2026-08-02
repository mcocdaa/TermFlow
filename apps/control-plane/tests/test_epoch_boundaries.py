import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from termflow_control_plane import app as app_module
from termflow_control_plane.api.events import _send_subscription
from termflow_control_plane.api.terminal import (
    _receive_terminal_input,
    _send_terminal_events,
)
from termflow_control_plane.auth.sessions import BrowserSessionStore
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.connections.terminal_hub import TerminalHub
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_protocol import MessageType, TerminalOpenedPayload, WireMessage


class _FakeWebSocket:
    def __init__(self, *incoming: dict[str, object]) -> None:
        self._incoming = list(incoming)
        self._never = asyncio.Event()
        self.closed: tuple[int, str] | None = None
        self.sent: list[str] = []
        self.sent_bytes: list[bytes] = []

    async def receive(self) -> dict[str, object]:
        if self._incoming:
            return self._incoming.pop(0)
        await self._never.wait()
        raise AssertionError("unreachable")

    async def close(self, *, code: int, reason: str = "") -> None:
        self.closed = (code, reason)

    async def send_text(self, value: str) -> None:
        self.sent.append(value)

    async def send_bytes(self, value: bytes) -> None:
        self.sent_bytes.append(value)


@pytest.mark.asyncio
async def test_terminal_input_checks_persisted_epoch_before_routing(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'terminal-boundary.db'}"
    database = Database(database_url)
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    terminal_hub = TerminalHub(queue_max_messages=2, queue_max_bytes=1024)
    terminal = await terminal_hub.register(uuid4(), session_key=None, auth_epoch=1)
    websocket = _FakeWebSocket({"type": "websocket.receive", "bytes": b"blocked"})
    terminal_router = SimpleNamespace(
        input=AsyncMock(),
        action=AsyncMock(),
        request_close=AsyncMock(),
    )
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=database_url,
        allow_insecure_loopback=True,
    )
    try:
        assert await repositories.auth_state.reset_and_increment_epoch() == 2

        await _receive_terminal_input(
            websocket,
            terminal,
            terminal_router,
            settings,
            repositories,
            1,
        )

        terminal_router.input.assert_not_awaited()
        assert terminal.terminated
        assert websocket.closed == (4401, "Authentication epoch changed")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_terminal_output_checks_persisted_epoch_before_sending(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'output-boundary.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    terminal_hub = TerminalHub(queue_max_messages=2, queue_max_bytes=1024)
    instance_id = uuid4()
    terminal = await terminal_hub.register(instance_id, session_key=None, auth_epoch=1)
    websocket = _FakeWebSocket()
    terminal_router = SimpleNamespace(request_close=AsyncMock())
    terminal.enqueue(
        WireMessage(
            type=MessageType.TERMINAL_OPENED,
            instance_id=instance_id,
            payload=TerminalOpenedPayload(
                terminal_id=terminal.terminal_id,
                stream_id=uuid4(),
                rows=24,
                cols=80,
            ).model_dump(mode="json"),
        )
    )
    try:
        assert await repositories.auth_state.reset_and_increment_epoch() == 2

        await _send_terminal_events(
            websocket,
            terminal,
            terminal_router,
            repositories,
            1,
        )

        terminal_router.request_close.assert_awaited_once_with(
            terminal,
            "client_closed",
        )
        assert websocket.sent == []
        assert websocket.sent_bytes == []
        assert websocket.closed == (4401, "Authentication epoch changed")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_event_subscription_consumes_quiet_client_disconnect(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'event-disconnect.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    subscriber = await EventHub(queue_size=2).subscribe(instance_id=None, auth_epoch=1)
    websocket = _FakeWebSocket({"type": "websocket.disconnect"})
    try:
        await asyncio.wait_for(
            _send_subscription(websocket, subscriber, repositories, 1),
            timeout=0.2,
        )
        assert websocket.closed is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_event_message_checks_persisted_epoch_before_sending(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'event-boundary.db'}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    hub = EventHub(queue_size=2)
    subscriber = await hub.subscribe(instance_id=None, auth_epoch=1)
    websocket = _FakeWebSocket()
    subscriber.queue.put_nowait(
        WireMessage(
            type=MessageType.TOPOLOGY_CHANGED,
            instance_id=uuid4(),
            payload={"topology": {}},
        )
    )
    try:
        assert await repositories.auth_state.reset_and_increment_epoch() == 2

        await _send_subscription(websocket, subscriber, repositories, 1)

        assert websocket.sent == []
        assert websocket.closed == (4401, "Authentication epoch changed")
    finally:
        await database.dispose()


class _FlakyAuthState:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self) -> SimpleNamespace:
        self.calls += 1
        if self.calls == 1:
            raise OperationalError("SELECT authentication_state", {}, Exception("busy"))
        return SimpleNamespace(epoch=2)


@pytest.mark.asyncio
async def test_epoch_watcher_retries_after_transient_database_error(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "_AUTHENTICATION_EPOCH_POLL_SECONDS",
        0.01,
        raising=False,
    )
    auth_state = _FlakyAuthState()
    repositories = SimpleNamespace(auth_state=auth_state)
    sessions = BrowserSessionStore(ttl=timedelta(minutes=5), capacity=2)
    terminal_hub = TerminalHub(queue_max_messages=2, queue_max_bytes=1024)
    event_hub = EventHub(queue_size=2)
    terminal = await terminal_hub.register(uuid4(), session_key=None, auth_epoch=1)
    subscriber = await event_hub.subscribe(instance_id=None, auth_epoch=1)
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        app_module._authentication_epoch_loop(
            repositories,
            sessions,
            terminal_hub,
            event_hub,
            stop,
        )
    )
    try:
        for _ in range(40):
            if terminal.terminated and subscriber.closed.is_set():
                break
            await asyncio.sleep(0.005)
        assert not watcher.done()
        assert auth_state.calls >= 2
        assert terminal.terminated
        assert subscriber.closed.is_set()
        assert sessions.epoch == 2
    finally:
        stop.set()
        await watcher
