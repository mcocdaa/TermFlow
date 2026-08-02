"""OAuth-style native public-client HTTP translation."""

from __future__ import annotations

import json
from typing import Annotated, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from termflow_protocol import (
    OAuthAuthorizationDecisionRequest,
    OAuthAuthorizationDecisionResponse,
    OAuthAuthorizationPreviewResponse,
    OAuthAuthorizationRequest,
    OAuthMetadataResponse,
    OAuthPublicJwk,
    OAuthRevokeRequest,
    OAuthRevokeResponse,
    OAuthTokenRequest,
    OAuthTokenResponse,
)

from termflow_control_plane.auth.audit import (
    AuthAuditErrorCode,
    AuthAuditOperation,
    AuthAuditResult,
    AuthenticationAudit,
)
from termflow_control_plane.auth.dpop import DpopInvalid, DpopNonceRequired, DpopVerifier
from termflow_control_plane.auth.oauth import FreshTotpVerifier, OAuthService
from termflow_control_plane.auth.rate_limit import AuthRateLimiter, direct_peer_source
from termflow_control_plane.config import Settings
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle

from .dependencies import get_repositories, get_settings, require_admin

router = APIRouter(tags=["oauth"])

_AUTHORIZATION_QUERY_FIELDS = {
    "response_type",
    "client_name",
    "platform",
    "client_version",
    "redirect_uri",
    "state",
    "code_challenge",
    "code_challenge_method",
    "dpop_jkt",
    "public_jwk",
    "scopes",
}
_AUTHORIZATION_REQUIRED_QUERY_FIELDS = _AUTHORIZATION_QUERY_FIELDS - {"client_version"}


def _service(
    request: Request,
    repositories: RepositoryBundle,
    settings: Settings,
) -> OAuthService:
    dpop = cast(DpopVerifier, request.app.state.dpop_verifier)
    totp_verifier = cast(
        FreshTotpVerifier | None,
        getattr(request.app.state, "oauth_totp_verifier", None),
    )
    return OAuthService(
        repositories,
        settings,
        dpop,
        totp_verifier=totp_verifier,
    )


def _invalid_request() -> TermFlowError:
    return TermFlowError("invalid_request", 400, "The authorization request is invalid.")


def _parse_authorization_request(request: Request) -> OAuthAuthorizationRequest:
    query = request.query_params
    try:
        query_fields = set(query.keys())
        if (
            not _AUTHORIZATION_REQUIRED_QUERY_FIELDS <= query_fields
            or not query_fields <= _AUTHORIZATION_QUERY_FIELDS
            or any(len(query.getlist(name)) != 1 for name in query_fields - {"scopes"})
        ):
            raise ValueError
        public_raw = query.get("public_jwk")
        if public_raw is None:
            raise ValueError
        public_jwk = OAuthPublicJwk.model_validate(json.loads(public_raw))
        return OAuthAuthorizationRequest.model_validate(
            {
                "response_type": query.get("response_type", "code"),
                "client_name": query.get("client_name"),
                "platform": query.get("platform"),
                "client_version": query.get("client_version"),
                "redirect_uri": query.get("redirect_uri"),
                "state": query.get("state"),
                "code_challenge": query.get("code_challenge"),
                "code_challenge_method": query.get("code_challenge_method", "S256"),
                "dpop_jkt": query.get("dpop_jkt"),
                "public_jwk": public_jwk,
                "scopes": query.getlist("scopes"),
            }
        )
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise _invalid_request() from exc


