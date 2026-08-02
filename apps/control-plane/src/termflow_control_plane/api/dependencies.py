"""FastAPI dependency boundaries for settings, repositories, and credentials."""

import json
from typing import Annotated, cast

from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from termflow_control_plane.auth.dpop import DpopInvalid, DpopNonceRequired, DpopVerifier
from termflow_control_plane.auth.service import AuthenticationService
from termflow_control_plane.auth.sessions import (
    BrowserSessionStore,
    browser_cookie_policy,
    origin_allowed,
)
from termflow_control_plane.auth.tokens import hash_token, secret_text_matches
from termflow_control_plane.config import Settings
from termflow_control_plane.connections.registry import LiveInstanceRegistry
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import Installation
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.routing.router import CommandRouter

bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_repositories(request: Request) -> RepositoryBundle:
    return cast(RepositoryBundle, request.app.state.repositories)


def get_browser_sessions(request: Request) -> BrowserSessionStore:
    return cast(BrowserSessionStore, request.app.state.browser_sessions)


def get_authentication_service(request: Request) -> AuthenticationService:
    return cast(AuthenticationService, request.app.state.authentication_service)


def get_registry(request: Request) -> LiveInstanceRegistry:
    return cast(LiveInstanceRegistry, request.app.state.registry)


def get_command_router(request: Request) -> CommandRouter:
    return cast(CommandRouter, request.app.state.command_router)


def _dpop_credentials(request: Request) -> HTTPAuthorizationCredentials | None:
    raw = request.headers.get("authorization", "")
    scheme, separator, credentials = raw.partition(" ")
    if not separator or scheme.lower() != "dpop" or not credentials:
        return None
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)


def _raw_bearer(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    return credentials.credentials


def _required_scope(request: Request) -> str | None:
    path = request.url.path
    mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"}
    if path.startswith("/api/v1/computers"):
        return "computers.write" if mutating else "computers.read"
    if path.startswith("/api/v1/instances/") and path.endswith("/topology"):
        return "terminal.read"
    if (
        path.startswith("/api/v1/instances/")
        and "/panes/" in path
        and path.endswith("/input")
    ):
        return "terminal.write"
    if path == "/api/v1/enrollment-tokens" or path.startswith("/api/v1/instances"):
        return "computers.write" if mutating else "computers.read"
    if path.startswith("/api/v1/terms"):
        return "terminal.write" if mutating else "terminal.read"
    if path == "/api/v1/dashboard":
        return "computers.read"
    return None


def _has_scope(encoded: str, required: str | None) -> bool:
    if required is None:
        return True
    try:
        scopes = json.loads(encoded)
    except (TypeError, ValueError):
        return False
    return isinstance(scopes, list) and required in scopes


async def require_admin(
    request: Request,
    response: Response,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[BrowserSessionStore, Depends(get_browser_sessions)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> None:
    credentials = credentials or _dpop_credentials(request)
    expected = settings.admin_token.get_secret_value()
    if credentials is not None:
        scheme = credentials.scheme.lower()
        if scheme not in {"bearer", "dpop"}:
            raise TermFlowError("unauthorized", 401, "Authentication is required.")
        supplied = credentials.credentials
        state = await repositories.auth_state.get()
        if (
            scheme == "bearer"
            and secret_text_matches(supplied, expected)
            and state.totp_enabled_at is None
        ):
            return
        if scheme == "bearer":
            cli_token = await repositories.auth_tokens.get_active(
                supplied,
                epoch=state.epoch,
                kind="cli",
            )
            if cli_token is not None:
                if not _has_scope(cli_token.scopes, _required_scope(request)):
                    raise TermFlowError(
                        "insufficient_scope", 403, "The credential lacks the required scope."
                    )
                return
        access_token = await repositories.auth_tokens.get_active(
            supplied,
            epoch=state.epoch,
            kind="access",
        )
        if access_token is not None and access_token.key_thumbprint is not None:
            if not _has_scope(access_token.scopes, _required_scope(request)):
                raise TermFlowError(
                    "insufficient_scope", 403, "The credential lacks the required scope."
                )
            proof = request.headers.get("dpop")
            if proof is None:
                raise TermFlowError("invalid_dpop_proof", 401, "DPoP proof is required.")
            verifier = cast(DpopVerifier, request.app.state.dpop_verifier)
            htu = f"{str(settings.public_base_url).rstrip('/')}{request.url.path}"
            try:
                verified = verifier.verify(
                    proof,
                    method=request.method,
                    htu=htu,
                    expected_jkt=access_token.key_thumbprint,
                    access_token=supplied,
                )
            except DpopNonceRequired as exc:
                raise TermFlowError(
                    "use_dpop_nonce",
                    401,
                    "A fresh DPoP nonce is required.",
                    headers={"DPoP-Nonce": exc.nonce, "Cache-Control": "no-store"},
                ) from exc
            except DpopInvalid as exc:
                raise TermFlowError("invalid_dpop_proof", 401, "DPoP proof is invalid.") from exc
            request.state.native_client_id = access_token.client_id
            request.state.native_key_thumbprint = access_token.key_thumbprint
            response.headers["DPoP-Nonce"] = verified.next_nonce
            return
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    state = await repositories.auth_state.get()
    policy = browser_cookie_policy(settings)
    if sessions.authenticate(request.cookies.get(policy.name), epoch=state.epoch) is None:
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not origin_allowed(
        request.headers.get("origin"),
        settings,
    ):
        raise TermFlowError("origin_not_allowed", 403, "The browser Origin is not allowed.")


async def require_web_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[BrowserSessionStore, Depends(get_browser_sessions)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> None:
    """Require an epoch-current Web Cookie and exact Origin on unsafe requests."""

    origin = request.headers.get("origin")
    if (origin is None and request.method not in {"GET", "HEAD", "OPTIONS"}) or (
        origin is not None and not origin_allowed(origin, settings)
    ):
        raise TermFlowError("origin_not_allowed", 403, "The browser Origin is not allowed.")
    state = await repositories.auth_state.get()
    policy = browser_cookie_policy(settings)
    if sessions.authenticate(request.cookies.get(policy.name), epoch=state.epoch) is None:
        raise TermFlowError("unauthorized", 401, "Authentication is required.")


async def require_installation(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> Installation:
    supplied = _raw_bearer(credentials)
    installation = await repositories.installations.get_by_token_hash(hash_token(supplied))
    if installation is None:
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    return installation
