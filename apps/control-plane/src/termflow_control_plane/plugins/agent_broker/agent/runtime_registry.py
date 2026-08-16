"""Binding-scoped runtime mapping for the Agent Broker (M4.5 spec §1/§6).

``AgentRuntimeRegistry`` resolves each :class:`AgentBinding` to a dedicated
adapter instance (``OpenCodeAdapter`` with base_url/directory/runtime_id/
binding_capability_epoch pinned from the binding), proves activation through
the supervisor's fail-closed ``accept_activation`` gate, and owns the
per-binding ``AgentPipelineService`` lifecycle (``start_all`` / ``stop_all`` /
``pipeline_for``).

Every binding gets an independent adapter and pipeline instance; the
``directory``/``runtime_id`` are fixed per binding, which satisfies the
two-binding isolation of plan gate 6.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.config import Settings
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
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    RuntimeNotReadyError,
    SupervisorConnector,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import AgentStreamHub
from termflow_control_plane.plugins.agent_broker.agent.turns import BackendEventScope
from termflow_control_plane.plugins.protocol import RuntimeRef

# M4 pin: the OpenCode backend version B negotiates with the pinned contract
# (tests/fixtures/opencode/opencode-pin.md); test_opencode_adapter.py pins the
# same value as its BACKEND_VERSION constant.
PINNED_OPENCODE_BACKEND_VERSION = "0.1.0"

RuntimeEndpointProvider = Callable[[RuntimeRef], tuple[str, str]]
"""Resolve a runtime ref to ``(base_url, directory)``.

The settings-based default serves the single-binding reference deployment; a
multi-binding fleet injects its own endpoint table (M4.5 spec §6).
"""

AdapterFactory = Callable[..., AgentBackend]
"""Construct a runtime adapter for one binding.

Tests inject a fake recording its constructor kwargs and ``close()`` calls;
production uses the default :func:`_build_opencode_adapter`.
"""


def _settings_endpoint_provider(settings: Settings) -> RuntimeEndpointProvider:
    """Default provider: every runtime maps to the reference deployment endpoint."""

    def resolve(runtime_ref: RuntimeRef) -> tuple[str, str]:
        return (settings.agent_opencode_base_url, settings.agent_opencode_directory)

    return resolve


def _build_opencode_adapter(
    *,
    base_url: str,
    directory: str,
    backend_version: str,
    runtime_id: str | None = None,
    binding_capability_epoch: int = 0,
    username: str | None = None,
    password: str | None = None,
) -> AgentBackend:
    return OpenCodeAdapter(
        base_url=base_url,
        directory=directory,
        backend_version=backend_version,
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


class AgentRuntimeRegistry:
    """binding → (pipeline, adapter, scope, supervisor gate) mapping with lifecycle.

    Every binding gets an independent adapter and pipeline instance (M4.5
    spec §6).  A binding whose runtime is missing or rejected by the
    supervisor never enters the mapping and stays disabled (fail closed); the
    API layer treats an unmapped binding as ``binding_runtime_unavailable``.
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
    ) -> None:
        # None in unit tests: the activation gate stays open, but a binding
        # still needs a runtime_ref/epoch to build an adapter (spec §6).
        self._supervisor = supervisor
        self._repositories = repositories
        self._sessions = sessions
        self._hub = hub
        self._settings = settings
        self._reconcile_attempts = settings.agent_pipeline_reconcile_attempts
        self._endpoint_provider = endpoint_provider or _settings_endpoint_provider(settings)
        self._adapter_factory = adapter_factory or _build_opencode_adapter
        self._bindings: dict[UUID, _BoundRuntime] = {}
        self.unavailable_bindings: dict[UUID, str] = {}

    async def build_pipeline(self, binding: AgentBinding) -> AgentPipelineService:
        """Resolve and activate one binding's runtime, fail closed on any doubt.

        Resolution order follows M4.5 spec §6: binding runtime fields →
        endpoint → adapter → capabilities → scope → supervisor
        ``accept_activation`` gate → pipeline.  Any missing runtime field or a
        rejected activation raises :class:`RuntimeNotReadyError` and the
        binding stays unmapped (the constructed adapter is closed so its owned
        HTTP client is never leaked).
        """
        runtime_ref, epoch = self._resolve_binding_runtime(binding)
        base_url, directory = self._endpoint_provider(runtime_ref)
        adapter_kwargs: dict[str, Any] = {
            "base_url": base_url,
            "directory": directory,
            "backend_version": PINNED_OPENCODE_BACKEND_VERSION,
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
            reconcile_attempts=self._reconcile_attempts,
        )
        self._bindings[binding.id] = _BoundRuntime(
            pipeline=pipeline,
            adapter=adapter,
            scope=scope,
            capabilities=capabilities,
            runtime_ref=runtime_ref,
            runtime_epoch=epoch,
            capability_ref=binding.capability_ref,
        )
        return pipeline

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
                pipeline = await self.build_pipeline(binding)
            except RuntimeNotReadyError as exc:
                self.unavailable_bindings[binding.id] = str(exc)
                continue
            await pipeline.start()

    async def stop_all(self) -> None:
        """Stop every pipeline (cancelling its tasks and closing its adapter)."""
        runtimes = list(self._bindings.values())
        # Clear the mapping before stopping so a failing stop can never leave
        # a half-closed binding resolvable again.
        self._bindings.clear()
        for runtime in runtimes:
            await runtime.pipeline.stop()

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
