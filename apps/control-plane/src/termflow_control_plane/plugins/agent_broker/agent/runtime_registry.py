"""Binding-scoped runtime mapping for the Agent Broker (M4.5 spec §1/§6).

``AgentRuntimeRegistry`` resolves each :class:`AgentBinding` to a dedicated
adapter instance (``OpenCodeAdapter`` with base_url/directory/runtime_id/
binding_capability_epoch pinned from the binding), attests the runtime
through the supervisor's fail-closed ``register``/``accept_activation``
gates, and owns the per-binding ``AgentPipelineService`` lifecycle
(``start_all`` / ``stop_all`` / ``pipeline_for``).

Every binding gets an independent adapter and pipeline instance.  Two
bindings must never resolve to the same ``(base_url, directory)`` endpoint
pair: the settings-based default provider is the single-binding reference
deployment, and the registry refuses a second binding that would share an
endpoint with an already-mapped binding (plan gate 6 two-binding isolation) -
a shared directory would let the bindings observe each other's events.
Multi-binding fleets inject their own endpoint provider with per-binding
directories.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.agent_contracts import AgentProfileConfig
from termflow_control_plane.config import Settings
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import AgentBinding
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackend,
    AgentBackendCapabilities,
)
from termflow_control_plane.plugins.agent_broker.agent.opencode import OpenCodeAdapter
from termflow_control_plane.plugins.agent_broker.agent.pipeline import (
    AgentPipelineService,
)
from termflow_control_plane.plugins.agent_broker.agent.provider_catalog import (
    ProviderCatalog,
    canonicalize_profile_config,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    RuntimeNotReadyError,
    RuntimeSupervisorError,
    SupervisorConnector,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import AgentStreamHub
from termflow_control_plane.plugins.agent_broker.agent.turns import BackendEventScope
from termflow_control_plane.plugins.protocol import CapabilityRef, RuntimeRef

# M4 pin: the OpenCode backend version B negotiates with the pinned contract
# (tests/fixtures/opencode/opencode-pin.md); test_opencode_adapter.py pins the
# same value as its BACKEND_VERSION constant.
PINNED_OPENCODE_BACKEND_VERSION = "0.1.0"

RuntimeEndpointProvider = Callable[[RuntimeRef], tuple[str, str]]
"""Resolve a runtime ref to ``(base_url, directory)``.

The settings-based default serves the single-binding reference deployment;
a multi-binding fleet injects its own endpoint table (M4.5 spec §6).
Whichever provider is used, the registry refuses a second binding that
resolves to the same ``(base_url, directory)`` as an already-mapped binding
(plan gate 6): bindings sharing a directory could observe each other's
events on the runtime's global stream.
"""

AdapterFactory = Callable[..., AgentBackend]
"""Construct a runtime adapter for one binding.

Tests inject a fake recording its constructor kwargs and ``close()`` calls;
production uses the default :func:`_build_opencode_adapter`.
"""

ProfileConfigProvider = Callable[[UUID], Awaitable[object | None]]
"""Load one Profile's persisted config by its server-owned identifier."""

_PROFILE_MISSING = "agent profile is missing; activation fails closed"
_PROFILE_LOOKUP_FAILED = "agent profile lookup failed; activation fails closed"
_PROFILE_CONFIG_INVALID = (
    "agent profile configuration is invalid; activation fails closed"
)
_PROFILE_PROVIDER_UNAVAILABLE = (
    "agent profile provider or model is unavailable; activation fails closed"
)


class RuntimeShutdownError(RuntimeError):
    """One or more runtimes failed shutdown after all mappings were removed."""

    def __init__(self, failure_count: int) -> None:
        self.failure_count = failure_count
        super().__init__(f"Agent runtime shutdown failed for {failure_count} binding(s)")


def _settings_endpoint_provider(settings: Settings) -> RuntimeEndpointProvider:
    """Default provider: the single-binding reference deployment endpoint.

    Every runtime resolves to the same ``agent_opencode_base_url`` /
    ``agent_opencode_directory`` pair.  That is correct only while exactly
    ONE binding is active: the registry refuses a second binding resolving
    to the same pair (plan gate 6) instead of letting two bindings observe
    each other's events through a shared directory.  Multi-binding fleets
    must inject their own endpoint table with per-binding directories.
    """

    def resolve(runtime_ref: RuntimeRef) -> tuple[str, str]:
        return (settings.agent_opencode_base_url, settings.agent_opencode_directory)

    return resolve


