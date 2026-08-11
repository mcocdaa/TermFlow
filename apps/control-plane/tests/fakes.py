"""Fake agent runtime supervisor for tests.

Pins down the fail-closed lifecycle semantics the broker relies on without
any real backend: epoch attestation on register/restart, activation blocking
after a quiesce drain timeout, rejection (and recording) of late old-epoch
tool calls, and ready-gated binding activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from termflow_control_plane.plugins.protocol import (
    BackendOperationOutcome,
    BackendOperationResult,
    CapabilityRef,
    DrainStatus,
    RuntimeHealth,
    RuntimeRef,
    RuntimeStatus,
)


@dataclass(slots=True)
class _RuntimeState:
    binding_id: str
    epoch: int
    capability_ref: CapabilityRef
    ready: bool
    drain_status: DrainStatus = DrainStatus.NOT_ATTEMPTED


class FakeAgentRuntimeSupervisor:
    """In-memory :class:`AgentRuntimeSupervisor` for tests and fake composition roots.

    ``ready``/``epoch`` configure the initial state a runtime is attested in
    on its first registration.
    """

    def __init__(self, *, ready: bool = True, epoch: int = 1) -> None:
        self._runtimes: dict[RuntimeRef, _RuntimeState] = {}
        self._initial_ready = ready
        self._initial_epoch = epoch
        self.rejected_registrations: list[tuple[str, RuntimeRef, int, int]] = []
        self.rejected_activations: list[tuple[RuntimeRef, int, int]] = []
        self.rejected_tool_calls: list[tuple[RuntimeRef, int, int]] = []
        self.cleaned_up: list[RuntimeRef] = []

    async def register(
        self,
        binding_id: str,
        runtime_ref: RuntimeRef,
        epoch: int,
        capability_ref: CapabilityRef,
    ) -> None:
        state = self._runtimes.get(runtime_ref)
        if state is not None and epoch < state.epoch:
            self.rejected_registrations.append((binding_id, runtime_ref, epoch, state.epoch))
            return
        self._runtimes[runtime_ref] = _RuntimeState(
            binding_id=binding_id,
            epoch=epoch,
            capability_ref=capability_ref,
            ready=self._initial_ready,
        )

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth:
        state = self._runtimes.get(runtime_ref)
        if state is None:
            return RuntimeHealth(
                status=RuntimeStatus.UNKNOWN,
                epoch=self._initial_epoch,
                observed_at=datetime.now(UTC),
            )
        status = RuntimeStatus.READY if state.ready else RuntimeStatus.NOT_READY
        return RuntimeHealth(
            status=status,
            epoch=state.epoch,
            observed_at=datetime.now(UTC),
        )

    async def quiesce(self, runtime_ref: RuntimeRef, deadline: datetime) -> DrainStatus:
        state = self._runtimes.get(runtime_ref)
        if datetime.now(UTC) > deadline:
            result = DrainStatus.DRAIN_TIMEOUT
        else:
            result = DrainStatus.DRAINED
        if state is not None:
            state.drain_status = result
            if result is DrainStatus.DRAIN_TIMEOUT:
                state.ready = False
        return result

    async def restart(
        self,
        runtime_ref: RuntimeRef,
        epoch: int,
        capability_ref: CapabilityRef,
    ) -> RuntimeHealth:
        state = self._runtimes.get(runtime_ref)
        if state is not None and epoch < state.epoch:
            self.rejected_registrations.append(
                (state.binding_id, runtime_ref, epoch, state.epoch)
            )
            return await self.health(runtime_ref)
        self._runtimes[runtime_ref] = _RuntimeState(
            binding_id=state.binding_id if state is not None else "unbound",
            epoch=epoch,
            capability_ref=capability_ref,
            ready=True,
        )
        return RuntimeHealth(
            status=RuntimeStatus.READY,
            epoch=epoch,
            observed_at=datetime.now(UTC),
        )

    async def cleanup(self, runtime_ref: RuntimeRef) -> BackendOperationResult:
        self.cleaned_up.append(runtime_ref)
        self._runtimes.pop(runtime_ref, None)
        return BackendOperationResult(
            outcome=BackendOperationOutcome.CONFIRMED,
            message=f"runtime {runtime_ref} cleaned up",
        )

    def accept_activation(self, runtime_ref: RuntimeRef, epoch: int) -> bool:
        """Fail-closed gate for starting a new conversation/run on a binding."""
        state = self._runtimes.get(runtime_ref)
        if state is None:
            self.rejected_activations.append((runtime_ref, epoch, self._initial_epoch))
            return False
        if state.drain_status is DrainStatus.DRAIN_TIMEOUT:
            self.rejected_activations.append((runtime_ref, epoch, state.epoch))
            return False
        if not state.ready:
            self.rejected_activations.append((runtime_ref, epoch, state.epoch))
            return False
        if epoch != state.epoch:
            self.rejected_activations.append((runtime_ref, epoch, state.epoch))
            return False
        return True

    def accept_tool_call(self, runtime_ref: RuntimeRef, epoch: int) -> bool:
        """Admit a tool call only for the currently attested runtime epoch."""
        state = self._runtimes.get(runtime_ref)
        if state is None:
            self.rejected_tool_calls.append((runtime_ref, epoch, self._initial_epoch))
            return False
        if epoch != state.epoch:
            self.rejected_tool_calls.append((runtime_ref, epoch, state.epoch))
            return False
        return True
