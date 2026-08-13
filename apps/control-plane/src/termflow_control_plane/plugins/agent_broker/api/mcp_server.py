"""MCP server connector over the official MCP Python SDK (plan §10, task M4.4/M5.2).

This module assembles B's MCP capability surface from the handlers in
:mod:`termflow_control_plane.plugins.agent_broker.api.mcp_tools` - the six
observe tools plus the two approval-gated write tools (M5.2) - and exposes
them over the pinned official MCP SDK's Streamable HTTP transport (plan §22:
adopt the SDK directly, keep B's security checks outside it).

SDK 2.0.0 surface used here (documented against the installed 2.0.0):

- :class:`mcp.server.MCPServer` with ``token_verifier`` + ``auth``: the SDK
  mounts ``BearerAuthBackend`` + ``RequireAuthMiddleware`` around the
  Streamable HTTP app, so every request needs a valid bearer token and the
  configured ``required_scopes`` before any MCP message is processed.
- :class:`mcp.server.auth.provider.TokenVerifier` protocol: B implements
  ``verify_token`` over ``require_agent_token`` semantics (hashed AgentToken
  bound to one binding/Term/runtime epoch; revoked/expired/epoch-mismatched
  and admin tokens fail closed - plan §10).
- :class:`mcp.server.mcpserver.context.Context` injection + the SDK's auth
  context (``mcp.server.auth.middleware.auth_context.get_access_token``) so
  tool bodies recover the verified binding identity for per-request scope
  checks.  In-process transports (in-memory/stdio) bypass the HTTP middleware;
  :func:`principal_override` is the explicit test seam for those.
- :class:`mcp.shared.exceptions.MCPError` with ``data``: ``TermFlowToolError``
  is translated into a structured JSON-RPC error carrying the stable
  ``termflow_error_code`` (plan §10: structured MCP errors with stable
  TermFlow error codes).

B owns the security checks around the SDK (plan §10): request/result byte
limits, a per-tool timeout, and per-binding concurrency/rate quotas are
enforced by the guarded wrappers in :func:`build_mcp_server`; unknown tool
calls fail closed; :func:`check_tool_config_drift` refuses to start when the
served tool surface drifts from the pinned OpenCode allowlist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST
from pydantic import AnyHttpUrl, BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from termflow_protocol.mcp import (
    MAX_PANE_READ_BYTES,
    ListPanesResult,
    PaneReadParams,
    PaneReadResult,
    PaneSendKeysParams,
    PaneSendKeysResult,
    PaneSendTextParams,
    PaneSendTextResult,
    TermFlowErrorCode,
    TermFlowToolName,
    WatchCancelParams,
    WatchCancelResult,
    WatchCreateParams,
    WatchCreateResult,
    WatchGetParams,
    WatchGetResult,
    WatchListParams,
    WatchListResult,
)

from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    ContinuationPort,
    TermFlowToolError,
    TerminalCommandPort,
    TerminalObservationPort,
)
from termflow_control_plane.plugins.agent_broker.api import mcp_tools
from termflow_control_plane.plugins.agent_broker.auth import (
    SCOPE_TERMINAL_OBSERVE,
    AgentTokenAuthenticator,
    AgentTokenAuthError,
    AgentTokenPrincipal,
)

logger = logging.getLogger(__name__)

#: Streamable HTTP mount path inside B's API namespace (plan §3.4/§13.1).
MCP_STREAMABLE_HTTP_PATH = "/api/v1/agent/mcp"

#: The complete tool surface served by this milestone (plan §10 + M5.2): the
#: observe tools plus the two approval-gated write tools.  ``list_panes`` and
#: ``watch_list`` take no required arguments; the rest validate against their
#: protocol model.
SERVED_TOOLS: tuple[TermFlowToolName, ...] = (
    TermFlowToolName.LIST_PANES,
    TermFlowToolName.PANE_READ,
    TermFlowToolName.PANE_SEND_TEXT,
    TermFlowToolName.PANE_SEND_KEYS,
    TermFlowToolName.WATCH_CREATE,
    TermFlowToolName.WATCH_LIST,
    TermFlowToolName.WATCH_GET,
    TermFlowToolName.WATCH_CANCEL,
)

#: Tools deferred from the served surface.  Empty since M5.2: the drift check
#: therefore requires the registered surface to equal the pinned allowlist
#: exactly (spec §6).
DEFERRED_WRITE_TOOLS: frozenset[str] = frozenset()

#: The write tools: their handlers receive the MCP request id as
#: ``tool_call_id`` (the replay gate of the approval flow, spec §6).
_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        TermFlowToolName.PANE_SEND_TEXT.value,
        TermFlowToolName.PANE_SEND_KEYS.value,
    }
)

#: Guardrail defaults (B owns the security checks around the SDK, plan §10).
DEFAULT_MAX_REQUEST_BYTES = 256 * 1024
#: Result ceiling: twice the protocol read bound plus room for JSON escaping
#: and watch-list envelopes, so a maximum-size ``pane_read`` still passes.
DEFAULT_MAX_RESULT_BYTES = 2 * MAX_PANE_READ_BYTES + 64 * 1024
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONCURRENT_PER_BINDING = 2
DEFAULT_RATE_PER_SECOND = 4.0
DEFAULT_RATE_BURST = 4
DEFAULT_MAX_TRACKED_BINDINGS = 64
#: The write tools wait synchronously for the human decision; the wait must
#: stay well below the tool timeout so the guard can still settle the call
#: (spec §1: the guardrail validates wait < timeout).
DEFAULT_APPROVAL_WAIT_TIMEOUT_SECONDS = 25.0
#: How long a created approval stays valid before it expires (spec §3).
DEFAULT_APPROVAL_TTL_SECONDS = 300.0

#: Loopback-only Host allowlist default: the MCP endpoint is never exposed to
#: a browser (plan §10) and deployment wiring must name the agent-internal
#: hosts explicitly (plan §16: agent_internal network).
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")

#: ``AuthSettings.issuer_url``/``resource_server_url`` placeholder.  B does not
#: implement the OAuth authorization-server endpoints; the fields are required
#: by ``AuthSettings`` and are only used for metadata when the AS routes are
#: enabled (they are not: tokens are verified through the ``TokenVerifier``
#: hook, plan §22).
ISSUER_URL = AnyHttpUrl("https://termflow.invalid")

#: Claim keys carrying the verified binding identity inside the SDK token.
_CLAIM_BINDING_ID = "binding_id"
_CLAIM_INSTANCE_ID = "instance_id"
_CLAIM_SCOPES = "scopes"
_CLAIM_RUNTIME_EPOCH = "runtime_epoch"


@dataclass(frozen=True, slots=True)
class McpGuardrailConfig:
    """B-side bounds around the SDK (plan §10), all testable and overridable."""

    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    max_concurrent_per_binding: int = DEFAULT_MAX_CONCURRENT_PER_BINDING
    rate_per_second: float = DEFAULT_RATE_PER_SECOND
    rate_burst: int = DEFAULT_RATE_BURST
    max_tracked_bindings: int = DEFAULT_MAX_TRACKED_BINDINGS
    approval_wait_timeout_seconds: float = DEFAULT_APPROVAL_WAIT_TIMEOUT_SECONDS
    approval_ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS

    def __post_init__(self) -> None:
        if self.max_request_bytes < 1:
            raise ValueError("max_request_bytes must be at least 1")
        if self.max_result_bytes < 1:
            raise ValueError("max_result_bytes must be at least 1")
        if self.tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")
        if self.max_concurrent_per_binding < 1:
            raise ValueError("max_concurrent_per_binding must be at least 1")
        if self.rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if self.rate_burst < 1:
            raise ValueError("rate_burst must be at least 1")
        if self.max_tracked_bindings < 1:
            raise ValueError("max_tracked_bindings must be at least 1")
        if self.approval_wait_timeout_seconds <= 0:
            raise ValueError("approval_wait_timeout_seconds must be positive")
        if self.approval_wait_timeout_seconds >= self.tool_timeout_seconds:
            raise ValueError(
                "approval_wait_timeout_seconds must be smaller than tool_timeout_seconds "
                "so the guard can settle a waiting write call"
            )
        if self.approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive")


class ToolConfigDriftError(RuntimeError):
    """The served MCP tool surface does not match the pinned OpenCode allowlist."""


def _termflow_tool_error(exc: TermFlowToolError) -> MCPError:
    """Map a handler failure to a structured MCP error (plan §10).

    The stable ``TermFlowErrorCode`` travels in ``data.termflow_error_code``;
    the JSON-RPC code is transport-level only.  Not-found and malformed-request
    failures map to ``invalid_params``; everything else to ``internal_error``.
    Handler-supplied ``data`` (e.g. an ``approval_id``) is merged in.
    """
    if exc.error_code in {
        TermFlowErrorCode.PANE_NOT_FOUND,
        TermFlowErrorCode.WATCH_NOT_FOUND,
        TermFlowErrorCode.INCARNATION_CHANGED,
        TermFlowErrorCode.INVALID_REQUEST,
    }:
        code = INVALID_PARAMS
    else:
        code = INTERNAL_ERROR
    data: dict[str, object] = {"termflow_error_code": exc.error_code.value}
    if exc.data:
        data.update(exc.data)
    return MCPError(
        code=code,
        message=exc.message,
        data=data,
    )


# ---------------------------------------------------------------------------
# AgentToken authentication (plan §10, §22: the TokenVerifier hook)
# ---------------------------------------------------------------------------


def _principal_claims(principal: AgentTokenPrincipal) -> dict[str, Any]:
    return {
        _CLAIM_BINDING_ID: str(principal.binding_id),
        _CLAIM_INSTANCE_ID: str(principal.instance_id),
        _CLAIM_SCOPES: sorted(principal.scopes),
        _CLAIM_RUNTIME_EPOCH: principal.runtime_epoch,
    }


def principal_from_access_token(token: AccessToken) -> AgentTokenPrincipal:
    """Rebuild the binding principal from a verified token's claims.

    Fail closed: a verified token that does not carry a complete, well-formed
    binding identity is rejected rather than partially trusted.
    """
    claims = token.claims or {}
    try:
        binding_id = UUID(str(claims[_CLAIM_BINDING_ID]))
        instance_id = UUID(str(claims[_CLAIM_INSTANCE_ID]))
        scopes = frozenset(str(scope) for scope in claims[_CLAIM_SCOPES])
        runtime_epoch = int(claims[_CLAIM_RUNTIME_EPOCH])
    except (KeyError, TypeError, ValueError) as exc:
        raise MCPError(
            code=INTERNAL_ERROR,
            message="the verified agent token does not carry a valid binding identity",
        ) from exc
    return AgentTokenPrincipal(
        binding_id=binding_id,
        instance_id=instance_id,
        scopes=scopes,
        runtime_epoch=runtime_epoch,
    )


class AgentTokenVerifier(TokenVerifier):
    """SDK ``TokenVerifier`` over ``require_agent_token`` semantics (plan §22).

    Accepts only a hashed AgentToken bound to one binding/Term/runtime epoch.
    Every failure - unknown, revoked, expired, inactive binding, epoch
    mismatch, malformed scopes - returns ``None`` so the SDK rejects the
    request with 401.  There is no admin/native token fallback (plan §10).
    """

    def __init__(self, authenticator: AgentTokenAuthenticator) -> None:
        self._authenticator = authenticator

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = await self._authenticator.authenticate(token)
        except AgentTokenAuthError:
            return None
        return AccessToken(
            token=token,
            client_id=str(principal.binding_id),
            scopes=sorted(principal.scopes),
            claims=_principal_claims(principal),
        )


#: In-process test seam: the SDK's bearer-token middleware only runs over HTTP
#: transports; in-memory/stdio transports bypass it, so tool calls from tests
#: (and future trusted in-process callers) inject the verified principal here.
#: HTTP requests can never set this contextvar.
_principal_override: ContextVar[AgentTokenPrincipal | None] = ContextVar(
    "termflow_mcp_principal", default=None
)


@asynccontextmanager
async def principal_override(principal: AgentTokenPrincipal) -> AsyncIterator[None]:
    """Run the next in-process tool calls as a specific binding principal.

    Test/embedded transport seam only: over Streamable HTTP the principal
    always comes from the verified bearer token via the SDK auth context.
    """
    token = _principal_override.set(principal)
    try:
        yield
    finally:
        _principal_override.reset(token)


def _require_principal() -> AgentTokenPrincipal:
    """Resolve the authenticated binding for the current tool call, fail closed."""
    access_token = get_access_token()
    if access_token is not None:
        return principal_from_access_token(access_token)
    override = _principal_override.get()
    if override is not None:
        return override
    raise MCPError(
        code=INVALID_REQUEST,
        message="an authenticated agent token is required",
    )


# ---------------------------------------------------------------------------
# per-binding concurrency/rate quota (plan §10)
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Minimal per-binding token bucket (bounded, deterministic, testable)."""

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = rate
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()

    async def consume(self) -> None:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return
        await asyncio.sleep((1.0 - self._tokens) / self._rate)
        self._refill()
        self._tokens -= 1.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now


