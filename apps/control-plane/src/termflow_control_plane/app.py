"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from termflow_protocol import (
    ErrorDetail,
    ErrorEnvelope,
    HealthResponse,
    InstancePresencePayload,
    MessageType,
    WireMessage,
)

from termflow_control_plane.api.bridge import router as bridge_router
from termflow_control_plane.api.enrollment import router as enrollment_router
from termflow_control_plane.api.events import router as events_router
from termflow_control_plane.api.instances import router as instances_router
from termflow_control_plane.api.sessions import router as sessions_router
from termflow_control_plane.auth.sessions import BrowserSessionStore
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.event_hub import EventHub
from termflow_control_plane.connections.registry import LiveConnection, LiveInstanceRegistry
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.routing.router import CommandRouter


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


def create_app(*, settings: Settings, database: Database | None = None) -> FastAPI:
    active_database = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await active_database.initialize()
        app.state.repositories = RepositoryBundle(active_database.session_factory)
        app.state.command_router = CommandRouter(
            registry=app.state.registry,
            audit=app.state.repositories.audit,
            settings=settings,
        )
        expiry_task = asyncio.create_task(
            _heartbeat_expiry_loop(app.state.registry, app.state.event_hub, settings)
        )
        try:
            yield
        finally:
            expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await expiry_task
            await active_database.dispose()

    app = FastAPI(title="TermFlow Control Plane", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = LiveInstanceRegistry(queue_size=settings.connection_queue_size)
    app.state.event_hub = EventHub(queue_size=settings.event_queue_size)
    app.state.browser_sessions = BrowserSessionStore(
        ttl=timedelta(seconds=settings.browser_session_ttl_seconds),
        capacity=settings.browser_session_capacity,
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
        return JSONResponse(status_code=exc.status_code, content=envelope.model_dump(mode="json"))

    @app.get("/healthz", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    app.include_router(enrollment_router)
    app.include_router(sessions_router)
    app.include_router(instances_router)
    app.include_router(bridge_router)
    app.include_router(events_router)
    return app
