"""MCP server connector tests (plan §10, §22; tasks M4.4, M5.2).

The server is assembled from the handlers (``api/mcp_tools.py``) through
the official MCP Python SDK 2.0.0:

- exactly the eight served tools are registered (observe + writes);
- list/read/watch calls succeed through the SDK's in-memory client transport
  and over Streamable HTTP with a real seeded AgentToken;
- the write tools round-trip through the approval-gated command port with a
  tool_call_id derived from the MCP request id, and a call without a request
  id fails closed;
- invalid, revoked, expired, epoch-mismatched, and admin tokens fail closed
  at the HTTP auth gate (``TokenVerifier`` hook, plan §22);
- B-side guardrails (plan §10): request/result byte limits, tool timeout,
  per-binding concurrency/rate quotas, unknown tools fail closed;
- config drift against the pinned OpenCode allowlist fails startup
  (registered must equal the allowlist exactly since M5.2);
- the Streamable HTTP app mounts at ``/api/v1/agent/mcp`` only while
  ``agent_broker_enabled``;
- the mounted SDK app's lifespan (session manager) runs under the FastAPI
  lifespan: a full initialize + tools/call round trip with a real seeded
  AgentToken succeeds through the app-level mount (M4.5 regression).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.routing import Mount
from termflow_control_plane.app import create_app
from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentToken, Base
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    WatchContinuationService,
)
from termflow_control_plane.plugins.agent_broker.api.mcp_server import (
    MCP_STREAMABLE_HTTP_PATH,
    McpGuardrailConfig,
    ToolConfigDriftError,
    build_mcp_server,
    check_tool_config_drift,
    create_streamable_http_app,
    pinned_allowlist_from_fixture,
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
    PaneSendTextParams,
    PaneSendTextResult,
    PaneSummary,
    TermFlowToolName,
)
from termflow_protocol.topology import PaneSnapshot, TopologySnapshot, WindowSnapshot

#: The pinned OpenCode config fixture (M0.3) that allowlists the TermFlow tools.
OPENCODE_CONFIG_FIXTURE = (
    Path(__file__).parent / "fixtures" / "opencode" / "opencode-config.yaml"
)


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


async def _revoke_token(repositories: RepositoryBundle, raw_token: str) -> None:
    async with repositories.session_factory() as session:
        await session.execute(
            update(AgentToken)
            .where(AgentToken.token_hash == hash_token(raw_token))
            .values(revoked_at=datetime.now(UTC))
        )
        await session.commit()


async def _seed_conversation(repositories: RepositoryBundle, binding) -> object:
    return await repositories.agent_conversations.create(
        binding_id=binding.id,
        title="round trip",
    )


def _seed_app_token(client: TestClient) -> str:
    """Seed a fresh binding + AgentToken through the running app's repositories."""

    async def _seed() -> str:
        binding = await _seed_binding(client.app.state.repositories)
        return await _seed_token(client.app.state.repositories, binding)

    return client.portal.call(_seed)


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


def _build_http_app(server: MCPServer, **kwargs) -> TestClient:
    """TestClient over the SDK Streamable HTTP app with the test host allowed."""
    app = create_streamable_http_app(server, allowed_hosts=["testserver"], **kwargs)
    return TestClient(app)


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


# ---------------------------------------------------------------------------
# tool surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_registers_exactly_the_eight_served_tools(repositories) -> None:
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )

    tools = await server.list_tools()
    names = {tool.name for tool in tools}

    assert names == {
        TermFlowToolName.LIST_PANES.value,
        TermFlowToolName.PANE_READ.value,
        TermFlowToolName.PANE_SEND_TEXT.value,
        TermFlowToolName.PANE_SEND_KEYS.value,
        TermFlowToolName.WATCH_CREATE.value,
        TermFlowToolName.WATCH_LIST.value,
        TermFlowToolName.WATCH_GET.value,
        TermFlowToolName.WATCH_CANCEL.value,
    }
    assert len(tools) == 8


@pytest.mark.asyncio
async def test_write_tools_round_trip_over_in_memory_transport(repositories) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    await _allow_async(repositories, binding, "%1")
    fake = FakeObservation()
    commands = FakeCommands()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=commands,
    )

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


