"""FastAPI dependency boundaries for settings, repositories, and credentials."""

import hmac
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from termflow_control_plane.auth.tokens import hash_token
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


def get_registry(request: Request) -> LiveInstanceRegistry:
    return cast(LiveInstanceRegistry, request.app.state.registry)


def get_command_router(request: Request) -> CommandRouter:
    return cast(CommandRouter, request.app.state.command_router)


def _raw_bearer(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise TermFlowError("unauthorized", 401, "Authentication is required.")
    return credentials.credentials


async def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    supplied = _raw_bearer(credentials)
    expected = settings.admin_token.get_secret_value()
    if not hmac.compare_digest(supplied, expected):
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
