"""FastAPI dependency boundaries for settings, repositories, and credentials."""

from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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


def _raw_bearer(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    return credentials.credentials


async def require_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[BrowserSessionStore, Depends(get_browser_sessions)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> None:
    expected = settings.admin_token.get_secret_value()
    if credentials is not None:
        supplied = _raw_bearer(credentials)
        if secret_text_matches(supplied, expected):
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
    """Require an epoch-current Web Cookie and exact Origin for security APIs."""

    if not origin_allowed(request.headers.get("origin"), settings):
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
