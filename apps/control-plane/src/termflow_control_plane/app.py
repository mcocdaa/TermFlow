"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from termflow_protocol import (
    ErrorDetail,
    ErrorEnvelope,
    HealthResponse,
    InstancePresencePayload,
    MessageType,
    WireMessage,
)

from termflow_control_plane import __version__
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
from termflow_control_plane.auth.sessions import BrowserSessionStore
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.connections.registry import LiveConnection, LiveInstanceRegistry
from termflow_control_plane.connections.terminal_hub import TerminalHub
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.routing.router import CommandRouter
from termflow_control_plane.routing.terminal_audit import TerminalAuditWriter
from termflow_control_plane.routing.terminal_router import TerminalRouter
from termflow_control_plane.web import install_web_hosting

logger = logging.getLogger(__name__)

_AUTHENTICATION_EPOCH_POLL_SECONDS = 1.0


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
        except SQLAlchemyError:
            logger.exception("Authentication epoch poll failed; retrying")


async def _verify_oauth_totp(service: AuthenticationService, code: str) -> bool:
    """Convert unavailable or invalid TOTP state into a closed authorization denial."""

    try:
        return await service.verify_fresh_totp(code)
    except AuthenticationRejected:
        return False


def create_app(*, settings: Settings, database: Database | None = None) -> FastAPI:
    active_database = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await active_database.initialize()
        app.state.repositories = RepositoryBundle(active_database.session_factory)
        auth_state = await app.state.repositories.auth_state.get()
        app.state.browser_sessions.synchronize_epoch(auth_state.epoch)
        await app.state.terminal_hub.synchronize_epoch(auth_state.epoch)
        await app.state.event_hub.synchronize_epoch(auth_state.epoch)
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
        app.state.terminal_router = TerminalRouter(
            registry=app.state.registry,
            hub=app.state.terminal_hub,
            audit=app.state.terminal_audit,
            capability_wait_seconds=settings.command_timeout_seconds,
            resume_grace_seconds=settings.terminal_resume_grace_seconds,
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
                auth_epoch_stop,
            )
        )
        try:
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
                    await active_database.dispose()

    app = FastAPI(
        title="TermFlow Control Plane",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = LiveInstanceRegistry(
        queue_size=settings.connection_queue_size,
        queue_max_bytes=settings.terminal_queue_max_bytes,
    )
    app.state.event_hub = EventHub(queue_size=settings.event_queue_size)
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
        max_backoff_seconds=getattr(settings, "auth_max_backoff_seconds", 300),
        purpose_budgets={
            "oauth_device_token": (
                getattr(settings, "oauth_device_poll_budget_capacity", 60),
                float(getattr(settings, "oauth_device_poll_budget_refill_seconds", 60)),
            ),
            "oauth_device_code": (
                getattr(settings, "oauth_device_poll_budget_capacity", 60),
                float(getattr(settings, "oauth_device_poll_budget_refill_seconds", 60)),
            ),
        },
    )
    app.state.dpop_verifier = DpopVerifier()

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
    install_web_hosting(app, settings.static_dir)
    return app
