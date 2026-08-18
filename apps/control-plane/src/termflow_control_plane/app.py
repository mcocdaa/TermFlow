"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_REQUEST
from sqlalchemy.exc import SQLAlchemyError
from starlette._utils import get_route_path
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Match, Mount
from starlette.types import Receive, Scope, Send
from termflow_protocol import (
    ErrorDetail,
    ErrorEnvelope,
    HealthResponse,
    InstancePresencePayload,
    MessageType,
    WireMessage,
)
from termflow_protocol.mcp import TermFlowErrorCode

from termflow_control_plane import __version__
from termflow_control_plane.api.agent_admin import router as agent_admin_router
from termflow_control_plane.api.agent_approvals import router as agent_approvals_router
from termflow_control_plane.api.agent_capabilities import router as agent_capabilities_router
from termflow_control_plane.api.agent_conversations import router as agent_conversations_router
from termflow_control_plane.api.agent_stream import router as agent_stream_router
from termflow_control_plane.api.bridge import router as bridge_router
from termflow_control_plane.api.clients import router as clients_router
from termflow_control_plane.api.computers import router as computers_router
from termflow_control_plane.api.dashboard import router as dashboard_router
from termflow_control_plane.api.enrollment import router as enrollment_router
from termflow_control_plane.api.events import router as events_router
from termflow_control_plane.api.instances import router as instances_router
from termflow_control_plane.api.oauth import router as oauth_router
from termflow_control_plane.api.security import cli_router
from termflow_control_plane.api.security import router as security_router
from termflow_control_plane.api.sessions import router as sessions_router
from termflow_control_plane.api.terminal import router as terminal_router_api
from termflow_control_plane.api.terms import router as terms_router
from termflow_control_plane.auth.audit import AuthenticationAudit
from termflow_control_plane.auth.dpop import DpopVerifier
from termflow_control_plane.auth.master_key import resolve_totp_master_key
from termflow_control_plane.auth.rate_limit import AuthRateLimiter
from termflow_control_plane.auth.secret_box import AesGcmSecretBox
from termflow_control_plane.auth.service import AuthenticationRejected, AuthenticationService
from termflow_control_plane.auth.sessions import BrowserSessionStore, browser_cookie_policy
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.connections.registry import LiveConnection, LiveInstanceRegistry
from termflow_control_plane.connections.terminal_hub import TerminalHub
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.approval_audit import (
    ApprovalAuditWriter,
)
from termflow_control_plane.plugins.agent_broker.agent.command_service import (
    CommandRouterGateway,
    CommandService,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import ApprovalPolicy
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    AgentRuntimeRegistry,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    SecretUnavailableError,
    SupervisorConnector,
    UnknownRuntimeError,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import (
    AGENT_STREAM_QUEUE_SIZE,
    AgentStreamHub,
)
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    ObservationService,
    WatchContinuationService,
)
from termflow_control_plane.plugins.agent_broker.agent.watches import (
    ObservationCursorStore,
    WatchEngine,
)
from termflow_control_plane.plugins.agent_broker.api.mcp_server import (
    MCP_STREAMABLE_HTTP_PATH,
    McpGuardrailConfig,
    build_mcp_server,
    check_tool_config_drift,
    create_streamable_http_app,
    pinned_allowlist_from_fixture,
    principal_from_access_token,
)
from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator
from termflow_control_plane.plugins.agent_broker.plugin import (
    AgentBrokerPlugin,
    build_watch_trigger_sink,
    run_agent_recovery,
)
from termflow_control_plane.plugins.context import build_feature_context
from termflow_control_plane.plugins.protocol import (
    AuthPort,
    BackendOperationOutcome,
    BackendOperationResult,
    CapabilityRef,
    DrainStatus,
    LifecyclePort,
    RuntimeHealth,
    RuntimeRef,
    RuntimeStatus,
    TerminalCommandPort,
    TerminalObservationPort,
    TermPort,
    UnitOfWorkFactory,
)
from termflow_control_plane.plugins.registry import FeatureRegistry
from termflow_control_plane.routing.router import CommandRouter
from termflow_control_plane.routing.terminal_audit import TerminalAuditWriter
from termflow_control_plane.routing.terminal_router import TerminalRouter
from termflow_control_plane.web import install_web_hosting

