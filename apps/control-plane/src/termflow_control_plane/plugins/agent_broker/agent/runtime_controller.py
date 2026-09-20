"""Desired-to-observed runtime reconciliation for Agent Bindings.

The controller is the only application service that turns an enabled desired
Binding into a published pipeline.  It serializes work per Binding and also
serializes endpoint mutations globally, so a started candidate is never made
visible until the final desired-state CAS succeeds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import AgentBinding, AgentRuntimeBinding
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.provider_catalog import (
    ProviderCatalog,
    canonicalize_profile_config,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    AgentRuntimeRegistry,
    RuntimeCandidate,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    RuntimeNotReadyError,
    SupervisorConnector,
)
from termflow_control_plane.plugins.protocol import RuntimeRef, RuntimeStatus

_RUNTIME_ASSIGNMENT_CONFLICT = "runtime_assignment_conflict"
_RUNTIME_UNREACHABLE = "runtime_unreachable"
_MCP_NOT_CONNECTED = "mcp_not_connected"
_PIPELINE_START_FAILED = "pipeline_start_failed"
_DISCLOSURE_REQUIRED = "binding_disclosure_required"
_DISCLOSURE_STALE = "binding_disclosure_stale"
_BINDING_STATE_CONFLICT = "binding_state_conflict"
_DEPLOYMENT_REQUIRED = "deployment_required"
_ACTIVE_DESIRED_STATES = frozenset({"enabled", "pending", "ready"})
_TRANSIENT_RUNTIME_FAILURES = frozenset({_RUNTIME_UNREACHABLE, _MCP_NOT_CONNECTED})

FenceHook = Callable[[UUID], Awaitable[object]]
BootstrapCapabilityChecker = Callable[[AgentBinding], Awaitable[bool]]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeReconcileResult:
    binding_id: UUID
    readiness: str
    reason_code: str | None
    applied_revision: int | None


class AgentRuntimeController:
    """Reconcile desired Binding rows into atomically published runtimes."""

    def __init__(
        self,
        *,
        repositories: RepositoryBundle,
        registry: AgentRuntimeRegistry,
        supervisor: SupervisorConnector,
        provider_catalog: ProviderCatalog,
        approval_fencer: FenceHook | None = None,
        stream_fencer: FenceHook | None = None,
        bootstrap_capability_checker: BootstrapCapabilityChecker | None = None,
    ) -> None:
        self._repositories = repositories
        self._registry = registry
        self._supervisor = supervisor
        self._provider_catalog = provider_catalog
        self._approval_fencer = approval_fencer
        self._stream_fencer = stream_fencer
        self._bootstrap_capability_checker = bootstrap_capability_checker
        self._binding_locks: dict[UUID, asyncio.Lock] = {}
        self._endpoint_lock = asyncio.Lock()

    async def reconcile(self, binding_id: UUID) -> RuntimeReconcileResult:
        """Converge one desired Binding without restarting the B process."""

        async with self._binding_lock(binding_id):
            return await self._reconcile_locked(binding_id)

    async def reconcile_all(self) -> list[RuntimeReconcileResult]:
        """Reconcile every persisted Binding in deterministic creation order."""

        bindings = await self._repositories.agent_bindings.list_all()
        results: list[RuntimeReconcileResult] = []
        for binding in bindings:
            results.append(await self.reconcile(binding.id))
        return results

    async def fence(
        self,
        binding_id: UUID,
        rotate_epoch: bool,
    ) -> RuntimeReconcileResult:
        """Synchronously unmap a Binding and optionally invalidate authority.

        Epoch rotation is performed only while the desired row is still
        enabled.  Disable/revoke mutations already rotate through their own
        desired-state transaction and therefore must not be rotated twice.
        """

        async with self._binding_lock(binding_id):
            binding = await self._repositories.agent_bindings.get_by_id(binding_id)
            if binding is None:
                async with self._endpoint_lock:
                    await self._retire_mapping_locked(binding_id)
                await self._fence_capabilities(binding_id, invalidate_tokens=True)
                return RuntimeReconcileResult(
                    binding_id=binding_id,
                    readiness="disabled",
                    reason_code=_BINDING_STATE_CONFLICT,
                    applied_revision=None,
                )

            if rotate_epoch and binding.status in _ACTIVE_DESIRED_STATES:
                if binding.runtime_ref and binding.capability_ref:
                    rotated = await self._repositories.agent_bindings.update_desired_runtime(
                        binding.id,
                        binding.runtime_ref,
                        binding.capability_ref,
                        binding.config_revision,
                        True,
                    )
                    if rotated is not None:
                        binding = rotated

            # Unpublish before any slower secondary fencing work.  Once the
            # desired epoch changed, token preflight also rejects old callers.
            async with self._endpoint_lock:
                await self._retire_mapping_locked(binding_id)

            # A closed desired state has already advanced its epoch in the
            # caller's transaction.  Even when ``rotate_epoch`` is false we
            # must still fence every retained authority (tokens, approvals,
            # and streams); otherwise a disabled/revoked Binding can leave
            # an old SSE subscriber or approved write alive.
            await self._fence_capabilities(
                binding_id,
                invalidate_tokens=(rotate_epoch or binding.status != "enabled"),
            )

            if binding.status not in _ACTIVE_DESIRED_STATES:
                # The runtime association lives in the supervisor's memory;
                # release it so a later setup can re-admit the same runtime.
                self._registry.release_supervisor_binding(binding.id, binding.runtime_ref)

            readiness = "not_ready" if binding.status in _ACTIVE_DESIRED_STATES else "disabled"
            reason = None if readiness == "disabled" else _RUNTIME_ASSIGNMENT_CONFLICT
            observed = await self._repositories.agent_runtime_bindings.mark_unavailable(
                binding_id,
                readiness,
                reason,
            )
            return self._result(binding_id, readiness, reason, observed)

    async def run_health_cycle(self) -> list[RuntimeReconcileResult]:
        """Probe live runtimes and retry transiently unavailable mappings.

        A B restart can finish while OpenCode is still reconnecting its MCP
        client.  Startup correctly leaves that binding unpublished; later
        health ticks retry only the two transport-level failures, while
        policy, disclosure, deployment, and assignment failures remain
        fail-closed until an explicit configuration change.
        """

        bindings = await self._repositories.agent_bindings.list_all()
        results: list[RuntimeReconcileResult] = []
        for listed in bindings:
            async with self._binding_lock(listed.id):
                binding = await self._repositories.agent_bindings.get_by_id(listed.id)
                observed = await self._repositories.agent_runtime_bindings.get_by_binding(listed.id)
                pipeline = self._registry.pipeline_for(listed.id)
                if pipeline is None:
                    if (
                        binding is not None
                        and binding.status in _ACTIVE_DESIRED_STATES
                        and observed is not None
                        and observed.readiness == "not_ready"
                        and observed.reason_code in _TRANSIENT_RUNTIME_FAILURES
                    ):
                        results.append(await self._reconcile_locked(listed.id))
                    continue
                if binding is None or observed is None:
                    async with self._endpoint_lock:
                        await self._retire_mapping_locked(listed.id)
                    await self._fence_capabilities(listed.id, invalidate_tokens=True)
                    results.append(
                        RuntimeReconcileResult(
                            binding_id=listed.id,
                            readiness="not_ready",
                            reason_code=_RUNTIME_ASSIGNMENT_CONFLICT,
                            applied_revision=None,
                        )
                    )
                    continue
                results.append(await self._check_health_locked(binding, observed))
        return results

    def _binding_lock(self, binding_id: UUID) -> asyncio.Lock:
        lock = self._binding_locks.get(binding_id)
        if lock is None:
            lock = asyncio.Lock()
            self._binding_locks[binding_id] = lock
        return lock

    async def _reconcile_locked(self, binding_id: UUID) -> RuntimeReconcileResult:
        binding = await self._repositories.agent_bindings.get_by_id(binding_id)
        if binding is None:
            async with self._endpoint_lock:
                await self._retire_mapping_locked(binding_id)
            return RuntimeReconcileResult(
                binding_id=binding_id,
                readiness="disabled",
                reason_code=_BINDING_STATE_CONFLICT,
                applied_revision=None,
            )

        if binding.status not in _ACTIVE_DESIRED_STATES:
            async with self._endpoint_lock:
                await self._retire_mapping_locked(binding_id)
            await self._fence_capabilities(binding_id, invalidate_tokens=True)
            observed = await self._repositories.agent_runtime_bindings.mark_unavailable(
                binding_id,
                "disabled",
                None,
            )
            return self._result(binding_id, "disabled", None, observed)

        try:
            fingerprint = await self._profile_fingerprint(binding)
        except (TermFlowError, ValueError):
            return await self._block_and_unmap(binding_id, _PIPELINE_START_FAILED)

        disclosure = await self._repositories.agent_provider_disclosures.get_current(
            binding_id,
            fingerprint,
        )
        policies = await self._repositories.pane_policies.get_for_binding(binding_id)
        observed = await self._repositories.agent_runtime_bindings.get_by_binding(binding_id)

        if (
            disclosure is not None
            and any(policy.allowed for policy in policies)
            and observed is not None
            and self._is_current_ready(binding, observed, fingerprint)
            and self._registry.pipeline_for(binding_id) is not None
        ):
            return await self._check_health_locked(binding, observed)

        reconciling = await self._repositories.agent_runtime_bindings.mark_reconciling(
            binding_id,
            binding.config_revision,
            fingerprint,
        )
        if reconciling is None:
            async with self._endpoint_lock:
                await self._retire_mapping_locked(binding_id)
            current = await self._repositories.agent_bindings.get_by_id(binding_id)
            if current is None or current.status not in _ACTIVE_DESIRED_STATES:
                return RuntimeReconcileResult(
                    binding_id=binding_id,
                    readiness="disabled",
                    reason_code=None,
                    applied_revision=None,
                )
            return RuntimeReconcileResult(
                binding_id=binding_id,
                readiness="not_ready",
                reason_code=_RUNTIME_ASSIGNMENT_CONFLICT,
                applied_revision=None,
            )

        async with self._endpoint_lock:
            if not await self._retire_mapping_locked(binding_id):
                await self._fence_capabilities(binding_id, invalidate_tokens=False)
                return await self._mark_unavailable(
                    binding_id,
                    "not_ready",
                    _PIPELINE_START_FAILED,
                )

            # Marking ``reconciling`` is the durable submit/tool-call fence.
            # Close in-flight approval waiters and streams only after the old
            # mapping has been removed; model/config changes intentionally do
            # not rotate the desired runtime epoch, so their existing token
            # rows remain present but cannot pass the observed-ready gate.
            await self._fence_capabilities(binding_id, invalidate_tokens=False)

            if disclosure is None:
                reason = (
                    _DISCLOSURE_STALE
                    if observed is not None
                    and observed.config_fingerprint not in (None, fingerprint)
                    else _DISCLOSURE_REQUIRED
                )
                return await self._mark_unavailable(binding_id, "blocked", reason)
            if not policies or not any(policy.allowed for policy in policies):
                return await self._mark_unavailable(
                    binding_id,
                    "blocked",
                    _PIPELINE_START_FAILED,
                )
            if not self._has_runtime_assignment(binding):
                return await self._mark_unavailable(
                    binding_id,
                    "not_ready",
                    _RUNTIME_ASSIGNMENT_CONFLICT,
                )
            if not await self._bootstrap_capability_available(binding):
                return await self._mark_unavailable(
                    binding_id,
                    "blocked",
                    _DEPLOYMENT_REQUIRED,
                )

            candidate: RuntimeCandidate
            try:
                candidate = await self._registry.build_candidate(binding)
                await self._registry.start_candidate(candidate)
            except RuntimeNotReadyError as exc:
                return await self._mark_unavailable(
                    binding_id,
                    "not_ready",
                    self._runtime_failure_reason(exc),
                )
            except Exception:
                return await self._mark_unavailable(
                    binding_id,
                    "not_ready",
                    _PIPELINE_START_FAILED,
                )

            # Starting the adapter/pipeline may take long enough for a Profile
            # or desired Binding mutation to commit.  Re-read both identities
            # before the database CAS: the CAS protects revision/epoch and the
            # disclosure row, while this explicit comparison also protects the
            # canonical Profile fingerprint from legacy writers that have not
            # yet been cut over to revision bumps.
            latest_binding = await self._repositories.agent_bindings.get_by_id(binding_id)
            if latest_binding is None or not self._same_desired_runtime(
                binding,
                latest_binding,
            ):
                await self._registry.discard_candidate(candidate)
                return await self._mark_unavailable(
                    binding_id,
                    "not_ready",
                    _RUNTIME_ASSIGNMENT_CONFLICT,
                )
            try:
                latest_fingerprint = await self._profile_fingerprint(latest_binding)
            except (TermFlowError, ValueError):
                await self._registry.discard_candidate(candidate)
                return await self._mark_unavailable(
                    binding_id,
                    "blocked",
                    _PIPELINE_START_FAILED,
                )
            if latest_fingerprint != fingerprint:
                await self._registry.discard_candidate(candidate)
                return await self._mark_unavailable(
                    binding_id,
                    "blocked",
                    _DISCLOSURE_STALE,
                )

            try:
                ready = await self._repositories.agent_runtime_bindings.compare_and_set_ready(
                    binding_id,
                    binding.config_revision,
                    fingerprint,
                    binding.runtime_epoch or 0,
                )
            except BaseException:
                await self._cleanup_candidate_failure(binding_id, candidate)
                await self._mark_unavailable_safely(
                    binding_id, "not_ready", _RUNTIME_ASSIGNMENT_CONFLICT
                )
                raise
            if not ready:
                await self._cleanup_candidate_failure(binding_id, candidate)
                return await self._mark_unavailable(
                    binding_id,
                    "not_ready",
                    _RUNTIME_ASSIGNMENT_CONFLICT,
                )

            try:
                await self._registry.publish_started(binding_id, candidate)
            except asyncio.CancelledError:
                # Cancellation can land after the CAS or after the registry
                # has promoted the candidate.  Shield cleanup so cancellation
                # never leaves ``ready`` persisted with a live/untracked map.
                await self._shield_cleanup_candidate_failure(binding_id, candidate)
                await self._mark_unavailable_safely(binding_id, "not_ready", _PIPELINE_START_FAILED)
                raise
            except Exception:
                await self._cleanup_candidate_failure(binding_id, candidate)
                return await self._mark_unavailable(
                    binding_id,
                    "not_ready",
                    _PIPELINE_START_FAILED,
                )

        return RuntimeReconcileResult(
            binding_id=binding_id,
            readiness="ready",
            reason_code=None,
            applied_revision=binding.config_revision,
        )

    async def _profile_fingerprint(self, binding: AgentBinding) -> str:
        profile = await self._repositories.agent_profiles.get_by_id(binding.profile_id)
        if profile is None:
            raise ValueError("profile missing")
        config, _ = canonicalize_profile_config(profile.config)
        return self._provider_catalog.disclosure_fingerprint(config)

    async def _check_health_locked(
        self,
        binding: AgentBinding,
        observed: AgentRuntimeBinding,
    ) -> RuntimeReconcileResult:
        try:
            fingerprint = await self._profile_fingerprint(binding)
        except (TermFlowError, ValueError):
            fingerprint = None
        disclosure = (
            await self._repositories.agent_provider_disclosures.get_current(binding.id, fingerprint)
            if fingerprint is not None
            else None
        )
        policies = await self._repositories.pane_policies.get_for_binding(binding.id)
        bootstrap_available = await self._bootstrap_capability_available(binding)

        async with self._endpoint_lock:
            reason: str | None = None
            readiness = "not_ready"
            if binding.status not in _ACTIVE_DESIRED_STATES:
                readiness, reason = "disabled", None
            elif fingerprint is None:
                readiness, reason = "blocked", _PIPELINE_START_FAILED
            elif disclosure is None:
                readiness = "blocked"
                reason = (
                    _DISCLOSURE_STALE
                    if observed.config_fingerprint not in (None, fingerprint)
                    else _DISCLOSURE_REQUIRED
                )
            elif not policies or not any(policy.allowed for policy in policies):
                readiness, reason = "blocked", _PIPELINE_START_FAILED
            elif not bootstrap_available:
                readiness, reason = "blocked", _DEPLOYMENT_REQUIRED
            elif (
                observed.readiness != "ready"
                or observed.observed_runtime_ref != binding.runtime_ref
                or observed.observed_runtime_epoch != binding.runtime_epoch
                or observed.observed_capability_ref != binding.capability_ref
                or observed.applied_revision != binding.config_revision
                or observed.config_fingerprint != fingerprint
            ):
                readiness, reason = "not_ready", _RUNTIME_ASSIGNMENT_CONFLICT
            if reason is not None or readiness == "disabled":
                await self._retire_mapping_locked(binding.id)
                # Health/config drift is an authority boundary.  Revoke token
                # rows as well as approvals/streams so a stale caller cannot
                # re-enter while the next reconcile is pending.
                await self._fence_capabilities(binding.id, invalidate_tokens=True)
                return await self._mark_unavailable(binding.id, readiness, reason)
            assert observed.observed_runtime_ref is not None
            assert observed.observed_runtime_epoch is not None
            try:
                health = await self._supervisor.health(RuntimeRef(observed.observed_runtime_ref))
            except Exception:
                health = None
            if (
                health is None
                or health.status is not RuntimeStatus.READY
                or health.epoch != observed.observed_runtime_epoch
            ):
                reason = self._health_failure_reason(health.detail if health is not None else None)
                # Security ordering: remove the routing entry before the
                # persistent state advertises the health failure.  The
                # supervisor readiness gate rejects every MCP call while the
                # runtime is unhealthy, so preserve the current-epoch token:
                # revoking it would make recovery require reissuing the same
                # deployment secret, which the unique token digest correctly
                # forbids.  Authority-changing disable/revoke/epoch paths
                # still invalidate their tokens.
                await self._retire_mapping_locked(binding.id)
                await self._fence_capabilities(binding.id, invalidate_tokens=False)
                return await self._mark_unavailable(
                    binding.id,
                    "not_ready",
                    reason,
                )
            recorded = await self._repositories.agent_runtime_bindings.record_health(observed.id)
            return self._result(binding.id, "ready", None, recorded or observed)

    async def _block_and_unmap(
        self,
        binding_id: UUID,
        reason: str,
    ) -> RuntimeReconcileResult:
        async with self._endpoint_lock:
            await self._retire_mapping_locked(binding_id)
        await self._fence_capabilities(binding_id, invalidate_tokens=False)
        return await self._mark_unavailable(binding_id, "blocked", reason)

    async def _fence_capabilities(
        self,
        binding_id: UUID,
        *,
        invalidate_tokens: bool,
    ) -> None:
        """Best-effort authority fencing after routing has been removed.

        Unpublish is the hard security boundary and is always performed by the
        caller first.  Secondary stores are swept independently so one failed
        audit/stream hook cannot leave another authority live.
        """
        operations: list[tuple[str, Callable[[], Awaitable[object]]]] = []
        if invalidate_tokens:
            operations.append(
                (
                    "agent tokens",
                    lambda: self._repositories.agent_tokens.expire_all_for_binding(binding_id),
                )
            )
        approval_fencer = self._approval_fencer
        if approval_fencer is not None:
            operations.append(("approvals", lambda: approval_fencer(binding_id)))
        stream_fencer = self._stream_fencer
        if stream_fencer is not None:
            operations.append(("streams", lambda: stream_fencer(binding_id)))
        for label, operation in operations:
            try:
                await operation()
            except Exception:
                # The desired status/epoch and the removed map already deny
                # new work.  Keep sweeping the remaining authority surfaces;
                # the next health/reconcile cycle will retry this best effort.
                logger.exception("Agent runtime %s fence failed for %s", label, binding_id)

    async def _bootstrap_capability_available(self, binding: AgentBinding) -> bool:
        checker = self._bootstrap_capability_checker
        if checker is None:
            return True
        try:
            return bool(await checker(binding))
        except Exception:
            logger.exception("Agent bootstrap capability check failed for %s", binding.id)
            return False

    async def _cleanup_candidate_failure(
        self, binding_id: UUID, candidate: RuntimeCandidate
    ) -> None:
        """Remove any promoted candidate and close all candidate-owned state."""
        try:
            # The controller retires the old mapping before building a
            # candidate, so a mapping returned here can only be the candidate
            # that failed publication (or a concurrent duplicate of it).
            await self._registry.unpublish(binding_id)
        except Exception:
            logger.exception("Agent candidate unpublish failed for %s", binding_id)
        try:
            await self._registry.discard_candidate(candidate)
        except Exception:
            logger.exception("Agent candidate cleanup failed for %s", binding_id)

    async def _shield_cleanup_candidate_failure(
        self, binding_id: UUID, candidate: RuntimeCandidate
    ) -> None:
        task = asyncio.create_task(
            self._cleanup_candidate_failure(binding_id, candidate),
            name=f"agent-runtime-cleanup-{binding_id}",
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The cleanup task remains scheduled even when the caller is
            # cancelled; consume a second cancellation-free checkpoint when
            # possible so tests and shutdown observe the unmapped state.
            with contextlib.suppress(Exception):
                await task

    async def _mark_unavailable_safely(
        self, binding_id: UUID, readiness: str, reason: str | None
    ) -> None:
        try:
            await self._mark_unavailable(binding_id, readiness, reason)
        except Exception:
            logger.exception("Agent runtime state update failed for %s", binding_id)

    async def _retire_mapping_locked(self, binding_id: UUID) -> bool:
        """Unmap first, then best-effort stop; return whether stop succeeded."""

        pipeline = await self._registry.unpublish(binding_id)
        if pipeline is None:
            return True
        try:
            await pipeline.stop()
        except Exception:
            # The security boundary is the already-completed unpublish.  A
            # shutdown detail may contain backend data, so callers receive
            # only their bounded reason code and still persist fenced state.
            return False
        return True

    async def _mark_unavailable(
        self,
        binding_id: UUID,
        readiness: str,
        reason: str | None,
    ) -> RuntimeReconcileResult:
        observed = await self._repositories.agent_runtime_bindings.mark_unavailable(
            binding_id,
            readiness,
            reason,
        )
        return self._result(binding_id, readiness, reason, observed)

    @staticmethod
    def _result(
        binding_id: UUID,
        readiness: str,
        reason: str | None,
        observed: AgentRuntimeBinding | None,
    ) -> RuntimeReconcileResult:
        return RuntimeReconcileResult(
            binding_id=binding_id,
            readiness=readiness,
            reason_code=reason,
            applied_revision=(observed.applied_revision if observed is not None else None),
        )

    @staticmethod
    def _has_runtime_assignment(binding: AgentBinding) -> bool:
        return bool(
            binding.runtime_ref
            and binding.runtime_ref.strip()
            and binding.runtime_epoch is not None
            and binding.runtime_epoch >= 1
            and binding.capability_ref
            and binding.capability_ref.strip()
        )

    @staticmethod
    def _is_current_ready(
        binding: AgentBinding,
        observed: AgentRuntimeBinding,
        fingerprint: str,
    ) -> bool:
        return (
            observed.readiness == "ready"
            and observed.applied_revision == binding.config_revision
            and observed.config_fingerprint == fingerprint
            and observed.observed_runtime_ref == binding.runtime_ref
            and observed.observed_runtime_epoch == binding.runtime_epoch
            and observed.observed_capability_ref == binding.capability_ref
        )

    @staticmethod
    def _same_desired_runtime(first: AgentBinding, second: AgentBinding) -> bool:
        return (
            second.status in _ACTIVE_DESIRED_STATES
            and second.config_revision == first.config_revision
            and second.runtime_ref == first.runtime_ref
            and second.runtime_epoch == first.runtime_epoch
            and second.capability_ref == first.capability_ref
        )

    @staticmethod
    def _runtime_failure_reason(exc: RuntimeNotReadyError) -> str:
        detail = str(exc).lower()
        if "mcp_not_connected" in detail:
            return _MCP_NOT_CONNECTED
        if (
            "same runtime endpoint" in detail
            or "already bound" in detail
            or "epoch" in detail
            or "capability_ref" in detail
            or "runtime_ref" in detail
        ):
            return _RUNTIME_ASSIGNMENT_CONFLICT
        return _RUNTIME_UNREACHABLE

    @staticmethod
    def _health_failure_reason(detail: str | None) -> str:
        if detail == _MCP_NOT_CONNECTED:
            return _MCP_NOT_CONNECTED
        return _RUNTIME_UNREACHABLE
