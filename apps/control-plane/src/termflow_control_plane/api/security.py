"""Web-only TOTP settings and root-to-CLI credential exchange."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from termflow_protocol import (
    CliTokenRequest,
    CliTokenResponse,
    TotpConfirmRequest,
    TotpProtectionRequest,
    TotpSetupRequest,
    TotpSetupResponse,
    TotpStatusResponse,
)

from termflow_control_plane.auth.audit import (
    AuthAuditErrorCode,
    AuthAuditOperation,
    AuthAuditResult,
    AuthenticationAudit,
)
from termflow_control_plane.auth.rate_limit import AuthRateLimiter, direct_peer_source
from termflow_control_plane.auth.service import (
    AuthenticationRejected,
    AuthenticationService,
    TotpUnavailable,
)
from termflow_control_plane.errors import TermFlowError

from .dependencies import get_authentication_service, require_web_admin

router = APIRouter(
    prefix="/api/v1/admin/totp",
    tags=["security"],
    dependencies=[Depends(require_web_admin)],
)
cli_router = APIRouter(prefix="/api/v1/admin", tags=["security"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def _audit_rejected(
    request: Request,
    operation: AuthAuditOperation = AuthAuditOperation.TOTP_VERIFICATION,
) -> None:
    audit: AuthenticationAudit = request.app.state.auth_audit
    await audit.record(
        operation,
        AuthAuditResult.REJECTED,
        direct_peer_source(request),
        error_code=AuthAuditErrorCode.INVALID_CREDENTIALS,
    )


async def _audit_ok(
    request: Request,
    operation: AuthAuditOperation = AuthAuditOperation.TOTP_VERIFICATION,
) -> None:
    audit: AuthenticationAudit = request.app.state.auth_audit
    await audit.record(
        operation,
        AuthAuditResult.OK,
        direct_peer_source(request),
    )


async def _limiter(
    request: Request,
    purpose: str,
    operation: AuthAuditOperation = AuthAuditOperation.TOTP_VERIFICATION,
) -> tuple[AuthRateLimiter, str]:
    limiter: AuthRateLimiter = request.app.state.auth_rate_limiter
    source = direct_peer_source(request)
    try:
        limiter.check(purpose, source)
    except TermFlowError:
        audit: AuthenticationAudit = request.app.state.auth_audit
        await audit.record(
            operation,
            AuthAuditResult.RATE_LIMITED,
            source,
            error_code=AuthAuditErrorCode.RATE_LIMITED,
        )
        raise
    return limiter, source


def _authentication_failed() -> TermFlowError:
    return TermFlowError("authentication_failed", 401, "Authentication failed.")


@cli_router.post(
    "/cli-tokens",
    response_model=CliTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_cli_token(
    request: CliTokenRequest,
    http_request: Request,
    response: Response,
    authentication: Annotated[AuthenticationService, Depends(get_authentication_service)],
) -> CliTokenResponse:
    operation = AuthAuditOperation.CLI_LOGIN
    limiter, source = await _limiter(http_request, "cli_token", operation)
    try:
        async with limiter.verification_slot():
            issued = await authentication.issue_cli_token(
                request.admin_token.get_secret_value(),
                request.totp_code.get_secret_value()
                if request.totp_code is not None
                else None,
                tuple(request.scopes),
            )
    except AuthenticationRejected as exc:
        limiter.record_failure("cli_token", source)
        await _audit_rejected(http_request, operation)
        raise _authentication_failed() from exc
    limiter.record_success("cli_token", source)
    await _audit_ok(http_request, operation)
    _no_store(response)
    return CliTokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        scopes=request.scopes,
    )


@router.get("", response_model=TotpStatusResponse)
async def get_totp_status(
    response: Response,
    authentication: Annotated[AuthenticationService, Depends(get_authentication_service)],
) -> TotpStatusResponse:
    configured, enabled, available = await authentication.totp_status()
    _no_store(response)
    return TotpStatusResponse(
        configured=configured,
        enabled=enabled,
        available=available,
    )


@router.post(
    "/setups",
    response_model=TotpSetupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_totp_setup(
    request: TotpSetupRequest,
    http_request: Request,
    response: Response,
    authentication: Annotated[AuthenticationService, Depends(get_authentication_service)],
) -> TotpSetupResponse:
    limiter, source = await _limiter(http_request, "totp_setup")
    try:
        async with limiter.verification_slot():
            setup = await authentication.begin_totp_setup(
                request.admin_token.get_secret_value(),
                request.totp_code.get_secret_value() if request.totp_code is not None else None,
            )
    except TotpUnavailable as exc:
        raise TermFlowError(
            "totp_unavailable",
            409,
            "TOTP is unavailable until the server encryption key is configured.",
        ) from exc
    except AuthenticationRejected as exc:
        limiter.record_failure("totp_setup", source)
        await _audit_rejected(http_request)
        raise _authentication_failed() from exc
    limiter.record_success("totp_setup", source)
    await _audit_ok(http_request)
    _no_store(response)
    return TotpSetupResponse(
        setup_id=setup.setup_id,
        provisioning_uri=setup.provisioning_uri,
        setup_key=setup.setup_key,
        expires_at=setup.expires_at,
    )


@router.post(
    "/setups/{setup_id}/confirm",
    response_model=TotpStatusResponse,
)
async def confirm_totp_setup(
    setup_id: UUID,
    request: TotpConfirmRequest,
    http_request: Request,
    response: Response,
    authentication: Annotated[AuthenticationService, Depends(get_authentication_service)],
) -> TotpStatusResponse:
    limiter, source = await _limiter(http_request, "totp_setup_confirm")
    try:
        async with limiter.verification_slot():
            accepted = await authentication.confirm_totp_setup(
                setup_id,
                request.code.get_secret_value(),
            )
    except TotpUnavailable:
        accepted = False
    if not accepted:
        limiter.record_failure("totp_setup_confirm", source)
        await _audit_rejected(http_request)
        raise _authentication_failed()
    limiter.record_success("totp_setup_confirm", source)
    await _audit_ok(http_request)
    _no_store(response)
    return TotpStatusResponse(configured=True, enabled=False, available=True)


@router.post("/enable", response_model=TotpStatusResponse)
async def enable_totp(
    request: TotpProtectionRequest,
    http_request: Request,
    response: Response,
    authentication: Annotated[AuthenticationService, Depends(get_authentication_service)],
) -> TotpStatusResponse:
    limiter, source = await _limiter(http_request, "totp_enable")
    try:
        async with limiter.verification_slot():
            enabled = await authentication.enable_totp(
                request.admin_token.get_secret_value(),
                request.code.get_secret_value(),
            )
    except (AuthenticationRejected, TotpUnavailable):
        enabled = False
    if not enabled:
        limiter.record_failure("totp_enable", source)
        await _audit_rejected(http_request)
        raise _authentication_failed()
    limiter.record_success("totp_enable", source)
    await _audit_ok(http_request)
    _no_store(response)
    return TotpStatusResponse(configured=True, enabled=True, available=True)


@router.delete("", response_model=TotpStatusResponse)
async def disable_totp(
    request: TotpProtectionRequest,
    http_request: Request,
    response: Response,
    authentication: Annotated[AuthenticationService, Depends(get_authentication_service)],
) -> TotpStatusResponse:
    limiter, source = await _limiter(http_request, "totp_disable")
    try:
        async with limiter.verification_slot():
            disabled = await authentication.disable_totp(
                request.admin_token.get_secret_value(),
                request.code.get_secret_value(),
            )
    except (AuthenticationRejected, TotpUnavailable):
        disabled = False
    if not disabled:
        limiter.record_failure("totp_disable", source)
        await _audit_rejected(http_request)
        raise _authentication_failed()
    limiter.record_success("totp_disable", source)
    await _audit_ok(http_request)
    _no_store(response)
    return TotpStatusResponse(configured=True, enabled=False, available=True)