def _build_opencode_adapter(
    *,
    base_url: str,
    directory: str,
    backend_version: str,
    provider_id: str,
    model_id: str,
    runtime_id: str | None = None,
    binding_capability_epoch: int = 0,
    username: str | None = None,
    password: str | None = None,
) -> AgentBackend:
    return OpenCodeAdapter(
        base_url=base_url,
        directory=directory,
        backend_version=backend_version,
        provider_id=provider_id,
        model_id=model_id,
        runtime_id=runtime_id,
        binding_capability_epoch=binding_capability_epoch,
        username=username,
        password=password,
    )


@dataclass(slots=True)
class _BoundRuntime:
    """Registry-side record for one activated binding."""

    pipeline: AgentPipelineService
    adapter: AgentBackend
    scope: BackendEventScope
    capabilities: AgentBackendCapabilities
    runtime_ref: RuntimeRef
    runtime_epoch: int
    capability_ref: str | None
    base_url: str
    directory: str


@dataclass(slots=True)
class RuntimeCandidate:
    """Fully constructed, but not yet published, binding runtime."""

    pipeline: AgentPipelineService
    adapter: AgentBackend
    scope: BackendEventScope
    capabilities: AgentBackendCapabilities
    runtime_ref: RuntimeRef
    runtime_epoch: int
    capability_ref: str
    base_url: str
    directory: str