logger = logging.getLogger(__name__)

_AUTHENTICATION_EPOCH_POLL_SECONDS = 1.0


class _AgentMcpInner:
    """Resolves the lifespan-built MCP app and dispatches to it (fail closed).

    ``_AgentMcpMount`` carries this as its ``app``; both Starlette's and
    FastAPI's route dispatch call it with the request scope, so requests that
    arrive before the lifespan builds the inner app get a 404.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        root_app = scope.get("app")
        inner = getattr(getattr(root_app, "state", None), "agent_mcp_app", None)
        if inner is None:
            response = Response("Agent Broker MCP endpoint is unavailable", status_code=404)
            await response(scope, receive, send)
            return
        await inner(scope, receive, send)


class _AgentMcpMount(Mount):
    """Mount serving the Agent Broker MCP app under ``/api/v1/agent/mcp``.

    Starlette's ``Mount`` only matches ``path/...`` (a trailing slash is
    required), and the SPA fallback would otherwise shadow the bare path with
    a ``405``.  This subclass also matches the exact mount path - with or
    without the trailing slash - and normalizes the child scope so the inner
    Streamable HTTP app (built at ``path="/"``) always sees its own ``/``
    route.  Requests before the lifespan builds the inner app fail closed
    with 404.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path, app=_AgentMcpInner())

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] in ("http", "websocket"):
            route_path = get_route_path(scope)
            if route_path == self.path or route_path == f"{self.path}/":
                root_path = scope.get("root_path", "")
                return Match.FULL, {
                    "path": root_path + self.path + "/",
                    "path_params": {},
                    "app_root_path": scope.get("app_root_path", root_path),
                    "root_path": root_path + self.path,
                    "endpoint": self.app,
                }
        return super().matches(scope)


async def _build_agent_mcp_app(app: FastAPI, settings: Settings) -> Starlette:
    """Assemble the MCP server once the repositories exist.

    B owns the security checks around the SDK (plan §10): bearer auth through
    ``require_agent_token``, byte limits, per-binding quotas, and the pinned
    tool-allowlist drift guard.  The drift guard reads the deployment-owned
    frozen OpenCode config when ``opencode_config_path`` is configured and
    refuses startup on any mismatch (plan §10, M0.3).

    The write tools run through the approval-gated :class:`CommandService`
    (M5.2): its gateway is the production :class:`CommandRouterGateway` over
    the app's :class:`CommandRouter`, its ledger comes from the observation
    cursor store, and it shares the composition-root approval policy and
    audit writer.

    Every served tool port is wrapped in the supervisor's fail-closed
    tool-call gate (plan §6.2.1): a binding-scoped MCP call is admitted only
    while its runtime is attested ready at the token's epoch.

    The SDK app is built at ``path="/"``: Starlette mounts rewrite the child
    scope's route path, so the mounted app sees its own ``/`` route while the
    public path stays ``/api/v1/agent/mcp``.
    """
    guardrails = McpGuardrailConfig(
        approval_wait_timeout_seconds=settings.agent_approval_wait_timeout_seconds,
        approval_ttl_seconds=settings.agent_approval_ttl_seconds,
    )
    commands = CommandService(
        repositories=app.state.repositories,
        sessions=app.state.session_factory,
        gateway=CommandRouterGateway(app.state.command_router),
        observation=ObservationService(
            app.state.registry,
            cursor_store=ObservationCursorStore(app.state.session_factory),
        ),
        approval_wait_timeout_seconds=settings.agent_approval_wait_timeout_seconds,
        approval_ttl_seconds=settings.agent_approval_ttl_seconds,
        audit=app.state.approval_audit,
        policy=app.state.approval_policy,
    )
    app.state.command_service = commands
    # Plan §6.2.1 fail-closed gate: every MCP tool invocation - observe,
    # watch, and write alike - is admitted only while the binding's runtime
    # is attested ready at the token's epoch.  The ports are wrapped here so
    # the api/mcp_server assembly stays untouched.
    tool_gate = _build_mcp_tool_gate(
        repositories=app.state.repositories,
        supervisor=getattr(app.state, "agent_runtime_supervisor", None),
    )
    server = build_mcp_server(
        observation=_SupervisorGatedToolPort(
            ObservationService(
                app.state.registry,
                cursor_store=ObservationCursorStore(app.state.session_factory),
            ),
            tool_gate,
        ),
        continuation=_SupervisorGatedToolPort(
            WatchContinuationService(
                app.state.repositories.watches,
                app.state.repositories.agent_bindings,
            ),
            tool_gate,
        ),
        policy_checker=app.state.repositories,
        token_auth=AgentTokenAuthenticator(app.state.repositories),
        sessions=app.state.session_factory,
        commands=_SupervisorGatedToolPort(commands, tool_gate),
        guardrails=guardrails,
    )
    config_path = settings.opencode_config_path
    if config_path:
        allowlist = pinned_allowlist_from_fixture(
            await asyncio.to_thread(lambda: Path(config_path).read_text(encoding="utf-8"))
        )
        registered = {tool.name for tool in await server.list_tools()}
        check_tool_config_drift(registered, allowlist)
    return create_streamable_http_app(
        server,
        path="/",
        max_request_bytes=settings.agent_mcp_max_request_bytes,
        allowed_hosts=settings.agent_mcp_allowed_hosts,
    )