@pytest.mark.asyncio
async def test_write_tool_without_request_id_fails_closed(repositories) -> None:
    """A write tool call without an MCP request id cannot be approved (spec §6)."""
    from termflow_control_plane.plugins.agent_broker.api import mcp_server as server_module
    from termflow_control_plane.plugins.agent_broker.api.mcp_tools import handle_pane_send_text

    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    ports = server_module._ToolPorts(
        observation=FakeObservation(),
        continuation=WatchContinuationService(
            repositories.watches, repositories.agent_bindings
        ),
        commands=FakeCommands(),
        repositories=repositories,
    )
    principal = _principal(binding)

    with pytest.raises(MCPError) as caught:
        await server_module._run_guarded(
            name=TermFlowToolName.PANE_SEND_TEXT,
            principal=principal,
            params=PaneSendTextParams(
                pane_id="%0",
                request_key="req-1",
                conversation_id=conversation.id,
                text="make test",
            ),
            param_model=PaneSendTextParams,
            handler=handle_pane_send_text,
            ports=ports,
            quota=server_module.PerBindingQuota(McpGuardrailConfig()),
            config=McpGuardrailConfig(),
            ctx=None,
        )
    assert caught.value.data.get("termflow_error_code") == "invalid_request"


def test_guardrail_requires_wait_timeout_below_tool_timeout() -> None:
    with pytest.raises(ValueError, match="approval_wait_timeout_seconds"):
        McpGuardrailConfig(tool_timeout_seconds=10.0, approval_wait_timeout_seconds=10.0)
    with pytest.raises(ValueError, match="approval_wait_timeout_seconds"):
        McpGuardrailConfig(tool_timeout_seconds=10.0, approval_wait_timeout_seconds=11.0)
    with pytest.raises(ValueError):
        McpGuardrailConfig(approval_wait_timeout_seconds=0)
    with pytest.raises(ValueError):
        McpGuardrailConfig(approval_ttl_seconds=0)
    # Defaults satisfy the constraint: 25 < 30.
    assert McpGuardrailConfig().approval_wait_timeout_seconds == 25.0
    assert McpGuardrailConfig().approval_ttl_seconds == 300.0


@pytest.mark.asyncio
async def test_all_observe_tools_round_trip_over_in_memory_transport(repositories) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )

    async with Client(server) as client:
        async with principal_override(_principal(binding)):
            listed = await client.call_tool(
                TermFlowToolName.LIST_PANES.value, {}
            )
            assert listed.is_error is False
            panes = listed.structured_content["panes"]
            assert {pane["pane_id"] for pane in panes} == {"%0", "%1"}

            read = await client.call_tool(
                TermFlowToolName.PANE_READ.value,
                {"params": {"pane_id": "%0", "view": "viewport"}},
            )
            assert read.is_error is False
            assert read.structured_content["content"] == "bounded output"

            created = await client.call_tool(
                TermFlowToolName.WATCH_CREATE.value,
                {
                    "params": {
                        "pane_id": "%0",
                        "conversation_id": str(conversation.id),
                        "condition": {"kind": "output_contains", "match": "ready"},
                        "one_shot": True,
                        "intent": "wait for readiness",
                    }
                },
            )
            assert created.is_error is False
            watch_id = created.structured_content["watch_id"]

            listed_watches = await client.call_tool(
                TermFlowToolName.WATCH_LIST.value, {}
            )
            assert listed_watches.is_error is False
            watches = listed_watches.structured_content["watches"]
            assert [watch["watch_id"] for watch in watches] == [watch_id]

            detail = await client.call_tool(
                TermFlowToolName.WATCH_GET.value,
                {"params": {"watch_id": watch_id}},
            )
            assert detail.is_error is False
            assert detail.structured_content["watch"]["watch_id"] == watch_id

            cancelled = await client.call_tool(
                TermFlowToolName.WATCH_CANCEL.value,
                {"params": {"watch_id": watch_id}},
            )
            assert cancelled.is_error is False
            assert cancelled.structured_content["ok"] is True

    assert [read.pane_id for read in fake.reads] == ["%0"]