class AgentRuntimeRegistry:
    """binding → (pipeline, adapter, scope, supervisor gate) mapping with lifecycle.

    Every binding gets an independent adapter and pipeline instance (M4.5
    spec §6).  A binding whose runtime is missing, shares an endpoint with
    another binding (plan gate 6), or is rejected by the supervisor's
    ``register`` attestation never enters the mapping and stays disabled
    (fail closed); the API layer treats an unmapped binding as
    ``binding_runtime_unavailable``.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        repositories: RepositoryBundle,
        sessions: async_sessionmaker[AsyncSession],
        hub: AgentStreamHub,
        supervisor: SupervisorConnector | None,
        endpoint_provider: RuntimeEndpointProvider | None = None,
        adapter_factory: AdapterFactory | None = None,
        profile_config_provider: ProfileConfigProvider | None = None,
        provider_catalog: ProviderCatalog | None = None,
    ) -> None:
        # None only in focused unit tests: the activation gate stays open,
        # but a binding still needs runtime fields (and now also a
        # capability_ref) to build an adapter (spec §6).  The production
        # composition root always injects a real supervisor, so the
        # register/accept_activation gates are active in production.
        self._supervisor = supervisor
        self._repositories = repositories
        self._sessions = sessions
        self._hub = hub
        self._settings = settings
        self._reconcile_attempts = settings.agent_pipeline_reconcile_attempts
        self._endpoint_provider = endpoint_provider or _settings_endpoint_provider(settings)
        self._adapter_factory = adapter_factory or _build_opencode_adapter
        self._profile_config_provider = (
            profile_config_provider
            or self._repository_profile_config_provider(repositories)
        )
        self._provider_catalog = provider_catalog or ProviderCatalog.from_settings(
            settings
        )
        self._bindings: dict[UUID, _BoundRuntime] = {}
        # Candidate and live endpoint checks share one lock.  This prevents a
        # concurrently-built candidate from racing a publication and exposing
        # two bindings on one runtime endpoint.
        self._endpoint_lock = asyncio.Lock()
        self._candidates: dict[UUID, RuntimeCandidate] = {}
        self.unavailable_bindings: dict[UUID, str] = {}

    async def build_candidate(self, binding: AgentBinding) -> RuntimeCandidate:
        """Resolve and activate one binding's runtime, fail closed on any doubt.

        Resolution order follows M4.5 spec §6: binding runtime fields →
        endpoint → shared-endpoint isolation guard (plan gate 6) → supervisor
        ``register`` attestation → adapter → capabilities → scope →
        supervisor ``accept_activation`` gate → pipeline.  Any missing runtime
        field, a shared endpoint, or a rejected attestation raises
        :class:`RuntimeNotReadyError` and the binding stays unmapped (a
        constructed adapter is closed so its owned HTTP client is never
        leaked).
        """
        profile_config = await self._resolve_profile_config(binding)
        runtime_ref, epoch = self._resolve_binding_runtime(binding)
        base_url, directory = self._endpoint_provider(runtime_ref)
        # Plan gate 6 (two-binding isolation): a second binding must never
        # resolve to the same (base_url, directory) as an already-mapped
        # binding - a shared directory would let the bindings observe each
        # other's events on the runtime's global stream.  Fail closed with a
        # reason ``start_all`` records in ``unavailable_bindings``.
        capability_ref = (binding.capability_ref or "").strip()
        if not capability_ref:
            raise RuntimeNotReadyError(
                f"binding {binding.id} has no capability_ref; activation fails closed"
            )
        if self._supervisor is not None:
            # Attest the runtime before any adapter is allocated: a
            # not-ready/epoch-mismatched/conflicting runtime fails closed
            # without constructing or leaking an HTTP client.
            try:
                await self._supervisor.register(
                    str(binding.id),
                    runtime_ref,
                    epoch,
                    CapabilityRef(capability_ref),
                )
            except (RuntimeSupervisorError, ValueError) as exc:
                raise RuntimeNotReadyError(
                    f"supervisor attestation failed for binding {binding.id}: {exc}"
                ) from exc
        adapter_kwargs: dict[str, Any] = {
            "base_url": base_url,
            "directory": directory,
            "backend_version": PINNED_OPENCODE_BACKEND_VERSION,
            "provider_id": profile_config.provider_id,
            "model_id": profile_config.model_id,
            "runtime_id": str(runtime_ref),
            "binding_capability_epoch": epoch,
        }
        # Only forward the basic-auth pair when configured: injected adapter
        # factories (tests) keep their exact constructor signatures.
        if self._settings.agent_opencode_username is not None:
            password = self._settings.agent_opencode_password
            assert password is not None
            adapter_kwargs["username"] = self._settings.agent_opencode_username
            adapter_kwargs["password"] = password.get_secret_value()
        adapter = self._adapter_factory(**adapter_kwargs)
        try:
            capabilities = await adapter.capabilities()
            scope = BackendEventScope(binding_id=str(binding.id), runtime_epoch=epoch)
        except BaseException:
            # Never leak an owned HTTP client on a failed activation.
            await adapter.close()
            raise
        if self._supervisor is not None and not self._supervisor.accept_activation(
            runtime_ref, epoch
        ):
            await adapter.close()
            raise RuntimeNotReadyError(
                f"supervisor rejected activation for binding {binding.id}: "
                f"runtime {runtime_ref} epoch {epoch} is not ready"
            )
        pipeline = AgentPipelineService(
            binding_id=binding.id,
            adapter=adapter,
            scope=scope,
            capabilities=capabilities,
            repositories=self._repositories,
            sessions=self._sessions,
            hub=self._hub,
            supervisor=self._supervisor,
            runtime_ref=str(runtime_ref),
            runtime_epoch=epoch,
            applied_config_revision=binding.config_revision,
            reconcile_attempts=self._reconcile_attempts,
        )
        candidate = RuntimeCandidate(
            pipeline=pipeline,
            adapter=adapter,
            scope=scope,
            capabilities=capabilities,
            runtime_ref=runtime_ref,
            runtime_epoch=epoch,
            capability_ref=capability_ref,
            base_url=base_url,
            directory=directory,
        )
        # Reserve the endpoint only after all activation checks and adapter
        # construction succeed.  Re-check under the lock to close the race
        # between concurrent candidate builds.
        try:
            async with self._endpoint_lock:
                self._ensure_endpoint_available_locked(binding.id, base_url, directory)
                previous_candidate = self._candidates.get(binding.id)
                self._candidates[binding.id] = candidate
        except BaseException:
            await self._close_candidate(candidate)
            raise
        if previous_candidate is not None:
            await self._close_candidate(previous_candidate)
        return candidate

    async def start_candidate(self, candidate: RuntimeCandidate) -> None:
        """Start a candidate without making it visible to callers."""
        try:
            await candidate.pipeline.start()
        except BaseException:
            await self._discard_candidate(candidate)
            raise

    async def publish_started(
        self, binding_id: UUID, candidate: RuntimeCandidate
    ) -> AgentPipelineService:
        """Publish a started candidate, then retire the previous mapping.

        The map swap occurs while holding the endpoint lock; stopping the old
        pipeline happens afterwards so readers always see either the old or
        new mapping, never a gap.
        """
        if not candidate.pipeline.started:
            await self._discard_candidate(candidate)
            raise RuntimeError("runtime candidate must be started before publication")
        try:
            async with self._endpoint_lock:
                if self._candidates.get(binding_id) is not candidate:
                    raise RuntimeError("runtime candidate is not registered")
                self._ensure_endpoint_available_locked(
                    binding_id, candidate.base_url, candidate.directory
                )
                old = self._bindings.get(binding_id)
                self._bindings[binding_id] = _BoundRuntime(
                    pipeline=candidate.pipeline,
                    adapter=candidate.adapter,
                    scope=candidate.scope,
                    capabilities=candidate.capabilities,
                    runtime_ref=candidate.runtime_ref,
                    runtime_epoch=candidate.runtime_epoch,
                    capability_ref=candidate.capability_ref,
                    base_url=candidate.base_url,
                    directory=candidate.directory,
                )
                self._candidates.pop(binding_id, None)
        except BaseException:
            # A stale candidate or a late endpoint conflict must never retain
            # an owned adapter.  The live mapping is untouched until the swap
            # above completes, so failure preserves the old runtime.
            await self._discard_candidate(candidate)
            raise
        if old is not None and old.pipeline is not candidate.pipeline:
            try:
                await old.pipeline.stop()
            except BaseException:
                # Publication is a security-sensitive transaction.  If the
                # old pipeline cannot be retired, do not leave the new one
                # live while the caller records a failed reconcile: remove
                # the promoted entry and close the candidate before
                # propagating the shutdown error.  The old pipeline is not
                # remapped because its stop outcome is unknown.
                async with self._endpoint_lock:
                    current = self._bindings.get(binding_id)
                    if current is not None and current.pipeline is candidate.pipeline:
                        self._bindings.pop(binding_id, None)
                try:
                    await candidate.pipeline.stop()
                except Exception:
                    # Preserve the original old-pipeline failure; the
                    # controller's next reconcile/cleanup sweep can retry.
                    pass
                raise
        return candidate.pipeline

    async def unpublish(self, binding_id: UUID) -> AgentPipelineService | None:
        """Remove a live mapping without stopping its pipeline."""
        async with self._endpoint_lock:
            runtime = self._bindings.pop(binding_id, None)
            self.unavailable_bindings.pop(binding_id, None)
        return runtime.pipeline if runtime is not None else None

    async def _discard_candidate(self, candidate: RuntimeCandidate) -> None:
        removed = False
        async with self._endpoint_lock:
            binding_id = next(
                (key for key, value in self._candidates.items() if value is candidate),
                None,
            )
            if binding_id is not None:
                self._candidates.pop(binding_id, None)
                removed = True
            # A concurrent duplicate publish may have already promoted this
            # candidate to the live mapping.  In that case the candidate is
            # still registry-owned and must not be stopped by the stale caller.
            live_owned = any(
                runtime.pipeline is candidate.pipeline
                for runtime in self._bindings.values()
            )
        if removed or not live_owned:
            await self._close_candidate(candidate)

    async def discard_candidate(self, candidate: RuntimeCandidate) -> None:
        """Discard a controller-rejected candidate without touching live state."""

        await self._discard_candidate(candidate)

    @staticmethod
    async def _close_candidate(
        candidate: RuntimeCandidate, *, suppress_errors: bool = True
    ) -> None:
        try:
            await candidate.pipeline.stop()
        except Exception:
            # Preserve the original activation/start failure.  Pipeline.stop
            # is idempotent and will have attempted to close its adapter.
            if not suppress_errors:
                raise

    def _ensure_endpoint_available_locked(
        self, binding_id: UUID, base_url: str, directory: str
    ) -> None:
        for other_id, existing in self._bindings.items():
            if (
                other_id != binding_id
                and existing.base_url == base_url
                and existing.directory == directory
            ):
                raise RuntimeNotReadyError(
                    f"binding {binding_id} resolves to the same runtime endpoint "
                    f"({base_url}, {directory}) as binding {other_id}; bindings "
                    "sharing a runtime endpoint could observe each other's "
                    "events (plan gate 6), so activation fails closed"
                )
        for other_id, existing_candidate in self._candidates.items():
            if (
                other_id != binding_id
                and existing_candidate.base_url == base_url
                and existing_candidate.directory == directory
            ):
                raise RuntimeNotReadyError(
                    f"binding {binding_id} resolves to the same runtime endpoint "
                    f"({base_url}, {directory}) as binding {other_id}; bindings "
                    "sharing a runtime endpoint could observe each other's "
                    "events (plan gate 6), so activation fails closed"
                )

    async def build_pipeline(self, binding: AgentBinding) -> AgentPipelineService:
        """Compatibility API: construct and map an unstarted pipeline."""
        candidate = await self.build_candidate(binding)
        # Historical callers use build_pipeline as a construction primitive
        # and expect ``pipeline.started`` to remain false.  Keep that API while
        # the atomic start/publication path is used by start_all/controller.
        async with self._endpoint_lock:
            old = self._bindings.get(binding.id)
            self._bindings[binding.id] = _BoundRuntime(
                pipeline=candidate.pipeline,
                adapter=candidate.adapter,
                scope=candidate.scope,
                capabilities=candidate.capabilities,
                runtime_ref=candidate.runtime_ref,
                runtime_epoch=candidate.runtime_epoch,
                capability_ref=candidate.capability_ref,
                base_url=candidate.base_url,
                directory=candidate.directory,
            )
            self._candidates.pop(binding.id, None)
        if old is not None and old.pipeline is not candidate.pipeline:
            await old.pipeline.stop()
        return candidate.pipeline

    @staticmethod
    def _repository_profile_config_provider(
        repositories: RepositoryBundle,
    ) -> ProfileConfigProvider:
        async def load(profile_id: UUID) -> object | None:
            profile = await repositories.agent_profiles.get_by_id(profile_id)
            return profile.config if profile is not None else None

        return load

    async def _resolve_profile_config(
        self,
        binding: AgentBinding,
    ) -> AgentProfileConfig:
        try:
            raw_config = await self._profile_config_provider(binding.profile_id)
        except SQLAlchemyError:
            raise RuntimeNotReadyError(_PROFILE_LOOKUP_FAILED) from None
        if raw_config is None:
            raise RuntimeNotReadyError(_PROFILE_MISSING)
        try:
            profile_config, _ = canonicalize_profile_config(raw_config)
        except TermFlowError:
            raise RuntimeNotReadyError(_PROFILE_CONFIG_INVALID) from None
        try:
            self._provider_catalog.resolve(profile_config)
        except TermFlowError:
            raise RuntimeNotReadyError(_PROFILE_PROVIDER_UNAVAILABLE) from None
        return profile_config

    async def start_all(self, bindings: Iterable[AgentBinding]) -> None:
        """Activate and start every binding that can prove runtime readiness.

        Bindings are activated independently (per-binding fail-closed
        isolation): a binding whose runtime is missing or rejected is left
        disabled/unmapped and recorded in ``unavailable_bindings`` so the
        remaining bindings keep running (M4.5 spec §6.2.1).  Any other
        failure propagates.
        """
        for binding in bindings:
            try:
                candidate = await self.build_candidate(binding)
            except RuntimeNotReadyError as exc:
                self.unavailable_bindings[binding.id] = str(exc)
                continue
            try:
                await self.start_candidate(candidate)
                await self.publish_started(binding.id, candidate)
            except RuntimeNotReadyError as exc:
                self.unavailable_bindings[binding.id] = str(exc)
                continue

    async def stop_all(self) -> None:
        """Stop every pipeline (cancelling its tasks and closing its adapter)."""
        # Include candidates that have been built/started but not published;
        # otherwise a controller shutdown could leak their adapters.
        binding_ids = tuple(dict.fromkeys((*self._bindings, *self._candidates)))
        await self.stop_bindings(binding_ids)

    async def stop_binding(self, binding_id: UUID) -> bool:
        """Stop and unmap one binding after revocation or profile fencing."""
        return await self.stop_bindings((binding_id,)) == 1

    async def stop_bindings(self, binding_ids: Iterable[UUID]) -> int:
        """Unmap all targets first, then attempt every ordinary shutdown."""

        target_ids = tuple(dict.fromkeys(binding_ids))
        runtimes: list[_BoundRuntime] = []
        candidates: list[RuntimeCandidate] = []
        async with self._endpoint_lock:
            for binding_id in target_ids:
                runtime = self._bindings.pop(binding_id, None)
                candidate = self._candidates.pop(binding_id, None)
                self.unavailable_bindings.pop(binding_id, None)
                if runtime is not None:
                    runtimes.append(runtime)
                if candidate is not None:
                    candidates.append(candidate)

        failure_count = 0
        for candidate in candidates:
            try:
                await self._close_candidate(candidate, suppress_errors=False)
            except Exception:
                failure_count += 1
        for runtime in runtimes:
            try:
                await runtime.pipeline.stop()
            except Exception:
                failure_count += 1
        if failure_count:
            raise RuntimeShutdownError(failure_count)
        return len(runtimes)

    def pipeline_for(self, binding_id: UUID) -> AgentPipelineService | None:
        """Return the binding's pipeline, or ``None`` when it is unmapped."""
        runtime = self._bindings.get(binding_id)
        return runtime.pipeline if runtime is not None else None

    def _resolve_binding_runtime(self, binding: AgentBinding) -> tuple[RuntimeRef, int]:
        """Extract the runtime identity a binding is provisioned for (fail closed)."""
        if not binding.runtime_ref or not binding.runtime_ref.strip():
            raise RuntimeNotReadyError(
                f"binding {binding.id} has no runtime_ref; activation fails closed"
            )
        if binding.runtime_epoch is None:
            raise RuntimeNotReadyError(
                f"binding {binding.id} has no runtime_epoch; activation fails closed"
            )
        return RuntimeRef(binding.runtime_ref.strip()), binding.runtime_epoch