def _request_id(request: Request) -> UUID:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else uuid4()


async def expire_stale_connections(
    registry: LiveInstanceRegistry,
    event_hub: EventHub,
    *,
    now: datetime,
    offline_after_seconds: int,
) -> list[LiveConnection]:
    expired = await registry.expire_before(now - timedelta(seconds=offline_after_seconds))
    for connection in expired:
        payload = InstancePresencePayload(status="offline", observed_at=now)
        await event_hub.publish(
            WireMessage(
                type=MessageType.INSTANCE_OFFLINE,
                instance_id=connection.instance_id,
                payload=payload.model_dump(mode="json"),
            )
        )
    return expired


async def _heartbeat_expiry_loop(
    registry: LiveInstanceRegistry,
    event_hub: EventHub,
    settings: Settings,
) -> None:
    while True:
        await asyncio.sleep(1)
        await expire_stale_connections(
            registry,
            event_hub,
            now=datetime.now(UTC),
            offline_after_seconds=settings.offline_after_seconds,
        )


async def _browser_session_expiry_loop(store: BrowserSessionStore) -> None:
    while True:
        await asyncio.sleep(1)
        store.prune_expired()


async def _authentication_epoch_loop(
    repositories: RepositoryBundle,
    browser_sessions: BrowserSessionStore,
    terminal_hub: TerminalHub,
    event_hub: EventHub,
    agent_stream_hub: AgentStreamHub,
    stop: asyncio.Event,
) -> None:
    """Observe reset commands running in another process through persisted epoch state."""

    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=_AUTHENTICATION_EPOCH_POLL_SECONDS,
            )
        except TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            state = await repositories.auth_state.get()
            browser_sessions.synchronize_epoch(state.epoch)
            await terminal_hub.synchronize_epoch(state.epoch)
            await event_hub.synchronize_epoch(state.epoch)
            # Agent streams are bound to the same authentication epoch, so a
            # credential rotation closes them even while idle (plan §13.1).
            await agent_stream_hub.synchronize_epoch(state.epoch)
        except SQLAlchemyError:
            logger.exception("Authentication epoch poll failed; retrying")


async def _verify_oauth_totp(service: AuthenticationService, code: str) -> bool:
    """Convert unavailable or invalid TOTP state into a closed authorization denial."""

    try:
        return await service.verify_fresh_totp(code)
    except AuthenticationRejected:
        return False