# ---------------------------------------------------------------------------
# AgentToken auth over Streamable HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_panes_and_pane_read_over_http_with_valid_agent_token(
    repositories,
) -> None:
    binding = await _seed_binding(repositories)
    await _allow_async(repositories, binding, "%0")
    raw_token = await _seed_token(repositories, binding)
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )
    with _build_http_app(server) as client:
        headers = _initialize(client, raw_token)

        listing = _call_tool(client, headers, TermFlowToolName.LIST_PANES.value, {})
        assert listing.get("result") is not None
        panes = listing["result"]["structuredContent"]["panes"]
        assert {pane["pane_id"] for pane in panes} == {"%0", "%1"}

        read = _call_tool(
            client,
            headers,
            TermFlowToolName.PANE_READ.value,
            {"params": {"pane_id": "%0", "view": "viewport"}},
        )
        assert read["result"]["structuredContent"]["content"] == "bounded output"
        assert read["result"]["structuredContent"]["instance_id"] == str(binding.term_id)


async def _unauthorized_statuses(repositories, token: str | None) -> int:
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )
    with _build_http_app(server) as client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
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
        return response.status_code


@pytest.mark.asyncio
async def test_missing_token_rejected(repositories) -> None:
    assert await _unauthorized_statuses(repositories, None) == 401


@pytest.mark.asyncio
async def test_invalid_token_rejected(repositories) -> None:
    assert await _unauthorized_statuses(repositories, "not-a-real-token") == 401


@pytest.mark.asyncio
async def test_admin_token_has_no_fallback(repositories) -> None:
    # Plan §10: the MCP token is never accepted as a generic admin credential.
    assert (
        await _unauthorized_statuses(
            repositories, "admin-token-that-is-long-enough-for-tests"
        )
        == 401
    )


@pytest.mark.asyncio
async def test_mismatched_host_header_rejected(repositories) -> None:
    # Plan §10: B explicitly configures and tests the allowed Host/Origin
    # values; a Host outside the agent-internal allowlist is refused before
    # any MCP processing (DNS-rebinding protection).
    binding = await _seed_binding(repositories)
    raw_token = await _seed_token(repositories, binding)
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )
    with _build_http_app(server) as client:
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
            headers={"host": "evil.example.com", "Authorization": f"Bearer {raw_token}"},
        )
        assert response.status_code == 421


@pytest.mark.asyncio
async def test_mismatched_origin_header_rejected(repositories) -> None:
    # Browsers are not a supported MCP transport (plan §10): a request from a
    # disallowed Origin is refused at the transport gate.
    binding = await _seed_binding(repositories)
    raw_token = await _seed_token(repositories, binding)
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )
    with _build_http_app(server) as client:
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
            headers={"origin": "https://evil.example", "Authorization": f"Bearer {raw_token}"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_revoked_token_rejected(repositories) -> None:
    binding = await _seed_binding(repositories)
    raw_token = await _seed_token(repositories, binding)
    await _revoke_token(repositories, raw_token)
    assert await _unauthorized_statuses(repositories, raw_token) == 401


@pytest.mark.asyncio
async def test_expired_token_rejected(repositories) -> None:
    binding = await _seed_binding(repositories)
    raw_token = await _seed_token(repositories, binding, expires_in=timedelta(hours=-1))
    assert await _unauthorized_statuses(repositories, raw_token) == 401


@pytest.mark.asyncio
async def test_epoch_mismatched_token_rejected(repositories) -> None:
    binding = await _seed_binding(repositories, runtime_epoch=2)
    raw_token = await _seed_token(repositories, binding, binding_epoch=1)
    assert await _unauthorized_statuses(repositories, raw_token) == 401


@pytest.mark.asyncio
async def test_token_without_observe_scope_rejected(repositories) -> None:
    binding = await _seed_binding(repositories)
    raw_token = await _seed_token(repositories, binding, scopes=(SCOPE_TERMINAL_WRITE,))
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )
    with _build_http_app(server) as client:
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
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# B-side guardrails (plan §10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_byte_limit_enforced(repositories) -> None:
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )
    with _build_http_app(server, max_request_bytes=1024) as client:
        binding = await _seed_binding(repositories)
        raw_token = await _seed_token(repositories, binding)
        headers = _initialize(client, raw_token)
        big_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "termflow_pane_read", "arguments": {"pad": "x" * 5000}},
            }
        )
        response = client.post(
            MCP_STREAMABLE_HTTP_PATH,
            content=big_body,
            headers=headers,
        )
        assert response.status_code == 413


