"""Binding-scoped runtime mapping for the Agent Broker (M4.5 spec §1/§6).

``AgentRuntimeRegistry`` resolves each :class:`AgentBinding` to a dedicated
adapter instance (``OpenCodeAdapter`` with base_url/directory/runtime_id/
binding_capability_epoch pinned from the binding), proves activation through
the supervisor's fail-closed ``accept_activation`` gate, and owns the adapter
lifecycle (``start_all`` / ``stop_all``).

The per-binding ``AgentPipelineService`` orchestration is the *next* M4.5
task.  Until it lands, :meth:`build_pipeline` constructs and registers the
bare adapter and returns it; the pipeline assembly point is marked with a
``TODO`` and :meth:`pipeline_for` resolves to that adapter (the next task
swaps the returned type for ``AgentPipelineService`` without touching this
mapping/gate/lifecycle behaviour).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from uuid import UUID

from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.models import AgentBinding
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackend,
    AgentBackendCapabilities,
)
from termflow_control_plane.plugins.agent_broker.agent.opencode import OpenCodeAdapter
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    RuntimeNotReadyError,
    SupervisorConnector,
)
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
) -> AgentBackend:
    return OpenCodeAdapter(
        base_url=base_url,
        directory=directory,
        backend_version=backend_version,
        runtime_id=runtime_id,
        binding_capability_epoch=binding_capability_epoch,
    )


@dataclass(slots=True)
class _BoundRuntime:
    """Registry-side record for one activated binding."""

    adapter: AgentBackend
    scope: BackendEventScope
    capabilities: AgentBackendCapabilities
    runtime_ref: RuntimeRef
    runtime_epoch: int
    capability_ref: str | None


class AgentRuntimeRegistry:
    """binding → (adapter, scope, supervisor gate) mapping with adapter lifecycle.

    Every binding gets an independent adapter instance (M4.5 spec §6: the
    ``directory``/``runtime_id`` are fixed per binding, which satisfies the
    two-binding isolation of plan gate 6).  A binding whose runtime is missing
    or rejected by the supervisor never enters the mapping and stays disabled
    (fail closed); the API layer treats an unmapped binding as
    ``binding_runtime_unavailable``.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        supervisor: SupervisorConnector | None,
        endpoint_provider: RuntimeEndpointProvider | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        # None in unit tests: the activation gate stays open, but a binding
        # still needs a runtime_ref/epoch to build an adapter (spec §6).
        self._supervisor = supervisor
        self._endpoint_provider = endpoint_provider or _settings_endpoint_provider(settings)
        self._adapter_factory = adapter_factory or _build_opencode_adapter
        self._bindings: dict[UUID, _BoundRuntime] = {}
        self.unavailable_bindings: dict[UUID, str] = {}

    async def build_pipeline(self, binding: AgentBinding) -> AgentBackend:
        """Resolve and activate one binding's runtime, fail closed on any doubt.

        Resolution order follows M4.5 spec §6: binding runtime fields →
        endpoint → adapter → capabilities → scope → supervisor
        ``accept_activation`` gate.  Any missing runtime field or a rejected
        activation raises :class:`RuntimeNotReadyError` and the binding stays
        unmapped (the constructed adapter is closed so its owned HTTP client
        is never leaked).

        TODO(M4.5 pipeline task): wrap the adapter in ``AgentPipelineService``,
        start it, and return the pipeline instead of the bare adapter.
        """
        runtime_ref, epoch = self._resolve_binding_runtime(binding)
        base_url, directory = self._endpoint_provider(runtime_ref)
        adapter = self._adapter_factory(
            base_url=base_url,
            directory=directory,
            backend_version=PINNED_OPENCODE_BACKEND_VERSION,
            runtime_id=str(runtime_ref),
            binding_capability_epoch=epoch,
        )
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
        self._bindings[binding.id] = _BoundRuntime(
            adapter=adapter,
            scope=scope,
            capabilities=capabilities,
            runtime_ref=runtime_ref,
            runtime_epoch=epoch,
            capability_ref=binding.capability_ref,
        )
        return adapter

    async def start_all(self, bindings: Iterable[AgentBinding]) -> None:
        """Activate every binding that can prove runtime readiness.

        Bindings are activated independently (per-binding fail-closed
        isolation): a binding whose runtime is missing or rejected is left
        disabled/unmapped and recorded in ``unavailable_bindings`` so the
        remaining bindings keep running (M4.5 spec §6.2.1).  Any other
        failure propagates.
        """
        for binding in bindings:
            try:
                await self.build_pipeline(binding)
            except RuntimeNotReadyError as exc:
                self.unavailable_bindings[binding.id] = str(exc)

    async def stop_all(self) -> None:
        """Stop every pipeline and release every runtime adapter.

        TODO(M4.5 pipeline task): ``pipeline.stop()`` precedes ``adapter.close()``.
        """
        runtimes = list(self._bindings.values())
        # Clear the mapping before closing so a failing close can never leave
        # a half-closed binding resolvable again.
        self._bindings.clear()
        for runtime in runtimes:
            await runtime.adapter.close()

    def pipeline_for(self, binding_id: UUID) -> AgentBackend | None:
        """Return the binding's pipeline; currently resolves to the bare adapter.

        TODO(M4.5 pipeline task): returns ``AgentPipelineService`` once the
        per-binding orchestration exists.
        """
        runtime = self._bindings.get(binding_id)
        return runtime.adapter if runtime is not None else None

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