def _unimplemented_port(_port: object) -> Any:
    """Return a placeholder object for a BFeatureContext port not yet implemented.

    The ``_port`` argument is documentation-only: it names the port this
    placeholder stands in for.  The composition root must not invent fake
    implementations for ports whose milestones have not landed; an explicitly
    labeled placeholder keeps the wiring honest.  ``Any`` is required because
    Protocol classes cannot be used as ``type[...]`` arguments under mypy
    strict.
    """
    return object()


class _UnattestedRuntimeClient:
    """Production ``RuntimeClient`` for deployments without a runtime manager.

    Plan §6.2.1: the reference Compose profile ships no deployment-owned
    runtime manager, and B must never attest a runtime it cannot observe
    (the runtime_supervisor connector's production wiring to such a manager
    is out of 0.2.0 scope).  Every health probe therefore reports
    ``UNKNOWN`` so ``SupervisorConnector.register`` fails closed: no binding
    is admitted and MCP tool calls stay gated until a deployment injects a
    real runtime-manager client.  This is honest fail-closed wiring, never a
    fabricated "ready" attestation.
    """

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth:
        del runtime_ref  # every runtime is unattested
        return RuntimeHealth(
            status=RuntimeStatus.UNKNOWN,
            epoch=1,
            observed_at=datetime.now(UTC),
            detail=(
                "no deployment-owned runtime manager is wired; runtime "
                "attestation fails closed (plan §6.2.1)"
            ),
        )

    async def quiesce(self, runtime_ref: RuntimeRef, deadline: datetime) -> DrainStatus:
        del runtime_ref, deadline
        # A drain that can never complete blocks activation (fail closed).
        return DrainStatus.DRAIN_TIMEOUT

    async def restart(
        self, runtime_ref: RuntimeRef, epoch: int, capability_secret: str
    ) -> RuntimeHealth:
        del epoch, capability_secret
        raise UnknownRuntimeError(
            f"runtime {runtime_ref} cannot be restarted: no deployment-owned "
            "runtime manager is wired (plan §6.2.1)"
        )

    async def cleanup(self, runtime_ref: RuntimeRef) -> BackendOperationResult:
        return BackendOperationResult(
            outcome=BackendOperationOutcome.UNKNOWN,
            message=(
                f"runtime {runtime_ref} was never attested: no "
                "deployment-owned runtime manager is wired; nothing to clean up"
            ),
        )


def _unavailable_secret_provider(capability_ref: CapabilityRef, epoch: int) -> str:
    raise SecretUnavailableError(
        f"no capability secret is available for {capability_ref} epoch {epoch}: "
        "no deployment-owned runtime manager is wired (plan §6.2.1)"
    )


def _build_production_runtime_supervisor() -> SupervisorConnector:
    """The composition root's production ``AgentRuntimeSupervisor``.

    Built from the fail-closed ``_UnattestedRuntimeClient`` because the
    reference deployment has no deployment-owned runtime manager to attest
    runtimes (plan §6.2.1).  Deployments that operate a real runtime
    manager replace this seam with a client wired to that manager; until
    then every binding activation and MCP tool call fails closed.
    """
    return SupervisorConnector(
        _UnattestedRuntimeClient(),
        _unavailable_secret_provider,
    )