class PerBindingQuota:
    """Bounded per-binding concurrency and rate quota (plan §10).

    Concurrency is an :class:`asyncio.Semaphore` per binding
    (``max_concurrent_per_binding``); rate is a small token bucket per binding.
    The number of tracked bindings is bounded by ``max_tracked_bindings`` with
    least-recently-used eviction, so memory cannot grow with untrusted traffic.
    """

    def __init__(self, config: McpGuardrailConfig) -> None:
        self._config = config
        self._semaphores: OrderedDict[UUID, asyncio.Semaphore] = OrderedDict()
        self._buckets: OrderedDict[UUID, _TokenBucket] = OrderedDict()

    @asynccontextmanager
    async def acquire(self, binding_id: UUID) -> AsyncIterator[None]:
        """Wait for the binding's concurrency slot and rate token."""
        semaphore = self._semaphore(binding_id)
        bucket = self._bucket(binding_id)
        await semaphore.acquire()
        try:
            await bucket.consume()
            yield
        finally:
            semaphore.release()

    def _semaphore(self, binding_id: UUID) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(binding_id)
        if semaphore is None:
            self._evict_if_full(self._semaphores)
            semaphore = asyncio.Semaphore(self._config.max_concurrent_per_binding)
            self._semaphores[binding_id] = semaphore
        self._semaphores.move_to_end(binding_id)
        return semaphore

    def _bucket(self, binding_id: UUID) -> _TokenBucket:
        bucket = self._buckets.get(binding_id)
        if bucket is None:
            self._evict_if_full(self._buckets)
            bucket = _TokenBucket(self._config.rate_per_second, self._config.rate_burst)
            self._buckets[binding_id] = bucket
        self._buckets.move_to_end(binding_id)
        return bucket

    def _evict_if_full(self, table: OrderedDict[Any, Any]) -> None:
        if len(table) >= self._config.max_tracked_bindings:
            table.popitem(last=False)


