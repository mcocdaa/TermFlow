"""Production HTTP-health ``RuntimeClient`` for the supervisor connector.

Scope decision (2026-08-22, user-approved simplification of plan §6.2.1):
the reference deployment performs no epoch-bound capability injection and no
container orchestration from B.  Admission is reduced to an authenticated
``GET /global/health`` probe against the configured OpenCode endpoint:

- ``health`` reports ``READY`` only while the endpoint answers the probe with
  HTTP 200 and the pinned JSON shape contains boolean ``healthy: true``; the reported
  epoch is resolved through the injected :term:`epoch resolver` so the
  supervisor's register/attest semantics stay unchanged.
- ``quiesce`` returns ``DRAINED`` immediately: the broker's
  one-active-run-per-binding invariant already serializes in-flight MCP tool
  calls, so there is nothing extra to drain at this layer.
- ``restart`` re-probes and reports health at the requested epoch; Compose
  owns the actual container lifecycle, and the capability secret handed here
  is already interpolated into the container config through OpenCode's
  native ``{env:}`` substitution (spike-verified on the pinned image).
- ``cleanup`` is a best-effort confirmed result; no runtime state lives here.

Retained protections: hashed agent-token authentication on the MCP endpoint,
the exact TermFlow tool allowlist, the registry's shared-endpoint isolation
guard, the supervisor's epoch-regression fencing, and binding revocation.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    SecretUnavailableError,
)
from termflow_control_plane.plugins.protocol import (
    BackendOperationOutcome,
    BackendOperationResult,
    CapabilityRef,
    DrainStatus,
    RuntimeHealth,
    RuntimeRef,
    RuntimeStatus,
)

EpochResolver = Callable[[RuntimeRef], Awaitable[int]]
SecretProvider = Callable[[CapabilityRef, int], str]

_DEFAULT_PROBE_TIMEOUT_SECONDS = 3.0
_MAX_PROBE_TIMEOUT_SECONDS = 30.0
_DEFAULT_RESTART_PROBE_ATTEMPTS = 20
_DEFAULT_RESTART_PROBE_DELAY_SECONDS = 0.25
_MAX_RESTART_PROBE_DELAY_SECONDS = 5.0
_MAX_RESTART_PROBE_ATTEMPTS = 100

# These values are deliberately the complete public vocabulary for this
# client.  In particular, probe failures must not expose the request URL,
# response body, headers, or exception text.
PUBLIC_READINESS_REASON_CODES = frozenset(
    {
        "runtime_unreachable",
        "mcp_not_connected",
        "runtime_epoch_unresolved",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeReadinessProbe:
    """Bounded, persistence-safe result of the two runtime admission probes."""

    healthy: bool
    mcp_connected: bool
    reason_code: str | None


def _observed_at() -> datetime:
    return datetime.now(UTC)


class HttpHealthRuntimeClient:
    """Admit a binding's runtime while its OpenCode endpoint proves healthy."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        directory: str | None = None,
        epoch_resolver: EpochResolver | None = None,
        probe_timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
        restart_probe_attempts: int = _DEFAULT_RESTART_PROBE_ATTEMPTS,
        restart_probe_delay_seconds: float = _DEFAULT_RESTART_PROBE_DELAY_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("a runtime base URL is required")
        self._base_url = base_url.strip().rstrip("/")
        if directory is not None and not directory.strip():
            raise ValueError("runtime directory must be a non-empty path when set")
        # OpenCode scopes MCP status to the session directory, so the probe
        # must address the same directory the adapter runs sessions in.
        self._directory = directory.strip() if directory is not None else None
        self._epoch_resolver = epoch_resolver
        if not math.isfinite(probe_timeout_seconds) or not (
            0 < probe_timeout_seconds <= _MAX_PROBE_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "probe_timeout_seconds must be finite and between 0 and "
                f"{_MAX_PROBE_TIMEOUT_SECONDS} seconds"
            )
        if not isinstance(restart_probe_attempts, int) or not (
            1 <= restart_probe_attempts <= _MAX_RESTART_PROBE_ATTEMPTS
        ):
            raise ValueError(
                f"restart_probe_attempts must be between 1 and {_MAX_RESTART_PROBE_ATTEMPTS}"
            )
        if not math.isfinite(restart_probe_delay_seconds) or not (
            0 <= restart_probe_delay_seconds <= _MAX_RESTART_PROBE_DELAY_SECONDS
        ):
            raise ValueError(
                "restart_probe_delay_seconds must be finite and between 0 and "
                f"{_MAX_RESTART_PROBE_DELAY_SECONDS} seconds"
            )
        self._probe_timeout_seconds = probe_timeout_seconds
        self._restart_probe_attempts = restart_probe_attempts
        self._restart_probe_delay_seconds = restart_probe_delay_seconds
        if (username is None) != (password is None):
            raise ValueError("runtime Basic auth username and password must be set together")
        basic_auth: tuple[str, str] | None = None
        if username is not None and password is not None:
            basic_auth = (username, password)
        self._client = httpx.AsyncClient(
            timeout=probe_timeout_seconds,
            auth=basic_auth,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _resolve_epoch(self, runtime_ref: RuntimeRef) -> int | None:
        """Resolve the runtime's expected epoch; ``None`` on any doubt."""
        if self._epoch_resolver is None:
            return 1
        try:
            resolved = await self._epoch_resolver(runtime_ref)
        except Exception:
            return None
        if resolved < 1:
            return None
        return resolved

    async def _get_json(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> tuple[object | None, bool]:
        """Fetch one JSON endpoint without retaining any failure diagnostics."""
        try:
            response = await self._client.get(f"{self._base_url}{path}", params=params)
        except Exception:
            # This boundary intentionally includes transport, timeout, and
            # protocol errors from httpx.  The exception is never public.
            return None, False
        if response.status_code != 200:
            return None, False
        try:
            return response.json(), True
        except Exception:
            # JSON decoding and response-content errors are also fail closed.
            return None, False

    async def readiness(self, runtime_ref: RuntimeRef) -> RuntimeReadinessProbe:
        """Probe health and then MCP connectivity, returning only safe codes."""
        del runtime_ref
        health_body, health_ok = await self._get_json("/global/health")
        # The pinned OpenCode contract requires a JSON object whose healthy
        # field is the boolean literal true.  Do not let an HTML proxy page,
        # a missing field, or a truthy string attest runtime readiness.
        if (
            not health_ok
            or not isinstance(health_body, dict)
            or health_body.get("healthy") is not True
        ):
            return RuntimeReadinessProbe(False, False, "runtime_unreachable")

        mcp_params = {"directory": self._directory} if self._directory else None
        mcp_body, mcp_ok = await self._get_json("/mcp", params=mcp_params)
        # /mcp is an object map.  Only the exact lowercase TermFlow key and
        # exact connected status constitute readiness; aliases are rejected.
        if (
            not mcp_ok
            or not isinstance(mcp_body, dict)
            or not isinstance(mcp_body.get("termflow"), dict)
            or mcp_body["termflow"].get("status") != "connected"
        ):
            return RuntimeReadinessProbe(True, False, "mcp_not_connected")
        return RuntimeReadinessProbe(True, True, None)

    async def _probe_once(self, runtime_ref: RuntimeRef) -> bool:
        """Return whether both health and TermFlow MCP probes succeed."""
        result = await self.readiness(runtime_ref)
        return result.healthy and result.mcp_connected

    def _unreachable(self, runtime_ref: RuntimeRef, detail: str) -> RuntimeHealth:
        del runtime_ref
        return RuntimeHealth(
            status=RuntimeStatus.UNKNOWN,
            epoch=1,
            observed_at=_observed_at(),
            detail=detail,
        )

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth:
        epoch = await self._resolve_epoch(runtime_ref)
        if epoch is None:
            return self._unreachable(
                runtime_ref,
                "runtime_epoch_unresolved",
            )
        probe = await self.readiness(runtime_ref)
        if not probe.healthy or not probe.mcp_connected:
            return RuntimeHealth(
                status=RuntimeStatus.UNKNOWN,
                epoch=epoch,
                observed_at=_observed_at(),
                detail=probe.reason_code or "runtime_unreachable",
            )
        return RuntimeHealth(
            status=RuntimeStatus.READY,
            epoch=epoch,
            observed_at=_observed_at(),
            detail="ready",
        )

    async def quiesce(self, runtime_ref: RuntimeRef, deadline: datetime) -> DrainStatus:
        del runtime_ref, deadline
        # The one-active-run-per-binding invariant serializes every in-flight
        # MCP tool call, so this layer has nothing additional to drain.
        return DrainStatus.DRAINED

    async def restart(
        self, runtime_ref: RuntimeRef, epoch: int, capability_secret: str
    ) -> RuntimeHealth:
        del capability_secret  # compose owns the container config lifecycle
        if epoch < 1:
            raise ValueError("epoch must be at least 1")
        last_reason = "runtime_unreachable"
        for attempt in range(self._restart_probe_attempts):
            probe = await self.readiness(runtime_ref)
            if probe.reason_code is not None:
                last_reason = probe.reason_code
            if probe.healthy and probe.mcp_connected:
                return RuntimeHealth(
                    status=RuntimeStatus.READY,
                    epoch=epoch,
                    observed_at=_observed_at(),
                    detail="ready",
                )
            if attempt + 1 < self._restart_probe_attempts:
                await asyncio.sleep(self._restart_probe_delay_seconds)
        return RuntimeHealth(
            status=RuntimeStatus.NOT_READY,
            epoch=epoch,
            observed_at=_observed_at(),
            detail=last_reason,
        )

    async def cleanup(self, runtime_ref: RuntimeRef) -> BackendOperationResult:
        del runtime_ref
        # No connector-side runtime state exists to clean up: container data
        # lives on the dedicated opencode-data volume owned by Compose.
        return BackendOperationResult(
            outcome=BackendOperationOutcome.CONFIRMED,
            message="no connector-side runtime state requires cleanup",
        )


async def resolve_binding_runtime_epoch(
    runtime_ref: RuntimeRef,
    *,
    get_by_runtime_ref: Callable[[str], Awaitable[object]],
) -> int:
    """Default epoch resolver: look up the binding provisioned for the ref.

    Raises when no binding carries the ref or its epoch is unset, which the
    client turns into an ``UNKNOWN`` health report (fail closed).
    """

    binding = await get_by_runtime_ref(str(runtime_ref))
    epoch = getattr(binding, "runtime_epoch", None)
    if epoch is None:
        raise LookupError(f"no binding epoch is provisioned for runtime {runtime_ref}")
    return int(epoch)


def settings_capability_secret_provider(
    token_getter: Callable[[], str | None],
) -> SecretProvider:
    """Build the supervisor secret provider from deployment settings.

    The raw capability secret lives only in the deployment environment (and,
    via ``{env:}`` interpolation, the container process); it is never stored
    by B.  Restarting without a configured secret fails closed.
    """

    def provide(capability_ref: CapabilityRef, epoch: int) -> str:
        raw = token_getter()
        if raw is None or not raw.strip():
            raise SecretUnavailableError(
                f"no agent OpenCode MCP capability secret is configured for "
                f"{capability_ref} epoch {epoch}; set "
                "TERMFLOW_AGENT_OPENCODE_MCP_TOKEN to enable runtime restarts"
            )
        return raw

    return provide
