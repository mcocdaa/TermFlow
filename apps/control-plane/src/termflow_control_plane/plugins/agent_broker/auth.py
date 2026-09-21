"""AgentToken authentication for the MCP capability surface (plan §10).

B owns the security checks around the MCP SDK.  ``require_agent_token``
accepts only a hashed :class:`~termflow_control_plane.persistence.models.AgentToken`
bound to one binding/Term/runtime epoch: the raw token is hashed, looked up
through the agent token repository (which already excludes revoked rows),
checked for expiry and binding status, and the token's binding epoch must
match the binding's current runtime epoch exactly.  Every failure raises
:class:`AgentTokenAuthError` (HTTP 401) and fails closed; there is no
generic admin/native token fallback and no caller-controlled ``term_id``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.persistence.repositories import RepositoryBundle, decode_scopes

#: Token scope required for pane reads and watch management (plan §10).
SCOPE_TERMINAL_OBSERVE = "terminal.observe"

#: Token scope required for pane writes; writes land with M5.
SCOPE_TERMINAL_WRITE = "terminal.write"

#: Token scope for exposing normalized workspace labels/cwd; never default.
SCOPE_PATH_OBSERVE = "terminal.path_observe"

#: Binding states under which the binding's MCP capability may serve tokens.
# ``enabled`` is the canonical 0011 desired state.  Keep the legacy values
# during the compatibility window so pre-migration rows and direct callers
# continue to fail/serve according to their existing state until migration
# maps them explicitly.
_ACTIVE_BINDING_STATES = frozenset({"pending", "ready", "enabled"})


class AgentTokenAuthError(Exception):
    """Authentication failure; always an unauthenticated 401.

    The reason is carried for server-side logging only and must not be
    echoed to the caller in a way that reveals token or binding state.
    """

    status_code = 401

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AgentTokenPrincipal(BaseModel):
    """Authenticated binding scope for one MCP request.

    ``instance_id`` is the Term the binding is attached to; tools never
    accept a caller-controlled ``term_id`` (plan §10).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: UUID
    instance_id: UUID
    scopes: frozenset[str] = Field(default_factory=frozenset)
    runtime_epoch: int = Field(ge=0)


class AgentTokenAuthenticator:
    """DI-friendly authenticator resolving raw bearer tokens to principals."""

    def __init__(self, repositories: RepositoryBundle) -> None:
        self._repositories = repositories

    async def authenticate(self, token: str, *, now: datetime | None = None) -> AgentTokenPrincipal:
        observed = now or datetime.now(UTC)
        if not token:
            raise AgentTokenAuthError("agent token required")
        token_row = await self._repositories.agent_tokens.get_by_hash(hash_token(token))
        if token_row is None:
            # Unknown or revoked: the lookup already excludes revoked rows.
            raise AgentTokenAuthError("unknown or revoked agent token")
        if token_row.expiry_epoch <= int(observed.timestamp()):
            raise AgentTokenAuthError("agent token expired")
        binding = await self._repositories.agent_bindings.get_by_id(token_row.binding_id)
        if binding is None:
            raise AgentTokenAuthError("agent token binding does not exist")
        if binding.status not in _ACTIVE_BINDING_STATES:
            raise AgentTokenAuthError("agent token binding is not active")
        if binding.runtime_epoch is None:
            # Fail closed until the supervisor provisions the runtime.
            raise AgentTokenAuthError("binding runtime is not provisioned")
        if token_row.binding_epoch != binding.runtime_epoch:
            raise AgentTokenAuthError("agent token epoch does not match the binding runtime epoch")
        try:
            scopes = frozenset(decode_scopes(token_row.scopes))
        except ValueError as exc:
            raise AgentTokenAuthError("agent token scopes are malformed") from exc
        return AgentTokenPrincipal(
            binding_id=token_row.binding_id,
            instance_id=binding.term_id,
            scopes=scopes,
            runtime_epoch=binding.runtime_epoch,
        )


def require_agent_token(
    authenticator: AgentTokenAuthenticator,
) -> Callable[[str], Awaitable[AgentTokenPrincipal]]:
    """Build a DI-style dependency closing over the authenticator.

    The returned callable accepts the raw bearer token and returns the
    authenticated :class:`AgentTokenPrincipal`, raising
    :class:`AgentTokenAuthError` (401) on every failure.  In a FastAPI-style
    composition root: ``Depends(require_agent_token(authenticator))``.
    """

    async def dependency(token: str) -> AgentTokenPrincipal:
        return await authenticator.authenticate(token)

    return dependency
