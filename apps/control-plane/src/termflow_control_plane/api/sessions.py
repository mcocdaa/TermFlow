"""Short-lived browser administrator sessions."""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from termflow_protocol import (
    BrowserSessionCreateRequest,
    BrowserSessionDeleteResponse,
    BrowserSessionResponse,
)

from termflow_control_plane.auth.sessions import (
    BrowserSessionStore,
    browser_cookie_policy,
    origin_allowed,
)
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.terminal_hub import TerminalHub
from termflow_control_plane.errors import TermFlowError

from .dependencies import get_browser_sessions, get_settings

router = APIRouter(prefix="/api/v1", tags=["sessions"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.post(
    "/session",
    response_model=BrowserSessionResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
@router.post(
    "/admin/sessions",
    response_model=BrowserSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_browser_session(
    request: BrowserSessionCreateRequest,
    http_request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[BrowserSessionStore, Depends(get_browser_sessions)],
) -> BrowserSessionResponse:
    if not origin_allowed(http_request.headers.get("origin"), settings):
        raise TermFlowError("origin_not_allowed", 403, "The browser Origin is not allowed.")
    supplied = request.admin_token.get_secret_value()
    if not hmac.compare_digest(supplied, settings.admin_token.get_secret_value()):
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    secret, expires_at = sessions.create()
    policy = browser_cookie_policy(settings)
    response.set_cookie(
        key=policy.name,
        value=secret,
        max_age=settings.browser_session_ttl_seconds,
        path="/",
        secure=policy.secure,
        httponly=True,
        samesite="strict",
    )
    _no_store(response)
    return BrowserSessionResponse(expires_at=expires_at)


@router.get(
    "/session",
    response_model=BrowserSessionResponse,
    include_in_schema=False,
)
@router.get(
    "/admin/session",
    response_model=BrowserSessionResponse,
)
async def get_browser_session(
    http_request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[BrowserSessionStore, Depends(get_browser_sessions)],
) -> BrowserSessionResponse:
    policy = browser_cookie_policy(settings)
    expires_at = sessions.authenticate(http_request.cookies.get(policy.name))
    if expires_at is None:
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    _no_store(response)
    return BrowserSessionResponse(expires_at=expires_at)


@router.delete(
    "/session",
    response_model=BrowserSessionDeleteResponse,
    include_in_schema=False,
)
@router.delete(
    "/admin/session",
    response_model=BrowserSessionDeleteResponse,
)
async def delete_browser_session(
    http_request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[BrowserSessionStore, Depends(get_browser_sessions)],
) -> BrowserSessionDeleteResponse:
    if not origin_allowed(http_request.headers.get("origin"), settings):
        raise TermFlowError("origin_not_allowed", 403, "The browser Origin is not allowed.")
    policy = browser_cookie_policy(settings)
    secret = http_request.cookies.get(policy.name)
    if sessions.authenticate(secret) is None:
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    session_key = sessions.session_key(secret)
    sessions.invalidate(secret)
    if session_key is not None:
        terminal_hub = http_request.app.state.terminal_hub
        assert isinstance(terminal_hub, TerminalHub)
        await terminal_hub.terminate_session(session_key)
    response.delete_cookie(
        key=policy.name,
        path="/",
        secure=policy.secure,
        httponly=True,
        samesite="strict",
    )
    _no_store(response)
    return BrowserSessionDeleteResponse()
