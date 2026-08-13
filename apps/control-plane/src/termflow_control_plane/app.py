"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
from termflow_control_plane.api.transcription import (
    MAX_CONCURRENT_TRANSCRIPTIONS,
    TRANSCRIPTION_TIMEOUT_SECONDS,
)
from termflow_control_plane.api.transcription import (
    router as transcription_router,
)
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
from termflow_control_plane.plugins.agent_broker.agent.permissions import ApprovalPolicy
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import (
    AGENT_STREAM_QUEUE_SIZE,
    AgentStreamHub,
)
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    ObservationService,
    WatchContinuationService,
)
from termflow_control_plane.plugins.agent_broker.agent.transcription import (
    NullTranscriptionProvider,
)
from termflow_control_plane.plugins.agent_broker.api.mcp_server import (
    MCP_STREAMABLE_HTTP_PATH,
    build_mcp_server,
    check_tool_config_drift,
    create_streamable_http_app,
    pinned_allowlist_from_fixture,
)
from termflow_control_plane.plugins.agent_broker.auth import AgentTokenAuthenticator
from termflow_control_plane.plugins.agent_broker.plugin import (
    AgentBrokerPlugin,
    run_agent_recovery,
)
from termflow_control_plane.plugins.context import build_feature_context
from termflow_control_plane.plugins.protocol import (
    AgentRuntimeSupervisor,
    AuthPort,
    LifecyclePort,
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
    """Assemble the observe-only MCP server once the repositories exist.

    B owns the security checks around the SDK (plan §10): bearer auth through
    ``require_agent_token``, byte limits, per-binding quotas, and the pinned
    tool-allowlist drift guard.  The drift guard reads the deployment-owned
    frozen OpenCode config when ``opencode_config_path`` is configured and
    refuses startup on any mismatch (plan §10, M0.3).

    The SDK app is built at ``path="/"``: Starlette mounts rewrite the child
    scope's route path, so the mounted app sees its own ``/`` route while the
    public path stays ``/api/v1/agent/mcp``.
    """
    server = build_mcp_server(
        observation=ObservationService(app.state.registry),
        continuation=WatchContinuationService(
            app.state.repositories.watches,
            app.state.repositories.agent_bindings,
        ),
        policy_checker=app.state.repositories,
        token_auth=AgentTokenAuthenticator(app.state.repositories),
    )
    config_path = settings.opencode_config_path
    if config_path:
        allowlist = pinned_allowlist_from_fixture(
            await asyncio.to_thread(
                lambda: Path(config_path).read_text(encoding="utf-8")
            )
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


def create_app(*, settings: Settings, database: Database | None = None) -> FastAPI:
    active_database = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await active_database.initialize()
        app.state.repositories = RepositoryBundle(active_database.session_factory)
        purged = await app.state.repositories.purge_expired(now=datetime.now(UTC))
        if any(purged.values()):
            logger.info("Purged expired persistence rows: %s", purged)
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
        app.state.terminal_router = TerminalRouter(
            registry=app.state.registry,
            hub=app.state.terminal_hub,
            audit=app.state.terminal_audit,
            capability_wait_seconds=settings.command_timeout_seconds,
            resume_grace_seconds=settings.terminal_resume_grace_seconds,
        )
        feature_context = build_feature_context(
            # No real port implementations exist yet, so every port is an
            # explicitly labeled placeholder until its milestone lands; real
            # terms/persistence adapters arrive with the M1.3+ services.
            auth=_unimplemented_port(AuthPort),  # AuthenticationService lacks AuthPort
            terms=_unimplemented_port(TermPort),  # real TermPort adapter lands with M1.3+
            observation=_unimplemented_port(TerminalObservationPort),  # lands with M2
            commands=_unimplemented_port(TerminalCommandPort),  # lands with M5
            persistence=_unimplemented_port(UnitOfWorkFactory),  # real adapter lands with M1.3+
            lifecycle=_unimplemented_port(LifecyclePort),  # lands with plugin background tasks
            runtime=_unimplemented_port(AgentRuntimeSupervisor),  # lands with M4
        )
        app.state.feature_context = feature_context
        await app.state.feature_registry.startup(feature_context)
        # Deterministic restart recovery (plan §17: B restarts): fence stale
        # inbox claims, mark stuck runs unknown, then retry pending cleanup
        # tombstones.  Fail-safe by design: errors are logged, never fatal.
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
    # Voice/STT plumbing (plan §14, task M7.1): the Null provider keeps text
    # chat fully functional until an optional STT container implements the
    # TranscriptionProvider port; the remaining bounds mirror the documented
    # constants in api.transcription and stay overridable per-app for tests.
    app.state.transcription_provider = NullTranscriptionProvider()
    app.state.transcription_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRANSCRIPTIONS)
    app.state.transcription_timeout_seconds = TRANSCRIPTION_TIMEOUT_SECONDS
    app.state.transcription_staging_dir = None
    app.state.feature_registry = FeatureRegistry()
    app.state.feature_registry.register(
        AgentBrokerPlugin(),
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
        app.include_router(transcription_router)
        # MCP Streamable HTTP is never exposed to a browser (plan §10); the
        # endpoint is only mounted while the plugin is enabled (plan §3.4).
        app.state.agent_mcp_app = None
        app.router.routes.append(_AgentMcpMount(MCP_STREAMABLE_HTTP_PATH))
    install_web_hosting(app, settings.static_dir)
    return app