@pytest.mark.asyncio
async def test_result_byte_limit_enforced(repositories) -> None:
    binding = await _seed_binding(repositories)
    await _allow_async(repositories, binding, "%0")
    raw_token = await _seed_token(repositories, binding)
    fake = FakeObservation(content="x" * 4096)
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
guardrails=McpGuardrailConfig(max_result_bytes=1024),
    )
    with _build_http_app(server) as client:
        headers = _initialize(client, raw_token)
        envelope = _call_tool(
            client,
            headers,
            TermFlowToolName.PANE_READ.value,
            {"params": {"pane_id": "%0", "view": "viewport"}},
        )

    error = envelope["error"]
    assert error["data"]["termflow_error_code"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_tool_timeout_enforced(repositories) -> None:
    binding = await _seed_binding(repositories)
    await _allow_async(repositories, binding, "%0")
    fake = FakeObservation()
    fake.read_delay = 1.0
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        guardrails=McpGuardrailConfig(
            tool_timeout_seconds=0.05,
            approval_wait_timeout_seconds=0.02,
        ),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )

    async with Client(server) as client:
        async with principal_override(_principal(binding)):
            with pytest.raises(MCPError) as caught:
                await client.call_tool(
                    TermFlowToolName.PANE_READ.value,
                    {"params": {"pane_id": "%0", "view": "viewport"}},
                )
    assert "timed out" in str(caught.value)


@pytest.mark.asyncio
async def test_concurrency_quota_serializes_per_binding(repositories) -> None:
    binding = await _seed_binding(repositories)
    await _allow_async(repositories, binding, "%0")
    fake = FakeObservation()
    fake.read_delay = 0.15
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        guardrails=McpGuardrailConfig(
            max_concurrent_per_binding=1,
            rate_per_second=100.0,
            rate_burst=100,
        ),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )

    async with Client(server) as client:
        async with principal_override(_principal(binding)):
            results = await asyncio.gather(
                *[
                    client.call_tool(
                        TermFlowToolName.PANE_READ.value,
                        {"params": {"pane_id": "%0", "view": "viewport"}},
                    )
                    for _ in range(3)
                ]
            )
    assert all(result.is_error is False for result in results)
    # The fake never ran more than one read at a time for this binding.
    assert fake.read_max_active == 1
    assert len(fake.reads) == 3


@pytest.mark.asyncio
async def test_concurrency_quota_is_per_binding(repositories) -> None:
    binding_a = await _seed_binding(repositories)
    binding_b = await _seed_binding(repositories)
    await _allow_async(repositories, binding_a, "%0")
    await _allow_async(repositories, binding_b, "%0")
    fake = FakeObservation()
    fake.read_delay = 0.15
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        guardrails=McpGuardrailConfig(
            max_concurrent_per_binding=1,
            rate_per_second=100.0,
            rate_burst=100,
        ),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )

    async def _read(binding) -> object:
        async with principal_override(_principal(binding)):
            return await client.call_tool(
                TermFlowToolName.PANE_READ.value,
                {"params": {"pane_id": "%0", "view": "viewport"}},
            )

    async with Client(server) as client:
        results = await asyncio.gather(_read(binding_a), _read(binding_b))
    assert all(result.is_error is False for result in results)
    # Different bindings run concurrently; only same-binding calls serialize.
    assert fake.read_max_active == 2