def _build_mcp_tool_gate(
    *,
    repositories: RepositoryBundle,
    supervisor: SupervisorConnector | None,
) -> Callable[[], Awaitable[None]]:
    """Build the supervisor's fail-closed MCP tool-call admission gate.

    Plan §6.2.1: a binding-scoped MCP call is admitted only while its
    runtime is attested ready at the token's epoch.  The gate resolves the
    verified binding identity from the SDK auth context, looks up the
    binding's runtime fields, and applies ``accept_tool_call``; any doubt -
    missing token, missing runtime fields, a stale epoch, an unwired
    supervisor, a rejecting or raising gate - fails closed.
    """

    async def gate() -> None:
        access_token = get_access_token()
        if access_token is None:
            raise MCPError(
                code=INVALID_REQUEST,
                message="an authenticated agent token is required",
            )
        principal = principal_from_access_token(access_token)
        binding = await repositories.agent_bindings.get_by_id(principal.binding_id)
        if binding is None or not binding.runtime_ref or binding.runtime_epoch is None:
            raise MCPError(
                code=INTERNAL_ERROR,
                message="the binding has no admitted runtime; the tool call fails closed",
                data={"termflow_error_code": TermFlowErrorCode.INTERNAL_ERROR.value},
            )
        if principal.runtime_epoch != binding.runtime_epoch:
            raise MCPError(
                code=INTERNAL_ERROR,
                message="the token carries a stale runtime epoch; the tool call fails closed",
                data={"termflow_error_code": TermFlowErrorCode.INTERNAL_ERROR.value},
            )
        if supervisor is None:
            raise MCPError(
                code=INTERNAL_ERROR,
                message="no runtime supervisor is wired; the tool call fails closed",
                data={"termflow_error_code": TermFlowErrorCode.INTERNAL_ERROR.value},
            )
        try:
            accepted = supervisor.accept_tool_call(
                RuntimeRef(binding.runtime_ref.strip()), binding.runtime_epoch
            )
        except Exception:
            logger.exception("Agent MCP tool gate raised; failing closed")
            accepted = False
        if not accepted:
            raise MCPError(
                code=INTERNAL_ERROR,
                message="the binding runtime is not ready; the tool call fails closed",
                data={"termflow_error_code": TermFlowErrorCode.INTERNAL_ERROR.value},
            )

    return gate


class _SupervisorGatedToolPort:
    """Apply the MCP tool-call gate at the boundary of a tool port.

    The MCP handlers receive the observation/continuation/commands ports as
    dependencies; wrapping them here applies the supervisor's ready/epoch
    admission to every tool invocation without touching the
    ``api/mcp_server`` assembly (owned elsewhere).  ``__getattr__`` keeps the
    proxy transparent for attribute access and non-callable members.
    """

    def __init__(
        self,
        delegate: object,
        gate: Callable[[], Awaitable[None]],
    ) -> None:
        self._delegate = delegate
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._delegate, name)
        if not callable(attribute):
            return attribute

        async def gated(*args: Any, **kwargs: Any) -> Any:
            await self._gate()
            return await attribute(*args, **kwargs)

        return gated


