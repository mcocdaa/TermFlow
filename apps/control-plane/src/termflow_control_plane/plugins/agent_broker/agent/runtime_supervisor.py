"""Deployment-owned agent runtime supervisor connector (plan §6.2.1, §16).

``SupervisorConnector`` implements the ``AgentRuntimeSupervisor`` lifecycle
authority without any Docker control inside B.  The connector is a pure
attestation/orchestration layer over two injected dependencies:

- a *runtime client*: an async adapter for the deployment-owned OpenCode
  endpoint (production uses an authenticated HTTP health client; tests inject
  a fake), and
- a *secret provider*: a callable returning the binding-scoped MCP capability
  secret on demand.  B never stores the raw token long-term; only its digest
  is retained by :class:`EpochBoundCapability`, and the raw token is handed to
  the runtime exactly once during a restart and then discarded.  No raw token
  is ever written into a shared image or volume by B (plan §16, §6.2.1).

A ``LocalDockerSupervisorFixture`` that shells out to ``docker`` is
deliberately NOT provided: Docker control belongs to deployment tooling, not
to B (plan §6.2.1).  Unit tests use an injected ``FakeRuntimeClient``; the
reference Compose service has a separate opt-in real-container smoke test and
an inspect-only security evidence script.

Fail-closed semantics mirrored from ``tests/fakes.py``:

- a runtime is admitted only when health reports ``READY`` with the expected
  epoch;
- a drain timeout blocks activation and MCP tool calls;
- a restart immediately fences the old epoch and stays not-ready until health
  proves the new epoch;
- cleanup drops all connector-side attestation state.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Protocol, runtime_checkable

from termflow_control_plane.plugins.protocol import (
    BackendOperationResult,
    CapabilityRef,
    DrainStatus,
    RuntimeHealth,
    RuntimeRef,
    RuntimeStatus,
)


class RuntimeSupervisorError(RuntimeError):
    """Base class for supervisor connector failures (always fail closed)."""


class RuntimeNotReadyError(RuntimeSupervisorError):
    """The runtime cannot prove ``READY`` at the moment of attestation."""


class EpochAttestationError(RuntimeSupervisorError):
    """The runtime reports an epoch that does not match the expected epoch."""


class UnknownRuntimeError(RuntimeSupervisorError):
    """An operation was requested for a runtime the supervisor never registered."""


class RuntimeBindingConflictError(RuntimeSupervisorError):
    """A runtime is already bound to a different Agent Binding."""


class SecretUnavailableError(RuntimeSupervisorError):
    """The secret provider could not supply an epoch-bound capability secret."""


class CapabilityReuseAcrossEpochsError(RuntimeSupervisorError):
    """The same raw capability token was provisioned for two different epochs."""


SecretProvider = Callable[[CapabilityRef, int], str]
"""Returns the binding-scoped MCP capability secret for an epoch on demand.