@pytest.mark.asyncio
async def test_rate_quota_delays_followup_calls(repositories) -> None:
    binding = await _seed_binding(repositories)
    await _allow_async(repositories, binding, "%0")
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        guardrails=McpGuardrailConfig(
            max_concurrent_per_binding=4,
            rate_per_second=10.0,
            rate_burst=1,
        ),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )

    async def _read() -> object:
        async with principal_override(_principal(binding)):
            return await client.call_tool(
                TermFlowToolName.PANE_READ.value,
                {"params": {"pane_id": "%0", "view": "viewport"}},
            )

    async with Client(server) as client:
        started = time.monotonic()
        await _read()
        await _read()
        elapsed = time.monotonic() - started
    # Burst of 1 at 10 tokens/s: the second call must wait ~0.1s.
    assert elapsed >= 0.08


@pytest.mark.asyncio
async def test_unknown_tool_fails_closed(repositories) -> None:
    fake = FakeObservation()
    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator

    server = build_mcp_server(
        observation=fake,
        continuation=continuation,
        policy_checker=repositories,
        token_auth=AgentTokenAuthenticator(repositories),
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        commands=FakeCommands(),
    )

    async with Client(server) as client:
        result = await client.call_tool("termflow_exec_shell", {})
    assert result.is_error is True
    assert "Unknown tool" in result.content[0].text


# ---------------------------------------------------------------------------
# config drift (plan §10, M0.3)
# ---------------------------------------------------------------------------


def test_pinned_allowlist_fixture_parses_to_eight_tools() -> None:
    allowlist = pinned_allowlist_from_fixture(OPENCODE_CONFIG_FIXTURE.read_text())
    assert allowlist == {
        TermFlowToolName.LIST_PANES.value,
        TermFlowToolName.PANE_READ.value,
        TermFlowToolName.PANE_SEND_TEXT.value,
        TermFlowToolName.PANE_SEND_KEYS.value,
        TermFlowToolName.WATCH_CREATE.value,
        TermFlowToolName.WATCH_LIST.value,
        TermFlowToolName.WATCH_GET.value,
        TermFlowToolName.WATCH_CANCEL.value,
    }


def test_config_drift_accepts_the_full_served_surface() -> None:
    allowlist = pinned_allowlist_from_fixture(OPENCODE_CONFIG_FIXTURE.read_text())
    # Since M5.2 the deferred set is empty: registered == allowlist exactly.
    check_tool_config_drift(allowlist, allowlist)


def test_config_drift_rejects_unreviewed_tool() -> None:
    allowlist = pinned_allowlist_from_fixture(OPENCODE_CONFIG_FIXTURE.read_text())
    registered = set(allowlist - {"termflow_pane_send_text", "termflow_pane_send_keys"})
    registered.add("termflow_exec_shell")
    with pytest.raises(ToolConfigDriftError):
        check_tool_config_drift(registered, allowlist)


def test_config_drift_rejects_missing_served_tool() -> None:
    allowlist = pinned_allowlist_from_fixture(OPENCODE_CONFIG_FIXTURE.read_text())
    registered = set(allowlist) - {TermFlowToolName.WATCH_CANCEL.value}
    with pytest.raises(ToolConfigDriftError):
        check_tool_config_drift(registered, allowlist)


def test_config_drift_rejects_missing_write_tool() -> None:
    allowlist = pinned_allowlist_from_fixture(OPENCODE_CONFIG_FIXTURE.read_text())
    # With the deferred set empty, a missing write tool is drift just like a
    # missing observe tool (registered must equal the allowlist exactly).
    registered = set(allowlist) - {TermFlowToolName.PANE_SEND_KEYS.value}
    with pytest.raises(ToolConfigDriftError):
        check_tool_config_drift(registered, allowlist)


def test_config_drift_rejects_allowlist_losing_a_tool() -> None:
    # The server surface is the source of truth here: if the pinned config no
    # longer allowlists a tool the server still serves, startup must fail.
    allowlist = set(
        pinned_allowlist_from_fixture(OPENCODE_CONFIG_FIXTURE.read_text())
    ) - {TermFlowToolName.PANE_READ.value}
    registered = set(
        pinned_allowlist_from_fixture(OPENCODE_CONFIG_FIXTURE.read_text())
    )
    with pytest.raises(ToolConfigDriftError):
        check_tool_config_drift(registered, allowlist)