@router.get(
    "/.well-known/oauth-authorization-server",
    response_model=OAuthMetadataResponse,
)
async def oauth_metadata(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> OAuthMetadataResponse:
    return _service(request, repositories, settings).metadata()


@router.get("/api/v1/oauth/authorize", response_model=None)
async def authorize_native_client(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    transaction_id: Annotated[UUID | None, Query()] = None,
) -> OAuthAuthorizationPreviewResponse | RedirectResponse:
    service = _service(request, repositories, settings)
    if transaction_id is not None:
        if (
            set(request.query_params.keys()) != {"transaction_id"}
            or len(request.query_params.getlist("transaction_id")) != 1
        ):
            raise _invalid_request()
        response.headers["Cache-Control"] = "no-store"
        return await service.preview(transaction_id)
    limiter = cast(AuthRateLimiter, request.app.state.auth_rate_limiter)
    source = direct_peer_source(request)
    limiter.check("native_authorization", source)
    transaction = await service.begin(_parse_authorization_request(request))
    return RedirectResponse(
        url=f"/authorize?{urlencode({'transaction_id': str(transaction)})}",
        status_code=307,
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/api/v1/oauth/authorize",
    response_model=OAuthAuthorizationDecisionResponse,
)
async def decide_native_authorization(
    body: OAuthAuthorizationDecisionRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> OAuthAuthorizationDecisionResponse:
    limiter = cast(AuthRateLimiter, request.app.state.auth_rate_limiter)
    audit = cast(AuthenticationAudit, request.app.state.auth_audit)
    source = direct_peer_source(request)
    try:
        limiter.check("native_authorization_decision", source)
    except TermFlowError:
        await audit.record(
            AuthAuditOperation.NATIVE_AUTHORIZATION,
            AuthAuditResult.RATE_LIMITED,
            source,
            error_code=AuthAuditErrorCode.RATE_LIMITED,
        )
        raise
    try:
        async with limiter.verification_slot():
            result = await _service(request, repositories, settings).decide(body)
    except TermFlowError:
        limiter.record_failure("native_authorization_decision", source)
        await audit.record(
            AuthAuditOperation.NATIVE_AUTHORIZATION,
            AuthAuditResult.REJECTED,
            source,
            error_code=AuthAuditErrorCode.INVALID_CREDENTIALS,
        )
        raise
    limiter.record_success("native_authorization_decision", source)
    await audit.record(
        AuthAuditOperation.NATIVE_AUTHORIZATION,
        AuthAuditResult.OK,
        source,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/api/v1/oauth/token", response_model=OAuthTokenResponse)
async def exchange_native_token(
    body: OAuthTokenRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    dpop_proof: Annotated[str | None, Header(alias="DPoP")] = None,
) -> OAuthTokenResponse:
    if dpop_proof is None:
        raise TermFlowError("invalid_dpop_proof", 401, "DPoP proof is required.")
    limiter = cast(AuthRateLimiter, request.app.state.auth_rate_limiter)
    audit = cast(AuthenticationAudit, request.app.state.auth_audit)
    source = direct_peer_source(request)
    try:
        limiter.check("oauth_token", source)
    except TermFlowError:
        await audit.record(
            AuthAuditOperation.TOKEN_EXCHANGE,
            AuthAuditResult.RATE_LIMITED,
            source,
            error_code=AuthAuditErrorCode.RATE_LIMITED,
        )
        raise
    service = _service(request, repositories, settings)
    verified = None
    try:
        async with limiter.verification_slot():
            verified = service.verify_token_proof(dpop_proof, body.public_jwk)
            result = await service.exchange(body, verified)
    except DpopNonceRequired as exc:
        raise TermFlowError(
            "use_dpop_nonce",
            401,
            "A fresh DPoP nonce is required.",
            headers={"DPoP-Nonce": exc.nonce, "Cache-Control": "no-store"},
        ) from exc
    except DpopInvalid as exc:
        limiter.record_failure("oauth_token", source)
        await audit.record(
            AuthAuditOperation.TOKEN_EXCHANGE,
            AuthAuditResult.REJECTED,
            source,
            error_code=AuthAuditErrorCode.INVALID_CREDENTIALS,
        )
        raise TermFlowError("invalid_dpop_proof", 401, "DPoP proof is invalid.") from exc
    except TermFlowError as exc:
        if verified is not None:
            exc.headers.update({"DPoP-Nonce": verified.next_nonce, "Cache-Control": "no-store"})
        limiter.record_failure("oauth_token", source)
        await audit.record(
            AuthAuditOperation.TOKEN_EXCHANGE,
            AuthAuditResult.REJECTED,
            source,
            error_code=AuthAuditErrorCode.INVALID_CREDENTIALS,
        )
        raise
    limiter.record_success("oauth_token", source)
    await audit.record(
        AuthAuditOperation.TOKEN_EXCHANGE,
        AuthAuditResult.OK,
        source,
    )
    assert verified is not None
    response.headers["DPoP-Nonce"] = verified.next_nonce
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/api/v1/oauth/revoke",
    response_model=OAuthRevokeResponse,
    dependencies=[Depends(require_admin)],
)
async def revoke_native_token(
    body: OAuthRevokeRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> OAuthRevokeResponse:
    client_id = getattr(request.state, "native_client_id", None)
    key_thumbprint = getattr(request.state, "native_key_thumbprint", None)
    if not isinstance(client_id, UUID) or not isinstance(key_thumbprint, str):
        raise TermFlowError("unauthorized", 401, "A native access token is required.")
    response.headers["Cache-Control"] = "no-store"
    return await _service(request, repositories, settings).revoke(
        body.token,
        client_id=client_id,
        key_thumbprint=key_thumbprint,
    )
