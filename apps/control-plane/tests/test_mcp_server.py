"""MCP server connector tests (plan §10, §22; tasks M4.4, M5.2).

Keeps the most end-to-end paths:

- the write tools round-trip through the approval-gated command port with a
  tool_call_id derived from the MCP request id;
- a full initialize + tools/call round trip with a real seeded AgentToken
  through the app-level mount (M4.5 regression).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from termflow_control_plane.app import create_app
from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.models import Base
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    WatchContinuationService,
)
from termflow_control_plane.plugins.agent_broker.api.mcp_server import (
    MCP_STREAMABLE_HTTP_PATH,
    build_mcp_server,
    principal_override,
)
from termflow_control_plane.plugins.agent_broker.auth import (
    SCOPE_TERMINAL_OBSERVE,
    SCOPE_TERMINAL_WRITE,
    AgentTokenPrincipal,
)
from termflow_protocol.mcp import (
    PaneReadParams,
    PaneReadResult,
    PaneSendKeysResult,
    PaneSendTextResult,
    PaneSummary,
    TermFlowToolName,
)
from termflow_protocol.topology import PaneSnapshot, TopologySnapshot, WindowSnapshot


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    bundle = RepositoryBundle(session_factory)
    # Test seam: let tests open ad-hoc sessions against the same database.
    bundle.session_factory = session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await engine.dispose()


async def _seed_binding(repositories: RepositoryBundle, *, runtime_epoch: int = 2):
    profile = await repositories.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    display_name = f"term-{uuid4().hex[:8]}"
    installation = await repositories.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    term = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    return await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="ready",
        runtime_ref="runtime-1",
        runtime_epoch=runtime_epoch,
        capability_ref="cap-1",
    )


async def _seed_token(
    repositories: RepositoryBundle,
    binding,
    *,
    binding_epoch: int | None = None,
    expires_in: timedelta = timedelta(hours=1),
    scopes: tuple[str, ...] = (SCOPE_TERMINAL_OBSERVE,),
) -> str:
    raw_token = f"mcp-{uuid4().hex}"
    epoch = binding.runtime_epoch if binding_epoch is None else binding_epoch
    await repositories.agent_tokens.create(
        binding_id=binding.id,
        token_hash=hash_token(raw_token),
        scopes=scopes,
        expiry_epoch=int((datetime.now(UTC) + expires_in).timestamp()),
        binding_epoch=epoch,
    )
    return raw_token


async def _seed_conversation(repositories: RepositoryBundle, binding) -> object:
    return await repositories.agent_conversations.create(
        binding_id=binding.id,
        title="round trip",
    )


def _live_topology() -> TopologySnapshot:
    """A Term topology for the app-level round trip (M4.5 regression)."""
    return TopologySnapshot(
        session_id="$0",
        session_name="test",
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="win0",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%0",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=80,
                        height=24,
                        active=True,
                        dead=False,
                    ),
                    PaneSnapshot(
                        pane_id="%1",
                        window_id="@0",
                        index=1,
                        title="build log",
                        width=80,
                        height=24,
                        active=False,
                        dead=False,
                    ),
                ],
            ),
        ],
    )


def _seed_app_round_trip(client: TestClient) -> str:
    """Seed a binding + AgentToken and a live Term with a pane topology."""

    async def _seed() -> str:
        binding = await _seed_binding(client.app.state.repositories)
        connection = await client.app.state.registry.register(binding.term_id)
        connection.topology = _live_topology()
        return await _seed_token(client.app.state.repositories, binding)

    return client.portal.call(_seed)


async def _allow_async(repositories: RepositoryBundle, binding, pane_id: str) -> None:
    await repositories.pane_policies.set_policy(
        binding_id=binding.id, pane_id=pane_id, allowed=True
    )


class FakeObservation:
    """TerminalObservationPort fake with bounded, delayable reads."""

    def __init__(self, content: str = "bounded output") -> None:
        self.content = content
        self.reads: list[PaneReadParams] = []
        self.list_calls = 0
        self.read_delay: float = 0.0
        self.read_active = 0
        self.read_max_active = 0

    async def list_panes(self, instance_id) -> list[PaneSummary]:
        self.list_calls += 1
        return [
            PaneSummary(pane_id="%0", index=0, title="shell", active=True, dead=False),
            PaneSummary(pane_id="%1", index=1, title="build log", active=False, dead=False),
        ]

    async def read_pane(self, instance_id, params: PaneReadParams) -> PaneReadResult:
        self.reads.append(params)
        self.read_active += 1
        self.read_max_active = max(self.read_max_active, self.read_active)
        try:
            if self.read_delay:
                await asyncio.sleep(self.read_delay)
            return PaneReadResult(
                instance_id=instance_id,
                pane_id=params.pane_id,
                view=params.view,
                content=self.content,
                truncated=False,
            )
        finally:
            self.read_active -= 1

    async def resolve_cursor(self, instance_id, pane_id):
        return None


class FakeCommands:
    """TerminalCommandPort fake: records calls, returns confirmed receipts."""

    def __init__(self) -> None:
        self.text_calls: list[tuple] = []
        self.keys_calls: list[tuple] = []
        self.text_delay: float = 0.0

    async def send_text(self, principal, params, *, tool_call_id):
        if self.text_delay:
            await asyncio.sleep(self.text_delay)
        self.text_calls.append((principal, params, tool_call_id))
        return PaneSendTextResult(
            request_key=params.request_key,
            ok=True,
            outcome="confirmed",
            approval_id=uuid4(),
        )

    async def send_keys(self, principal, params, *, tool_call_id):
        self.keys_calls.append((principal, params, tool_call_id))
        return PaneSendKeysResult(
            request_key=params.request_key,
            ok=True,
            outcome="confirmed",
            approval_id=uuid4(),
        )


def _principal(binding, *, scopes: frozenset[str] | None = None) -> AgentTokenPrincipal:
    return AgentTokenPrincipal(
        binding_id=binding.id,
        instance_id=binding.term_id,
        scopes=(
            scopes
            if scopes is not None
            else frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE})
        ),
        runtime_epoch=binding.runtime_epoch or 1,
    )


def _sse_payload(text: str) -> dict:
    """Extract the JSON-RPC envelope from an SSE response body."""
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(f"no data line in SSE body: {text!r}")


def _initialize(client: TestClient, token: str) -> dict[str, str]:
    """Initialize an MCP session and return the request headers to reuse."""
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        MCP_STREAMABLE_HTTP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mcp-test", "version": "1"},
            },
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    session_id = response.headers.get("mcp-session-id")
    assert session_id is not None
    headers["mcp-session-id"] = session_id
    response = client.post(
        MCP_STREAMABLE_HTTP_PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return headers


def _call_tool(
    client: TestClient, headers: dict[str, str], name: str, arguments: dict
) -> dict:
    response = client.post(
        MCP_STREAMABLE_HTTP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return _sse_payload(response.text)


def _build_server(repositories: RepositoryBundle, commands: FakeCommands) -> MCPServer:
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    return build_mcp_server(
        observation=FakeObservation(),
        continuation=WatchContinuationService(
            repositories.watches, repositories.agent_bindings
        ),
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=commands,
    )


async def test_write_tools_round_trip_over_in_memory_transport(repositories) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    await _allow_async(repositories, binding, "%1")
    commands = FakeCommands()
    server = _build_server(repositories, commands)

    async with Client(server) as client:
        async with principal_override(_principal(binding)):
            text = await client.call_tool(
                TermFlowToolName.PANE_SEND_TEXT.value,
                {
                    "params": {
                        "pane_id": "%0",
                        "request_key": "req-1",
                        "conversation_id": str(conversation.id),
                        "intent": "run the tests",
                        "text": "make test",
                        "submit": True,
                    }
                },
            )
            assert text.is_error is False
            content = text.structured_content
            assert content["ok"] is True
            assert content["outcome"] == "confirmed"
            assert content["request_key"] == "req-1"
            assert content["approval_id"] is not None

            keys = await client.call_tool(
                TermFlowToolName.PANE_SEND_KEYS.value,
                {
                    "params": {
                        "pane_id": "%1",
                        "request_key": "req-2",
                        "conversation_id": str(conversation.id),
                        "keys": ["ctrl-c"],
                    }
                },
            )
            assert keys.is_error is False
            assert keys.structured_content["outcome"] == "confirmed"

    # The command port received the principal and a non-empty tool_call_id
    # derived from the MCP request id (spec §6).
    assert len(commands.text_calls) == 1
    text_call = commands.text_calls[0]
    assert text_call[0] == _principal(binding)
    assert text_call[1].text == "make test"
    assert text_call[2]
    assert len(commands.keys_calls) == 1
    assert commands.keys_calls[0][1].keys == ("ctrl-c",)


def test_mcp_mount_full_round_trip_with_valid_agent_token(tmp_path) -> None:
    # M4.5 regression: the SDK's Streamable HTTP app starts its session
    # manager from its own Starlette lifespan, but Starlette only runs the
    # lifespan of the top-level app, so a mounted inner app's lifespan was
    # never entered and every request past the auth gate failed with
    # "Task group is not initialized".  This exercises the whole path:
    # a real seeded AgentToken, initialize + tools/call through the app-level
    # mount, and a real tool result back.
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        allow_insecure_loopback=True,
        enable_docs=True,
        # TestClient sends ``Host: testserver``; name it like deployment
        # wiring names the agent-internal hosts (plan §16).
        agent_mcp_allowed_hosts=("testserver",),
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        raw_token = _seed_app_round_trip(client)
        headers = _initialize(client, raw_token)

        listing = _call_tool(client, headers, TermFlowToolName.LIST_PANES.value, {})
        assert listing.get("result") is not None
        panes = listing["result"]["structuredContent"]["panes"]
        assert {pane["pane_id"] for pane in panes} == {"%0", "%1"}