# ---------------------------------------------------------------------------
# guarded tool wrappers (B owns the security checks around the SDK, plan §10)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ToolPorts:
    """The typed ports the handlers delegate to (plan §10, §18, M5.2)."""

    observation: TerminalObservationPort
    continuation: ContinuationPort
    commands: TerminalCommandPort
    repositories: RepositoryBundle

    def kwargs_for(self, name: TermFlowToolName) -> dict[str, Any]:
        """Keyword dependencies for the named handler (see ``api/mcp_tools``)."""
        if name in {TermFlowToolName.LIST_PANES, TermFlowToolName.PANE_READ}:
            return {"observation": self.observation, "repositories": self.repositories}
        if name in {TermFlowToolName.PANE_SEND_TEXT, TermFlowToolName.PANE_SEND_KEYS}:
            return {"commands": self.commands, "repositories": self.repositories}
        return {"continuation": self.continuation, "repositories": self.repositories}


async def _run_guarded(
    *,
    name: TermFlowToolName,
    principal: AgentTokenPrincipal,
    params: BaseModel | None,
    param_model: type[BaseModel] | None,
    handler: Callable[..., Awaitable[BaseModel]],
    ports: _ToolPorts,
    quota: PerBindingQuota,
    config: McpGuardrailConfig,
    ctx: Context | None = None,
) -> BaseModel:
    """Execute one handler under the binding quota, timeout, and byte bounds."""
    if params is None and param_model is not None:
        # A client may omit the optional ``params`` argument; the protocol
        # model then validates the call (``WatchListParams`` is valid empty,
        # required-field models fail closed).
        params = param_model()
    async with quota.acquire(principal.binding_id):
        try:
            async with asyncio.timeout(config.tool_timeout_seconds):
                kwargs = ports.kwargs_for(name)
                if name in _WRITE_TOOLS:
                    # The write tools pin replay protection on the MCP
                    # request id; a call without one cannot be approved
                    # (spec §6: no ctx/request_id -> invalid_request).
                    request_id = ctx.request_id if ctx is not None else None
                    if request_id is None:
                        raise MCPError(
                            code=INVALID_REQUEST,
                            message=f"tool {name.value} requires a request id",
                            data={
                                "termflow_error_code": TermFlowErrorCode.INVALID_REQUEST.value
                            },
                        )
                    tool_call_id = str(request_id)
                    if not 1 <= len(tool_call_id) <= 128:
                        raise MCPError(
                            code=INVALID_REQUEST,
                            message="tool call id must be 1-128 characters",
                            data={
                                "termflow_error_code": TermFlowErrorCode.INVALID_REQUEST.value
                            },
                        )
                    kwargs["tool_call_id"] = tool_call_id
                if params is None:
                    result = await handler(principal, **kwargs)
                else:
                    result = await handler(principal, params, **kwargs)
        except TermFlowToolError as exc:
            raise _termflow_tool_error(exc) from exc
        except TimeoutError as exc:
            logger.warning("tool %s timed out after %ss", name.value, config.tool_timeout_seconds)
            raise MCPError(
                code=INTERNAL_ERROR,
                message=f"tool {name.value} timed out",
                data={"termflow_error_code": TermFlowErrorCode.INTERNAL_ERROR.value},
            ) from exc
    encoded = json.dumps(result.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")
    if len(encoded) > config.max_result_bytes:
        logger.warning(
            "tool %s result exceeded the %s byte limit", name.value, config.max_result_bytes
        )
        raise MCPError(
            code=INTERNAL_ERROR,
            message=f"tool {name.value} result exceeded the byte limit",
            data={"termflow_error_code": TermFlowErrorCode.QUOTA_EXCEEDED.value},
        )
    return result


def _guarded_tool(
    *,
    name: TermFlowToolName,
    handler: Callable[..., Awaitable[BaseModel]],
    param_model: type[BaseModel] | None,
    result_model: type[BaseModel],
    ports: _ToolPorts,
    quota: PerBindingQuota,
    config: McpGuardrailConfig,
) -> Callable[..., Awaitable[BaseModel]]:
    """Build the SDK tool callable wrapping one observe or write handler.

    Tools without arguments (``list_panes``, ``watch_list``) get a bare
    signature; the rest validate their arguments against the protocol model
    through the SDK's type-to-schema support.  Results are returned as their
    protocol model so the SDK advertises the typed output schema (plan §10).
    """

    if param_model is None:

        async def call(ctx: Context | None = None) -> result_model:  # type: ignore[valid-type]
            principal = _require_principal()
            return await _run_guarded(
                name=name,
                principal=principal,
                params=None,
                param_model=None,
                handler=handler,
                ports=ports,
                quota=quota,
                config=config,
                ctx=ctx,
            )

        # ``from __future__ import annotations`` stores annotations as strings;
        # bind the real type objects so the SDK's ``inspect.signature(
        # eval_str=True)`` sees them (the closure ``param_model`` could not be
        # resolved from the module globals otherwise).
        call.__annotations__ = {
            "ctx": Context | None,
            "return": result_model,
        }

    else:

        async def call(  # type: ignore[misc]
            params: param_model | None = None,  # type: ignore[valid-type]
            ctx: Context | None = None,
        ) -> result_model:  # type: ignore[valid-type]
            principal = _require_principal()
            return await _run_guarded(
                name=name,
                principal=principal,
                params=params,
                param_model=param_model,
                handler=handler,
                ports=ports,
                quota=quota,
                config=config,
                ctx=ctx,
            )

        call.__annotations__ = {
            "params": param_model | None,
            "ctx": Context | None,
            "return": result_model,
        }

    call.__name__ = name.value
    call.__qualname__ = f"termflow_mcp.{name.value}"
    return call


def _register_tools(
    *,
    server: MCPServer,
    ports: _ToolPorts,
    quota: PerBindingQuota,
    config: McpGuardrailConfig,
) -> None:
    """Register exactly the served tools (observe + approval-gated writes)."""
    specs: list[
        tuple[
            TermFlowToolName,
            Callable[..., Awaitable[BaseModel]],
            type[BaseModel] | None,
            type[BaseModel],
        ]
    ] = [
        (TermFlowToolName.LIST_PANES, mcp_tools.handle_list_panes, None, ListPanesResult),
        (TermFlowToolName.PANE_READ, mcp_tools.handle_pane_read, PaneReadParams, PaneReadResult),
        (
            TermFlowToolName.PANE_SEND_TEXT,
            mcp_tools.handle_pane_send_text,
            PaneSendTextParams,
            PaneSendTextResult,
        ),
        (
            TermFlowToolName.PANE_SEND_KEYS,
            mcp_tools.handle_pane_send_keys,
            PaneSendKeysParams,
            PaneSendKeysResult,
        ),
        (
            TermFlowToolName.WATCH_CREATE,
            mcp_tools.handle_watch_create,
            WatchCreateParams,
            WatchCreateResult,
        ),
        (
            TermFlowToolName.WATCH_LIST,
            mcp_tools.handle_watch_list,
            WatchListParams,
            WatchListResult,
        ),
        (TermFlowToolName.WATCH_GET, mcp_tools.handle_watch_get, WatchGetParams, WatchGetResult),
        (
            TermFlowToolName.WATCH_CANCEL,
            mcp_tools.handle_watch_cancel,
            WatchCancelParams,
            WatchCancelResult,
        ),
    ]
    # Self-check: the registered surface must equal the declared served set,
    # so the milestone contract cannot silently drift (plan §10, M5.2).
    if tuple(name for name, _, _, _ in specs) != SERVED_TOOLS:
        raise ToolConfigDriftError(
            "the tool registration drifted from SERVED_TOOLS"
        )
    for name, handler, param_model, result_model in specs:
        server.add_tool(
            _guarded_tool(
                name=name,
                handler=handler,
                param_model=param_model,
                result_model=result_model,
                ports=ports,
                quota=quota,
                config=config,
            ),
            name=name.value,
            description=handler.__doc__ or name.value,
        )


# ---------------------------------------------------------------------------
# assembly (plan §10, §22)
# ---------------------------------------------------------------------------


def build_mcp_server(
    observation: TerminalObservationPort,
    continuation: ContinuationPort,
    policy_checker: RepositoryBundle,
    token_auth: AgentTokenAuthenticator,
    *,
    sessions: async_sessionmaker[AsyncSession],
    commands: TerminalCommandPort,
    guardrails: McpGuardrailConfig | None = None,
    name: str = "termflow",
    version: str = "0.2.0-dev.0",
) -> MCPServer:
    """Assemble the MCP server from the existing handlers.

    ``policy_checker`` is the repository bundle the handlers use for pane
    policy and conversation ownership checks (plan §10); ``token_auth`` is
    the ``require_agent_token`` authenticator the SDK ``TokenVerifier`` hook
    delegates to (plan §22); ``sessions`` is the session factory the
    caller's approval flow was built on and ``commands`` is the
    approval-gated :class:`TerminalCommandPort` the write handlers delegate
    to (M5.2).  The returned :class:`~mcp.server.MCPServer` enforces bearer
    auth with the ``terminal.observe`` scope at the HTTP gate (the write
    tools add their own ``terminal.write`` gate inside the handlers) and
    guards every tool call with the configured B-side bounds.
    """
    config = guardrails or McpGuardrailConfig()
    quota = PerBindingQuota(config)
    ports = _ToolPorts(
        observation=observation,
        continuation=continuation,
        commands=commands,
        repositories=policy_checker,
    )
    server = MCPServer(
        name=name,
        version=version,
        token_verifier=AgentTokenVerifier(token_auth),
        auth=AuthSettings(
            issuer_url=ISSUER_URL,
            # ``resource_server_url`` is required by ``AuthSettings``; it names
            # this MCP endpoint as the protected resource identifier.  B never
            # enables the OAuth authorization-server routes; ``TokenVerifier``
            # is the only token path (plan §22).
            resource_server_url=AnyHttpUrl(f"{ISSUER_URL}{MCP_STREAMABLE_HTTP_PATH}"),
            required_scopes=[SCOPE_TERMINAL_OBSERVE],
        ),
    )
    _register_tools(server=server, ports=ports, quota=quota, config=config)
    return server


def create_streamable_http_app(
    server: MCPServer,
    *,
    path: str = MCP_STREAMABLE_HTTP_PATH,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    allowed_hosts: Sequence[str] = DEFAULT_ALLOWED_HOSTS,
    allowed_origins: Sequence[str] = (),
) -> Starlette:
    """Return the SDK's Streamable HTTP ASGI app with B's transport bounds.

    ``max_request_bytes`` is the request body ceiling (the SDK answers with
    ``413`` beyond it); ``allowed_hosts``/``allowed_origins`` feed the SDK's
    DNS-rebinding protection.  MCP Streamable HTTP is never exposed to a
    browser (plan §10): only deployment-owned agent-internal hosts belong in
    ``allowed_hosts`` (plan §16).
    """
    return server.streamable_http_app(
        streamable_http_path=path,
        max_request_body_size=max_request_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
            allowed_origins=list(allowed_origins),
        ),
    )