The deployment guarantees the secret is stable within one epoch and fresh
across epochs: rotation must invalidate the previous token.
"""


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@runtime_checkable
class RuntimeClient(Protocol):
    """Deployment-owned runtime manager adapter injected into the connector.

    The connector never drives Docker itself; production wiring uses an
    authenticated HTTP client for the deployment-owned OpenCode endpoint.
    """

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth: ...

    async def quiesce(self, runtime_ref: RuntimeRef, deadline: datetime) -> DrainStatus: ...

    async def restart(
        self, runtime_ref: RuntimeRef, epoch: int, capability_secret: str
    ) -> RuntimeHealth: ...

    async def cleanup(self, runtime_ref: RuntimeRef) -> BackendOperationResult: ...


class EpochBoundCapability:
    """Epoch-bound MCP capability secret lifecycle (plan §6.2.1, §16).

    Each epoch gets a freshly provisioned raw token.  Only its SHA-256 digest
    is retained in B; the raw token is handed to the runtime exactly once for
    secret/config injection and is then discarded.  B therefore never holds a
    long-lived raw token and never writes one into a shared image or volume.

    Provisioning the same raw token for two different epochs fails closed:
    a rotation that does not change the token would not invalidate the
    previous epoch's capability.
    """

    def __init__(self) -> None:
        self._digests: dict[tuple[CapabilityRef, int], str] = {}

    def provision(self, capability_ref: CapabilityRef, epoch: int, raw_token: str) -> None:
        """Record the digest of a fresh epoch-bound raw token.

        The raw token itself must be handed to the runtime by the caller and
        then discarded; only the digest is stored here.
        """
        if not raw_token or not raw_token.strip():
            raise ValueError("capability secret must not be empty")
        digest = _hash_secret(raw_token)
        for (other_ref, other_epoch), other_digest in self._digests.items():
            if other_digest == digest and (other_ref, other_epoch) != (capability_ref, epoch):
                raise CapabilityReuseAcrossEpochsError(
                    "the same capability secret was provisioned for multiple epochs; "
                    "rotation must yield a fresh token"
                )
        self._digests[(capability_ref, epoch)] = digest

    def verify(self, capability_ref: CapabilityRef, epoch: int, presented: str) -> bool:
        """Fail-closed verification of a presented token against the stored digest."""
        stored = self._digests.get((capability_ref, epoch))
        if stored is None:
            return False
        return hmac.compare_digest(stored, _hash_secret(presented))

    def revoke(self, capability_ref: CapabilityRef, epoch: int) -> None:
        """Forget the digest for one (capability, epoch) pair."""
        self._digests.pop((capability_ref, epoch), None)


@dataclass(slots=True)
class _AttestedRuntime:
    binding_id: str
    epoch: int
    capability_ref: CapabilityRef
    ready: bool
    drain_status: DrainStatus = DrainStatus.NOT_ATTEMPTED


class SupervisorConnector:
    """Deployment-owned ``AgentRuntimeSupervisor`` with no Docker control.

    Attests runtime health/epoch, rotates the epoch-bound capability through
    the injected ``secret_provider`` + ``RuntimeClient``, and exposes the same
    fail-closed activation/tool-call gates the broker relies on
    (``tests/fakes.py`` semantics).
    """

    def __init__(
        self,
        runtime_client: RuntimeClient,
        secret_provider: SecretProvider,
        *,
        health_poll_attempts: int = 20,
        health_poll_delay_seconds: float = 0.05,
    ) -> None:
        if runtime_client is None:
            raise ValueError("a runtime client is required")
        if secret_provider is None or not callable(secret_provider):
            raise ValueError("a callable secret provider is required")
        if health_poll_attempts < 1:
            raise ValueError("health_poll_attempts must be at least 1")
        if health_poll_delay_seconds < 0:
            raise ValueError("health_poll_delay_seconds must not be negative")
        self._client = runtime_client
        self._secret_provider = secret_provider
        self._health_poll_attempts = health_poll_attempts
        self._health_poll_delay = health_poll_delay_seconds
        self._runtimes: dict[RuntimeRef, _AttestedRuntime] = {}
        self._capabilities = EpochBoundCapability()

    # -- AgentRuntimeSupervisor -------------------------------------------------

    async def register(
        self,
        binding_id: str,
        runtime_ref: RuntimeRef,
        epoch: int,
        capability_ref: CapabilityRef,
    ) -> None:
        """Attest a runtime and admit its binding, fail-closed on any doubt.

        The runtime must report ``READY`` with exactly the expected epoch;
        a not-ready/unknown runtime, an epoch mismatch, an epoch regression,
        or a second binding on the same runtime is rejected.
        """
        if not binding_id or not binding_id.strip():
            raise ValueError("binding_id must not be empty")
        if epoch < 1:
            raise ValueError("epoch must be at least 1")
        existing = self._runtimes.get(runtime_ref)
        if existing is not None and existing.binding_id != binding_id:
            raise RuntimeBindingConflictError(
                f"runtime {runtime_ref} is already bound to {existing.binding_id!r}; "
                "one binding per runtime (plan §6.2.1)"
            )
        if existing is not None and epoch < existing.epoch:
            raise EpochAttestationError(
                f"epoch regression for {runtime_ref}: {epoch} < attested {existing.epoch}"
            )
        observed = await self._client.health(runtime_ref)
        if observed.status is not RuntimeStatus.READY:
            raise RuntimeNotReadyError(
                f"runtime {runtime_ref} is {observed.status.value}, not ready; "
                "binding admission fails closed"
            )
        if observed.epoch != epoch:
            raise EpochAttestationError(
                f"runtime {runtime_ref} reports epoch {observed.epoch}, "
                f"expected {epoch}"
            )
        self._runtimes[runtime_ref] = _AttestedRuntime(
            binding_id=binding_id,
            epoch=epoch,
            capability_ref=capability_ref,
            ready=True,
        )

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth:
        """Report runtime health overlaid with the connector's attestation.

        Once a runtime is attested, anything other than ``READY`` with the
        attested epoch is reported as ``NOT_READY`` and revokes the connector's
        trust: the runtime must re-register or restart with the current epoch
        before B accepts MCP calls again (plan §6.2.1 fail-closed drift guard).
        """
        observed = await self._client.health(runtime_ref)
        attested = self._runtimes.get(runtime_ref)
        if attested is None:
            return observed
        if (
            attested.ready
            and observed.status is RuntimeStatus.READY
            and observed.epoch == attested.epoch
        ):
            return observed
        attested.ready = False
        return RuntimeHealth(
            status=RuntimeStatus.NOT_READY,
            epoch=attested.epoch,
            observed_at=datetime.now(UTC),
            detail=(
                f"runtime drift: client reports {observed.status.value} "
                f"epoch {observed.epoch}, attested epoch {attested.epoch}"
            ),
        )

    async def quiesce(self, runtime_ref: RuntimeRef, deadline: datetime) -> DrainStatus:
        """Drain the runtime's in-flight MCP calls before the deadline.

        A drain timeout blocks activation and tool calls (fail-closed), so the
        old conversation can never start a new run.
        """
        result = await self._client.quiesce(runtime_ref, deadline)
        attested = self._runtimes.get(runtime_ref)
        if attested is not None:
            attested.drain_status = result
            if result is DrainStatus.DRAIN_TIMEOUT:
                attested.ready = False
        return result

    async def restart(
        self,
        runtime_ref: RuntimeRef,
        epoch: int,
        capability_ref: CapabilityRef,
    ) -> RuntimeHealth:
        """Rotate the epoch-bound capability and restart the runtime.

        The fresh secret is obtained from the secret provider, its digest is
        provisioned, and the raw token is handed to the runtime exactly once.
        The old epoch is fenced immediately; the returned health is ``READY``
        only after health proves the new epoch, ``NOT_READY`` otherwise.
        """
        attested = self._runtimes.get(runtime_ref)
        if attested is None:
            raise UnknownRuntimeError(
                f"runtime {runtime_ref} is not registered; register before restart"
            )
        if epoch < 1:
            raise ValueError("epoch must be at least 1")
        if epoch < attested.epoch:
            raise EpochAttestationError(
                f"restart epoch regression for {runtime_ref}: {epoch} < {attested.epoch}"
            )
        # Fence the currently attested epoch before any injected provider or
        # runtime operation can fail.  A failed restart must never leave the
        # old epoch eligible for tool calls while the runtime transition is
        # unknown.
        attested.ready = False
        attested.drain_status = DrainStatus.NOT_ATTEMPTED
        raw_secret = self._secret_provider(capability_ref, epoch)
        if not raw_secret or not raw_secret.strip():
            raise SecretUnavailableError(
                f"no capability secret available for {capability_ref} epoch {epoch}; "
                "restart fails closed without a fresh capability"
            )
        self._capabilities.provision(capability_ref, epoch, raw_secret)
        # The capability was provisioned for the requested transition.  Move
        # the attestation identity before awaiting the deployment operation so
        # a client exception leaves an explicit, fenced not-ready state for
        # the new epoch rather than making the old epoch appear healthy.
        attested.epoch = epoch
        attested.capability_ref = capability_ref
        await self._client.restart(runtime_ref, epoch, raw_secret)
        # The raw token has now been delivered; nothing retains it here.

        observed: RuntimeHealth | None = None
        for _ in range(self._health_poll_attempts):
            observed = await self._client.health(runtime_ref)
            if observed.status is RuntimeStatus.READY and observed.epoch == epoch:
                attested.ready = True
                return observed
            await asyncio.sleep(self._health_poll_delay)
        return RuntimeHealth(
            status=RuntimeStatus.NOT_READY,
            epoch=epoch,
            observed_at=observed.observed_at if observed is not None else datetime.now(UTC),
            detail="runtime did not prove the expected epoch after restart",
        )

    async def cleanup(self, runtime_ref: RuntimeRef) -> BackendOperationResult:
        """Remove the runtime and forget all connector-side attestation state."""
        result = await self._client.cleanup(runtime_ref)
        self._runtimes.pop(runtime_ref, None)
        return result

    def release(self, runtime_ref: RuntimeRef, binding_id: str) -> None:
        """Forget one closed Binding's attestation for a runtime.

        Revocation/disable retires the Binding, but its runtime association
        lives in connector memory.  Without this release the deployment's
        single runtime stays bound to the closed Binding, so every later
        setup on the same runtime_ref fails admission with an 'already bound'
        conflict until B restarts.
        """
        attested = self._runtimes.get(runtime_ref)
        if attested is not None and attested.binding_id == binding_id:
            self._runtimes.pop(runtime_ref, None)

    async def aclose(self) -> None:
        """Close an optional async transport owned by the runtime client.

        Runtime clients such as :class:`HttpHealthRuntimeClient` own an
        ``httpx.AsyncClient``.  Test doubles and deployment adapters that do
        not hold resources need no close hook, so this remains an optional
        capability rather than part of the lifecycle protocol.
        """
        closer = getattr(self._client, "aclose", None)
        if closer is None:
            return
        result = closer()
        if isawaitable(result):
            await result

    # -- Fail-closed gates (same semantics as tests/fakes.py) -------------------

    def accept_activation(self, runtime_ref: RuntimeRef, epoch: int) -> bool:
        """Admit a new conversation/run on a binding only for a ready runtime."""
        attested = self._runtimes.get(runtime_ref)
        if attested is None:
            return False
        if attested.drain_status is DrainStatus.DRAIN_TIMEOUT:
            return False
        if not attested.ready:
            return False
        return epoch == attested.epoch

    def accept_tool_call(self, runtime_ref: RuntimeRef, epoch: int) -> bool:
        """Admit an MCP tool call only for the currently attested ready epoch."""
        attested = self._runtimes.get(runtime_ref)
        if attested is None:
            return False
        if not attested.ready:
            return False
        return epoch == attested.epoch
