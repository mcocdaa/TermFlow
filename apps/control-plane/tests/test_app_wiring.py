"""Production wiring tests for the runtime supervisor (plan §6.2.1 review fix).

Covers the composition root's supervisor wiring:

- production ``create_app`` wires a real ``SupervisorConnector`` whose
  runtime client fails closed (the reference compose profile has no
  deployment-owned runtime manager, so no runtime can be attested and every
  binding stays disabled);
- a pre-existing binding is left unmapped at startup with its fail-closed
  reason recorded in ``unavailable_bindings``;
- the MCP tool-call gate rejects every tool invocation whose binding runtime
  is not attested ready (fail closed), and admits calls when a test-injected
  supervisor attests the runtime;
- ``.env.example`` documents the container-deployment configuration path for
  the MCP allowed-hosts setting.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    SupervisorConnector,
)
from termflow_control_plane.plugins.agent_broker.api.mcp_server import (
    MCP_STREAMABLE_HTTP_PATH,
)
from termflow_control_plane.plugins.agent_broker.auth import SCOPE_TERMINAL_OBSERVE
from termflow_control_plane.plugins.protocol import RuntimeRef
from termflow_protocol.mcp import TermFlowToolName
from termflow_protocol.topology import PaneSnapshot, TopologySnapshot, WindowSnapshot

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"

ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[3] / ".env.example"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        admin_token=ADMIN_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        allow_insecure_loopback=True,
        enable_docs=True,
        # TestClient sends ``Host: testserver``; name it like deployment
        # wiring names the agent-internal hosts (plan §16).
        agent_mcp_allowed_hosts=("testserver",),
    )


async def _seed_binding(repositories: RepositoryBundle, *, runtime_epoch: int = 2):
    profile = await repositories.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    display_name = f"term-{uuid4().hex[:8]}"
    installation = await repositories.installations.create(digest_secret(f"computer-{uuid4().hex}"))
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
    scopes: tuple[str, ...] = (SCOPE_TERMINAL_OBSERVE,),
) -> str:
    raw_token = f"mcp-{uuid4().hex}"
    await repositories.agent_tokens.create(
        binding_id=binding.id,
        token_hash=hash_token(raw_token),
        scopes=scopes,
        expiry_epoch=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        binding_epoch=binding.runtime_epoch or 1,
    )
    return raw_token


class _PermissiveRuntimeSupervisor(SupervisorConnector):
    """Attest-anything supervisor for admitted-path tests.

    Subclasses the real connector so the create_app seam keeps its type; the
    register/accept gates are overridden to attest any runtime.  Only used by
    tests that exercise the admitted path.
    """

    def __init__(self) -> None:
        super().__init__(
            _DummyRuntimeClient(),
            lambda capability_ref, epoch: f"test-secret-{epoch}",
            health_poll_delay_seconds=0.0,
        )

    async def register(self, binding_id, runtime_ref, epoch, capability_ref) -> None:
        del binding_id, runtime_ref, epoch, capability_ref

    def accept_activation(self, runtime_ref, epoch) -> bool:
        del runtime_ref, epoch
        return True

    def accept_tool_call(self, runtime_ref, epoch) -> bool:
        del runtime_ref, epoch
        return True


class _DummyRuntimeClient:
    """Never reached: the permissive supervisor overrides every gate."""

    async def health(self, runtime_ref):
        raise AssertionError("unreachable")

    async def quiesce(self, runtime_ref, deadline):
        raise AssertionError("unreachable")

    async def restart(self, runtime_ref, epoch, capability_secret):
        raise AssertionError("unreachable")

    async def cleanup(self, runtime_ref):
        raise AssertionError("unreachable")


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


def _call_list_panes(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post(
        MCP_STREAMABLE_HTTP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": TermFlowToolName.LIST_PANES.value, "arguments": {}},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return _sse_payload(response.text)


# ---------------------------------------------------------------------------
# Production supervisor wiring.
# ---------------------------------------------------------------------------


def test_production_wiring_constructs_the_real_supervisor(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as client:
        supervisor = client.app.state.agent_runtime_supervisor
        assert isinstance(supervisor, SupervisorConnector)
        registry = client.app.state.agent_runtime_registry
        assert registry._supervisor is supervisor

        async def probe() -> str:
            health = await supervisor._client.health(RuntimeRef("runtime-1"))
            return health.status.value

        # The production client never fabricates readiness: health is UNKNOWN.
        assert client.portal.call(probe) == "unknown"


def test_preexisting_binding_fails_closed_at_startup(tmp_path) -> None:
    """A binding with runtime fields stays unmapped when nothing can attest it.

    The reference compose profile has no deployment-owned runtime manager,
    so register attestation fails closed: the binding is recorded in
    ``unavailable_bindings`` and its MCP tool calls are rejected.
    """
    settings = _settings(tmp_path)

    async def seed() -> str:
        database = Database(settings.database_url)
        await database.initialize()
        bundle = RepositoryBundle(database.session_factory)
        binding = await _seed_binding(bundle)
        raw_token = await _seed_token(bundle, binding)
        await database.dispose()
        return raw_token

    raw_token = asyncio.run(seed())

    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as client:
        registry = client.app.state.agent_runtime_registry
        # Exactly one binding existed at startup; it was refused fail closed.
        assert len(registry.unavailable_bindings) == 1
        (reason,) = registry.unavailable_bindings.values()
        assert "attestation failed" in reason
        assert list(registry._bindings) == []

        headers = _initialize(client, raw_token)
        payload = _call_list_panes(client, headers)
        # The tool gate rejected the call with a structured MCP error.
        assert "result" not in payload
        assert payload["error"]["code"] == -32603
        error_data = payload["error"]["data"]
        assert error_data["termflow_error_code"] == "internal_error"
        assert "not ready" in payload["error"]["message"]


def test_mcp_tool_gate_rejects_post_startup_binding_without_attestation(
    tmp_path,
) -> None:
    """A binding created after startup has no attested runtime: fail closed."""
    settings = _settings(tmp_path)
    app = create_app(settings=settings, database=Database(settings.database_url))
    with TestClient(app) as client:

        async def seed() -> str:
            bundle = RepositoryBundle(client.app.state.session_factory)
            binding = await _seed_binding(bundle)
            return await _seed_token(bundle, binding)

        raw_token = client.portal.call(seed)
        headers = _initialize(client, raw_token)
        payload = _call_list_panes(client, headers)

        assert "result" not in payload
        assert payload["error"]["code"] == -32603
        assert payload["error"]["data"]["termflow_error_code"] == "internal_error"


def test_mcp_tool_gate_admits_when_supervisor_attests_the_runtime(tmp_path) -> None:
    """With an injected attested supervisor the tool call passes the gate."""
    settings = _settings(tmp_path)
    app = create_app(
        settings=settings,
        database=Database(settings.database_url),
        agent_runtime_supervisor=_PermissiveRuntimeSupervisor(),
    )
    with TestClient(app) as client:

        async def seed() -> tuple[UUID, str]:
            bundle = RepositoryBundle(client.app.state.session_factory)
            binding = await _seed_binding(bundle)
            connection = await client.app.state.registry.register(binding.term_id)
            connection.topology = TopologySnapshot(
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
                        ],
                    ),
                ],
            )
            return binding.term_id, await _seed_token(bundle, binding)

        term_id, raw_token = client.portal.call(seed)
        headers = _initialize(client, raw_token)
        payload = _call_list_panes(client, headers)

        assert "error" not in payload, payload
        result = payload["result"]["structuredContent"]
        assert result["instance_id"] == str(term_id)
        assert [pane["pane_id"] for pane in result["panes"]] == ["%0"]


# ---------------------------------------------------------------------------
# Configuration-path documentation (.env.example).
# ---------------------------------------------------------------------------


def test_env_example_documents_container_mcp_allowed_hosts() -> None:
    text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "TERMFLOW_AGENT_MCP_ALLOWED_HOSTS" in text
    # The documented container-deployment value names B's internal host.
    assert "control-plane:8000" in text
    # No real-looking secrets may be committed.
    for forbidden in (
        "test-opencode-password",
        "test-opencode-model-key",
        "sk-ant-",
        "sk-proj-",
    ):
        assert forbidden not in text