# ---------------------------------------------------------------------------
# config drift guard (plan §10, M0.3)
# ---------------------------------------------------------------------------


def check_tool_config_drift(
    registered_tools: Iterable[str],
    pinned_allowlist: Iterable[str],
    *,
    deferred_tools: Collection[str] = DEFERRED_WRITE_TOOLS,
) -> None:
    """Fail closed when the served tool surface drifts from the pinned config.

    The M0.3 frozen OpenCode config allowlists the exact TermFlow MCP tools.
    Since M5.2 the deferred set is empty, so the registered surface must
    equal the pinned allowlist exactly - an un-reviewed tool, a missing
    served tool, or an allowlist losing a served tool all refuse startup.
    """
    registered = frozenset(registered_tools)
    allowlist = frozenset(pinned_allowlist)
    unreviewed = registered - allowlist
    if unreviewed:
        raise ToolConfigDriftError(
            "the MCP server would serve tools that are not in the pinned "
            f"OpenCode allowlist: {sorted(unreviewed)}"
        )
    expected = allowlist - frozenset(deferred_tools)
    if registered != expected:
        detail: list[str] = []
        if expected - registered:
            detail.append(f"missing {sorted(expected - registered)}")
        if registered - expected:
            detail.append(f"unexpected {sorted(registered - expected)}")
        raise ToolConfigDriftError(
            "the registered MCP tool surface drifted from the pinned OpenCode "
            f"allowlist: {'; '.join(detail)}"
        )


def pinned_allowlist_from_fixture(config_text: str) -> frozenset[str]:
    """Parse the frozen OpenCode config fixture (JSON-in-YAML, plan M0.3).

    Returns the ``permission`` keys that name TermFlow MCP tools; everything
    else (``*`` deny, built-in tools) is ignored.  Raises on malformed input.
    """
    # The fixture body is JSON with comment lines; strip those, then parse.
    body = "".join(
        line for line in config_text.splitlines() if not line.lstrip().startswith("#")
    )
    try:
        config = json.loads(body)
    except ValueError as exc:
        raise ValueError("the pinned OpenCode config is not valid JSON") from exc
    permission = config.get("permission")
    if not isinstance(permission, dict):
        raise ValueError("the pinned OpenCode config has no permission section")
    return frozenset(
        key
        for key in permission
        if key != "*" and key.startswith("termflow_")
    )