def create_app(
    *,
    settings: Settings,
    database: Database | None = None,
    agent_runtime_supervisor: SupervisorConnector | None = None,
) -> FastAPI:
    """Assemble the FastAPI application.

    ``agent_runtime_supervisor`` is the composition-root test seam: production
    callers pass ``None`` and the lifespan wires the fail-closed production
    supervisor (:func:`_build_production_runtime_supervisor`); tests inject a
    fake to exercise attested-runtime paths.
    """
    active_database = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await active_database.initialize()
        app.state.repositories = RepositoryBundle(active_database.session_factory)
        purge_now = datetime.now(UTC)
        purged = await app.state.repositories.purge_expired(now=purge_now)
        auth_state = await app.state.repositories.auth_state.get()
        app.state.browser_sessions.synchronize_epoch(auth_state.epoch)
        await app.state.terminal_hub.synchronize_epoch(auth_state.epoch)
        await app.state.event_hub.synchronize_epoch(auth_state.epoch)
        await app.state.agent_stream_hub.synchronize_epoch(auth_state.epoch)
        master_key = resolve_totp_master_key(settings)
        secret_box = (
            AesGcmSecretBox(master_key, key_version=settings.totp_master_key_version)
            if master_key is not None
            else None
        )
        app.state.authentication_service = AuthenticationService(
            app.state.repositories,
            settings,
            secret_box=secret_box,
        )
        app.state.oauth_totp_verifier = lambda code: _verify_oauth_totp(
            app.state.authentication_service,
            code,
        )
        app.state.auth_audit = AuthenticationAudit(
            getattr(app.state.repositories, "auth_audit", None)
        )
        app.state.command_router = CommandRouter(
            registry=app.state.registry,
            audit=app.state.repositories.audit,
            settings=settings,
        )
        app.state.terminal_audit = TerminalAuditWriter(app.state.repositories.audit)
        app.state.terminal_audit.start()
        # M5.2 approval wiring: one shared audit writer and policy serve the
        # approval REST API, the binding-status revocation path, and the MCP
        # CommandService (spec §7: composition root owns the shared instance).
        app.state.approval_audit = ApprovalAuditWriter(app.state.repositories)
        app.state.approval_policy = ApprovalPolicy(
            app.state.repositories,
            app.state.session_factory,
            audit=app.state.approval_audit,
        )
        # Expired approvals sweep through the policy (spec §5/§7), never the
        # repository directly: the policy records one ``expired`` audit event
        # per swept row.  The count joins the startup purge report.
        purged["approvals"] = await app.state.approval_policy.expire_pending(now=purge_now)
        if any(purged.values()):
            logger.info("Purged expired persistence rows: %s", purged)
        app.state.terminal_router = TerminalRouter(
            registry=app.state.registry,
            hub=app.state.terminal_hub,
            audit=app.state.terminal_audit,
            capability_wait_seconds=settings.command_timeout_seconds,
            resume_grace_seconds=settings.terminal_resume_grace_seconds,
        )
        # Plan §6.2.1: the runtime supervisor is a required control-plane
        # port.  Production wires the real SupervisorConnector (fail-closed
        # when no deployment-owned runtime manager can attest runtimes);
        # tests may inject a fake through create_app.
        runtime_supervisor = agent_runtime_supervisor or _build_production_runtime_supervisor()
        app.state.agent_runtime_supervisor = runtime_supervisor
        feature_context = build_feature_context(
            # No real port implementations exist yet, so every port except
            # the runtime supervisor is an explicitly labeled placeholder
            # until its milestone lands; real terms/persistence adapters
            # arrive with the M1.3+ services.
            auth=_unimplemented_port(AuthPort),  # AuthenticationService lacks AuthPort
            terms=_unimplemented_port(TermPort),  # real TermPort adapter lands with M1.3+
            observation=_unimplemented_port(TerminalObservationPort),  # lands with M2
            commands=_unimplemented_port(TerminalCommandPort),  # lands with M5
            persistence=_unimplemented_port(UnitOfWorkFactory),  # real adapter lands with M1.3+
            lifecycle=_unimplemented_port(LifecyclePort),  # lands with plugin background tasks
            runtime=runtime_supervisor,
        )
        app.state.feature_context = feature_context
        # Deterministic restart recovery (plan §17: B restarts): fence stale
        # inbox claims, mark stuck runs unknown, then retry pending cleanup
        # tombstones.  Fail-safe by design: errors are logged, never fatal.
        # It runs BEFORE plugin startup so the spec §7 order holds: recovery
        # fences stale runs before any pipeline can claim new work.
        try:
            recovered = await run_agent_recovery(
                app.state.repositories,
                approval_policy=app.state.approval_policy,
            )
            if any(
                (
                    recovered.inbox_recovered,
                    recovered.inbox_delivery_unknown,
                    recovered.inbox_reconciled,
                    recovered.runs_marked_unknown,
                    recovered.cleanup_jobs_retried,
                    recovered.cleanup_jobs_completed,
                    recovered.approvals_revoked,
                    recovered.approvals_marked_unknown,
                )
            ):
                logger.info("Agent restart recovery: %s", recovered)
        except Exception:
            logger.exception("Agent restart recovery failed")
        # M4.5 runtime wiring (spec §3a/§7): the registry and watch engine
        # need the lifespan-built repositories, session factory, and hubs, so
        # the composition root builds them here and exposes them on app.state.
        # The plugin (registered with enabled=agent_broker_enabled) receives
        # them and its startup/shutdown hooks drive the lifecycle in the spec
        # order: watch engine first, then per-binding pipelines, then the
        # deadline tick task - and the exact reverse at shutdown.
        if settings.agent_broker_enabled:
            app.state.agent_runtime_registry = AgentRuntimeRegistry(
                settings=settings,
                repositories=app.state.repositories,
                sessions=app.state.session_factory,
                hub=app.state.agent_stream_hub,
                # Plan §6.2.1: the supervisor activation gate is always
                # wired in production.  Without a deployment-owned runtime
                # manager the supervisor fails closed, so every binding
                # stays disabled until its runtime can be attested
                # (spec §6).  Bindings without runtime fields fail closed
                # regardless of the supervisor.
                supervisor=runtime_supervisor,
            )
            app.state.agent_watch_engine = WatchEngine(
                sessions=app.state.session_factory,
                watches=app.state.repositories.watches,
                deliveries=app.state.repositories.agent_watch_deliveries,
                inbox=app.state.repositories.agent_inbox,
                hub=app.state.event_hub,
            )
            # Review fix M1: the trigger sink is the single delivery port for
            # every fired trigger (live and deadline).  ``fire()`` committed
            # the inbox row; the sink renders the typed WatchTriggeredInput
            # and hands it to the binding's pipeline so the dispatcher can
            # submit the continuation turn.
            app.state.agent_watch_engine.on_fired = build_watch_trigger_sink(
                app.state.agent_watch_engine,
                app.state.agent_runtime_registry,
            )
            app.state.agent_broker_plugin.bind_runtime_services(
                watch_engine=app.state.agent_watch_engine,
                registry=app.state.agent_runtime_registry,
                sessions=app.state.session_factory,
                watch_tick_seconds=settings.agent_watch_deadline_tick_seconds,
            )
        await app.state.feature_registry.startup(feature_context)
        # The MCP capability surface is built here because the continuation
        # service and token authenticator need the repository bundle; the
        # mounted ASGI wrapper serves it only while the plugin is enabled.
        mcp_lifespan_ctx = None
        if settings.agent_broker_enabled:
            app.state.agent_mcp_app = await _build_agent_mcp_app(app, settings)
            # The SDK's Streamable HTTP app starts its session manager from
            # its own Starlette lifespan, but Starlette only runs the
            # lifespan of the top-level app: the lifespan of an app mounted
            # with ``Mount`` is never entered, so every request past the auth
            # gate would fail with "Task group is not initialized".  Enter
            # the inner app's lifespan from here instead (the SDK documents
            # ``session_manager.run()`` as the host-app lifespan hook).
            mcp_lifespan_ctx = app.state.agent_mcp_app.router.lifespan_context(
                app.state.agent_mcp_app
            )
        expiry_task = asyncio.create_task(
            _heartbeat_expiry_loop(app.state.registry, app.state.event_hub, settings)
        )
        session_expiry_task = asyncio.create_task(
            _browser_session_expiry_loop(app.state.browser_sessions)
        )
        auth_epoch_stop = asyncio.Event()
        auth_epoch_task = asyncio.create_task(
            _authentication_epoch_loop(
                app.state.repositories,
                app.state.browser_sessions,
                app.state.terminal_hub,
                app.state.event_hub,
                app.state.agent_stream_hub,
                auth_epoch_stop,
            )
        )
        try:
            if mcp_lifespan_ctx is not None:
                async with mcp_lifespan_ctx:
                    yield
            else:
                yield
        finally:
            expiry_task.cancel()
            session_expiry_task.cancel()
            auth_epoch_stop.set()
            with suppress(asyncio.CancelledError):
                await expiry_task
            with suppress(asyncio.CancelledError):
                await session_expiry_task
            try:
                await auth_epoch_task
            finally:
                try:
                    await app.state.terminal_audit.close()
                finally:
                    try:
                        await app.state.feature_registry.shutdown()
                    finally:
                        await active_database.dispose()

    app = FastAPI(
        title="TermFlow Control Plane",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    browser_cookie_policy(settings)
    app.state.settings = settings
    app.state.session_factory = active_database.session_factory
    app.state.registry = LiveInstanceRegistry(
        queue_size=settings.connection_queue_size,
        queue_max_bytes=settings.terminal_queue_max_bytes,
    )
    app.state.event_hub = EventHub(queue_size=settings.event_queue_size)
    app.state.agent_stream_hub = AgentStreamHub(queue_size=AGENT_STREAM_QUEUE_SIZE)
    app.state.terminal_hub = TerminalHub(
        queue_max_messages=settings.terminal_queue_max_messages,
        queue_max_bytes=settings.terminal_queue_max_bytes,
    )
    app.state.browser_sessions = BrowserSessionStore(
        ttl=timedelta(seconds=settings.browser_session_ttl_seconds),
        capacity=settings.browser_session_capacity,
        on_revoke=app.state.terminal_hub.terminate_session_nowait,
    )
    app.state.auth_rate_limiter = AuthRateLimiter(
        capacity=getattr(settings, "auth_attempt_budget_capacity", 5),
        refill_seconds=float(getattr(settings, "auth_attempt_refill_seconds", 60)),
        global_capacity=getattr(settings, "auth_global_verification_capacity", 32),
        max_backoff_seconds=getattr(settings, "auth_max_backoff_seconds", 300),
        purpose_budgets={
            "oauth_device_token": (
                getattr(settings, "oauth_device_poll_budget_capacity", 60),
                float(getattr(settings, "oauth_device_poll_budget_refill_seconds", 60)),
            ),
        },
    )
    app.state.dpop_verifier = DpopVerifier()
    app.state.feature_registry = FeatureRegistry()
    # The plugin instance is owned by the composition root: the lifespan
    # attaches the runtime services it builds (registry + watch engine) to
    # this exact instance before feature startup, so the plugin's
    # startup/shutdown hooks drive their lifecycle under the
    # agent_broker_enabled gate.
    app.state.agent_broker_plugin = AgentBrokerPlugin()
    app.state.feature_registry.register(
        app.state.agent_broker_plugin,
        enabled=settings.agent_broker_enabled,
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        raw = request.headers.get("X-Request-ID")
        try:
            request.state.request_id = UUID(raw) if raw else uuid4()
        except ValueError:
            request.state.request_id = uuid4()
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        if request.url.path == "/api/v1/oauth/token":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(TermFlowError)
    async def handle_termflow_error(request: Request, exc: TermFlowError) -> JSONResponse:
        envelope = ErrorEnvelope(
            error=ErrorDetail(
                code=exc.code,
                message=exc.message,
                request_id=_request_id(request),
            )
        )
        headers = dict(exc.headers)
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(mode="json"),
            headers=headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        envelope = ErrorEnvelope(
            error=ErrorDetail(
                code="invalid_request",
                message="The request is invalid.",
                request_id=_request_id(request),
            )
        )
        return JSONResponse(
            status_code=422,
            content=envelope.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    app.include_router(enrollment_router)
    app.include_router(sessions_router)
    app.include_router(security_router)
    app.include_router(cli_router)
    app.include_router(oauth_router)
    app.include_router(clients_router)
    app.include_router(dashboard_router)
    app.include_router(computers_router)
    app.include_router(terms_router)
    app.include_router(terminal_router_api)
    app.include_router(instances_router)
    app.include_router(bridge_router)
    app.include_router(events_router)
    app.include_router(agent_capabilities_router)
    # Agent Broker functional routers are owned by the plugin and are only
    # mounted while the plugin is enabled; the capability-discovery endpoint
    # stays mounted unconditionally so C can observe the disabled state.
    if settings.agent_broker_enabled:
        app.include_router(agent_admin_router)
        app.include_router(agent_conversations_router)
        app.include_router(agent_stream_router)
        app.include_router(agent_approvals_router)
        # MCP Streamable HTTP is never exposed to a browser (plan §10); the
        # endpoint is only mounted while the plugin is enabled (plan §3.4).
        app.state.agent_mcp_app = None
        app.router.routes.append(_AgentMcpMount(MCP_STREAMABLE_HTTP_PATH))
    install_web_hosting(app, settings.static_dir)
    return app