# ---------------------------------------------------------------------------
# app.py mounting (plan §3.4: only while agent_broker_enabled)
# ---------------------------------------------------------------------------


def test_mcp_mount_present_when_plugin_enabled(tmp_path) -> None:
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        allow_insecure_loopback=True,
        enable_docs=True,
    )
    app = create_app(settings=settings)
    assert any(
        isinstance(route, Mount) and route.path == MCP_STREAMABLE_HTTP_PATH
        for route in app.routes
    )
    with TestClient(app) as client:
        # The auth gate is the first line of defense: unauthenticated requests
        # to the mounted MCP endpoint are rejected before any MCP processing.
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
        )
        assert response.status_code == 401


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


def test_mcp_mount_absent_when_plugin_disabled(tmp_path) -> None:
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        allow_insecure_loopback=True,
        enable_docs=True,
        agent_broker_enabled=False,
    )
    app = create_app(settings=settings)
    assert not any(
        isinstance(route, Mount) and route.path == MCP_STREAMABLE_HTTP_PATH
        for route in app.routes
    )
    with TestClient(app) as client:
        # No MCP endpoint exists; the reserved API root is not web-served.
        response = client.get(MCP_STREAMABLE_HTTP_PATH)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# app.py settings plumbing (plan §10: the drift guard and host guard are
# reachable from deployment settings, not only through direct unit calls)
# ---------------------------------------------------------------------------


def _app_initialize_body() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "mcp-test", "version": "1"},
        },
    }


def test_app_startup_fails_on_config_drift_when_opencode_config_path_configured(
    tmp_path,
) -> None:
    # Plan §10/M0.3: with settings.opencode_config_path configured, a
    # mismatched pinned OpenCode config must refuse startup through the real
    # app factory (the drift guard is reachable in the deployed app, not only
    # through direct check_tool_config_drift calls).
    drifted = tmp_path / "opencode-config.yaml"
    drifted.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "permission": {
                    "*": "deny",
                    "termflow_list_panes": "ask",
                    "termflow_pane_read": "ask",
                    "termflow_pane_send_text": "ask",
                    "termflow_pane_send_keys": "ask",
                    "termflow_watch_create": "ask",
                    "termflow_watch_list": "ask",
                    "termflow_watch_get": "ask",
                    # termflow_watch_cancel deliberately missing -> drift
                },
                "mcp": {},
            }
        )
    )
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        allow_insecure_loopback=True,
        opencode_config_path=str(drifted),
    )
    app = create_app(settings=settings)
    with pytest.raises(ToolConfigDriftError):
        with TestClient(app):
            pass


def test_app_startup_succeeds_with_matching_opencode_config(tmp_path) -> None:
    # The pinned fixture allowlist matches the observe surface: the app must
    # start normally when the configured config is the frozen one (M0.3).
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        allow_insecure_loopback=True,
        opencode_config_path=str(OPENCODE_CONFIG_FIXTURE),
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_app_built_mcp_transport_honors_configured_agent_network_hosts(tmp_path) -> None:
    # Plan §16: deployment expresses the agent-internal hosts through
    # settings.agent_mcp_allowed_hosts; a configured agent-network host passes
    # the transport gate while an unlisted host is refused with 421.  The app
    # factory builds the inner Streamable HTTP app from Settings, so this is
    # the real wiring, not a direct create_streamable_http_app call.  The
    # requests go through the app-level mount: the session manager runs under
    # the FastAPI lifespan (M4.5), so the transport gate is reachable there.
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        allow_insecure_loopback=True,
        agent_mcp_allowed_hosts=("agent-network.internal",),
    )
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as client:
        raw_token = _seed_app_token(client)
        bearer = {"Authorization": f"Bearer {raw_token}"}
        allowed = client.post(
            MCP_STREAMABLE_HTTP_PATH,
            json=_app_initialize_body(),
            headers={**bearer, "host": "agent-network.internal"},
        )
        assert allowed.status_code == 200
        refused = client.post(
            MCP_STREAMABLE_HTTP_PATH,
            json=_app_initialize_body(),
            headers={**bearer, "host": "evil.example.com"},
        )
        assert refused.status_code == 421
