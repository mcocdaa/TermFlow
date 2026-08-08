"""Short-lived browser administrator sessions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from termflow_protocol import (
    BrowserSessionChallengeResponse,
    BrowserSessionCreateRequest,
    BrowserSessionDeleteResponse,
    BrowserSessionResponse,
    BrowserSessionTotpRequest,
)

from termflow_control_plane.auth.audit import (
    AuthAuditErrorCode,
    AuthAuditOperation,
    AuthAuditResult,
    AuthenticationAudit,
)
from termflow_control_plane.auth.rate_limit import AuthRateLimiter, client_source
from termflow_control_plane.auth.service import AuthenticationRejected, AuthenticationService
from termflow_control_plane.auth.sessions import (
    BrowserSessionStore,
    browser_cookie_policy,
    origin_allowed,
)
from termflow_control_plane.config import Settings
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle

from .dependencies import (
    get_authentication_service,
    get_browser_sessions,
    get_repositories,
    get_settings,
)

router = APIRouter(prefix="/api/v1", tags=["sessions"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _set_browser_session_cookie(
    response: Response,
    settings: Settings,
    sessions: BrowserSessionStore,
    *,
    epoch: int,
) -> BrowserSessionResponse:
    secret, expires_at = sessions.create(epoch=epoch)
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


@router.post(
    "/session",
    response_model=BrowserSessionResponse | BrowserSessionChallengeResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
@router.post(
    "/admin/sessions",
    response_model=BrowserSessionResponse | BrowserSessionChallengeResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": BrowserSessionChallengeResponse,
            "description": "A fresh TOTP code is required.",
        }
    },
)
async def create_browser_session(
    request: BrowserSessionCreateRequest,
    http_request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[BrowserSessionStore, Depends(get_browser_sessions)],
    authentication: Annotated[AuthenticationService, Depends(get_authentication_service)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> BrowserSessionResponse | BrowserSessionChallengeResponse:
    source = client_source(http_request)
    limiter: AuthRateLimiter = http_request.app.state.auth_rate_limiter
    audit: AuthenticationAudit = http_request.app.state.auth_audit
    if not origin_allowed(http_request.headers.get("origin"), settings):
        await audit.record(
            AuthAuditOperation.WEB_SESSION_LOGIN,
            AuthAuditResult.REJECTED,
            source,
            error_code=AuthAuditErrorCode.ORIGIN_REJECTED,
        )
        raise TermFlowError("origin_not_allowed", 403, "The browser Origin is not allowed.")
    try:
        limiter.check("web_session", source)
    except TermFlowError:
        await audit.record(
            AuthAuditOperation.WEB_SESSION_LOGIN,
            AuthAuditResult.RATE_LIMITED,
            source,
            error_code=AuthAuditErrorCode.RATE_LIMITED,
        )
        raise
    async with limiter.verification_slot():
        supplied = request.admin_token.get_secret_value()
        try:
            challenge = await authentication.begin_web_login(supplied)
        except AuthenticationRejected as exc:
            limiter.record_failure("web_session", source)
            await audit.record(
                AuthAuditOperation.WEB_SESSION_LOGIN,
                AuthAuditResult.REJECTED,
                source,
                error_code=AuthAuditErrorCode.INVALID_CREDENTIALS,
            )
            raise TermFlowError(
                "authentication_failed", 401, "Authentication failed."
            ) from exc
    limiter.record_success("web_session", source)
    await audit.record(
        AuthAuditOperation.WEB_SESSION_LOGIN,
        AuthAuditResult.OK,
        source,
    )
    if challenge is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        _no_store(response)
        return BrowserSessionChallengeResponse(
            challenge_id=challenge.challenge_id,
            expires_at=challenge.expires_at,
        )
    state = await repositories.auth_state.get()
    return _set_browser_session_cookie(response, settings, sessions, epoch=state.epoch)


@router.post(
    "/admin/sessions/{challenge_id}/totp",
    response_model=BrowserSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def complete_browser_session_totp(
    challenge_id: UUID,
    request: BrowserSessionTotpRequest,
    http_request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[BrowserSessionStore, Depends(get_browser_sessions)],
    authentication: Annotated[AuthenticationService, Depends(get_authentication_service)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> BrowserSessionResponse:
    source = client_source(http_request)
    limiter: AuthRateLimiter = http_request.app.state.auth_rate_limiter
    audit: AuthenticationAudit = http_request.app.state.auth_audit
    if not origin_allowed(http_request.headers.get("origin"), settings):
        await audit.record(
            AuthAuditOperation.TOTP_VERIFICATION,
            AuthAuditResult.REJECTED,
            source,
            error_code=AuthAuditErrorCode.ORIGIN_REJECTED,
        )
        raise TermFlowError("origin_not_allowed", 403, "The browser Origin is not allowed.")
    try:
        limiter.check("web_session_totp", source)
    except TermFlowError:
        await audit.record(
            AuthAuditOperation.TOTP_VERIFICATION,
            AuthAuditResult.RATE_LIMITED,
            source,
            error_code=AuthAuditErrorCode.RATE_LIMITED,
        )
        raise
    async with limiter.verification_slot():
        try:
            accepted = await authentication.complete_web_login(
                challenge_id,
                request.code.get_secret_value(),
            )
        except AuthenticationRejected:
            accepted = False
    if not accepted:
        limiter.record_failure("web_session_totp", source)
        await audit.record(
            AuthAuditOperation.TOTP_VERIFICATION,
            AuthAuditResult.REJECTED,
            source,
            error_code=AuthAuditErrorCode.INVALID_CREDENTIALS,
        )
        raise TermFlowError("authentication_failed", 401, "Authentication failed.")
    limiter.record_success("web_session_totp", source)
    await audit.record(
        AuthAuditOperation.TOTP_VERIFICATION,
        AuthAuditResult.OK,
        source,
    )
    state = await repositories.auth_state.get()
    return _set_browser_session_cookie(response, settings, sessions, epoch=state.epoch)


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
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> BrowserSessionResponse:
    policy = browser_cookie_policy(settings)
    state = await repositories.auth_state.get()
    expires_at = sessions.authenticate(
        http_request.cookies.get(policy.name),
        epoch=state.epoch,
    )
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
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> BrowserSessionDeleteResponse:
    if not origin_allowed(http_request.headers.get("origin"), settings):
        raise TermFlowError("origin_not_allowed", 403, "The browser Origin is not allowed.")
    policy = browser_cookie_policy(settings)
    secret = http_request.cookies.get(policy.name)
    state = await repositories.auth_state.get()
    if sessions.authenticate(secret, epoch=state.epoch) is None:
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    sessions.invalidate(secret)
    response.delete_cookie(
        key=policy.name,
        path="/",
        secure=policy.secure,
        httponly=True,
        samesite="strict",
    )
    _no_store(response)
    return BrowserSessionDeleteResponse()
