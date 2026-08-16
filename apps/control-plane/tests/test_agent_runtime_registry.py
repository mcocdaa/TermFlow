"""AgentRuntimeRegistry tests (M4.5 spec §1/§6 binding-scoped runtime mapping).

The registry resolves each ``AgentBinding`` to a dedicated adapter instance
(base_url/directory/runtime_id/binding_capability_epoch), proves activation
through the supervisor's fail-closed ``accept_activation`` gate, and owns the
per-binding ``AgentPipelineService`` lifecycle (``start_all`` / ``stop_all`` /
``pipeline_for``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentBinding
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackendCapabilities,
    CancelScope,
    ConcurrencyMode,
    ContextMode,
    ReplayMode,
    RuntimeIsolation,
    SubmitMode,
    ToolCallIdentity,
)
from termflow_control_plane.plugins.agent_broker.agent.pipeline import (
    AgentPipelineService,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    PINNED_OPENCODE_BACKEND_VERSION,
    AgentRuntimeRegistry,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    RuntimeNotReadyError,
    SupervisorConnector,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import AgentStreamHub
from termflow_control_plane.plugins.protocol import (
    BackendOperationOutcome,
    BackendOperationResult,
    CapabilityRef,
    DrainStatus,
    RuntimeHealth,
    RuntimeRef,
    RuntimeStatus,
)

RUNTIME_REF = "runtime-1"
CAPABILITY_REF = "capability-1"


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    # Test seam: let tests open ad-hoc sessions against the same database.
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


@pytest.fixture
def hub() -> AgentStreamHub:
    return AgentStreamHub()


def make_binding(
    *,
    binding_id: UUID | None = None,
    runtime_ref: str | None = RUNTIME_REF,
    runtime_epoch: int | None = 1,
    capability_ref: str | None = CAPABILITY_REF,
) -> AgentBinding:
    return AgentBinding(
        id=binding_id or uuid4(),
        profile_id=uuid4(),
        term_id=uuid4(),
        status="active",
        runtime_ref=runtime_ref,
        runtime_epoch=runtime_epoch,
        capability_ref=capability_ref,
    )


class FakeRuntimeClient:
    """Minimal runtime-manager double: only ``health`` is exercised by the gate."""

    def __init__(self, *, ready: bool = True, epoch: int = 1) -> None:
        self._ready = ready
        self._epoch = epoch

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth:
        return RuntimeHealth(
            status=RuntimeStatus.READY if self._ready else RuntimeStatus.NOT_READY,
            epoch=self._epoch,
            observed_at=datetime.now(UTC),
        )

    async def quiesce(self, runtime_ref: RuntimeRef, deadline: datetime) -> DrainStatus:
        return DrainStatus.DRAINED

    async def restart(
        self, runtime_ref: RuntimeRef, epoch: int, capability_secret: str
    ) -> RuntimeHealth:
        return await self.health(runtime_ref)

    async def cleanup(self, runtime_ref: RuntimeRef) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOperationOutcome.CONFIRMED)


def make_supervisor(*, ready: bool = True, epoch: int = 1) -> SupervisorConnector:
    return SupervisorConnector(
        FakeRuntimeClient(ready=ready, epoch=epoch),
        lambda capability_ref, epoch: f"secret-{epoch}",
        health_poll_delay_seconds=0.0,
    )


class FakeAdapter:
    """Adapter double recording its constructor kwargs and close calls.

    ``events`` never yields (a hanging subscription) so a started pipeline's
    SSE consumer idles until ``stop()`` cancels it; no registry test needs to
    drive real backend events.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.directory = kwargs.get("directory", "/workspace")
        self.runtime_id = kwargs.get("runtime_id") or "runtime-1"

    async def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            context_mode=ContextMode.LOST_ON_RESTART,
            submit_mode=SubmitMode.NON_IDEMPOTENT,
            cancel_scope=CancelScope.CONVERSATION,
            replay_mode=ReplayMode.NONE,
            concurrency_mode=ConcurrencyMode.SERIALIZED,
            tool_call_identity=ToolCallIdentity.BINDING,
            runtime_isolation=RuntimeIsolation.BINDING,
        )

    async def events(self, scope):  # pragma: no cover - hanging subscription
        if False:
            yield

    async def close(self) -> None:
        self.closed = True


def make_registry(
    settings: Settings,
    repositories: RepositoryBundle,
    hub: AgentStreamHub,
    *,
    supervisor: SupervisorConnector | None = None,
    endpoint_provider=None,
    adapter_factory=None,
) -> AgentRuntimeRegistry:
    return AgentRuntimeRegistry(
        settings=settings,
        repositories=repositories,
        sessions=repositories.session_factory,
        hub=hub,
        supervisor=supervisor,
        endpoint_provider=endpoint_provider,
        adapter_factory=adapter_factory,
    )


# ---------------------------------------------------------------------------
# Tests: binding -> adapter mapping.
# ---------------------------------------------------------------------------


async def test_build_pipeline_maps_binding_to_adapter(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    registry = make_registry(settings, repositories, hub)
    binding = make_binding(runtime_ref=RUNTIME_REF, runtime_epoch=3)

    pipeline = await registry.build_pipeline(binding)

    assert isinstance(pipeline, AgentPipelineService)
    adapter = pipeline.adapter
    assert adapter.base_url == settings.agent_opencode_base_url
    assert adapter.directory == settings.agent_opencode_directory
    assert adapter.runtime_id == RUNTIME_REF
    assert adapter.binding_capability_epoch == 3
    assert adapter.backend_version == PINNED_OPENCODE_BACKEND_VERSION
    assert registry.pipeline_for(binding.id) is pipeline
    assert pipeline.started is False  # build constructs; start_all starts
    await registry.stop_all()


async def test_build_pipeline_applies_configured_reconcile_attempt_limit(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    configured = settings.model_copy(update={"agent_pipeline_reconcile_attempts": 9})
    registry = make_registry(configured, repositories, hub)

    pipeline = await registry.build_pipeline(make_binding())

    assert pipeline._reconcile_attempts == 9
    await registry.stop_all()


async def test_injected_endpoint_provider_resolves_runtime(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    def provider(runtime_ref: RuntimeRef) -> tuple[str, str]:
        return (f"http://endpoint/{runtime_ref}", f"/workspaces/{runtime_ref}")

    registry = make_registry(settings, repositories, hub, endpoint_provider=provider)
    binding = make_binding(runtime_ref=RUNTIME_REF, runtime_epoch=1)

    pipeline = await registry.build_pipeline(binding)

    assert pipeline.adapter.base_url == "http://endpoint/runtime-1"
    assert pipeline.adapter.directory == "/workspaces/runtime-1"
    await registry.stop_all()


async def test_start_all_activates_and_starts_each_binding(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    registry = make_registry(settings, repositories, hub)
    first = make_binding()
    second = make_binding(runtime_ref="runtime-2", runtime_epoch=5)

    await registry.start_all([first, second])

    first_pipeline = registry.pipeline_for(first.id)
    second_pipeline = registry.pipeline_for(second.id)
    assert isinstance(first_pipeline, AgentPipelineService)
    assert isinstance(second_pipeline, AgentPipelineService)
    assert first_pipeline.started is True
    assert second_pipeline.started is True
    assert first_pipeline.adapter.runtime_id == RUNTIME_REF
    assert first_pipeline.adapter.binding_capability_epoch == 1
    assert second_pipeline.adapter.runtime_id == "runtime-2"
    assert second_pipeline.adapter.binding_capability_epoch == 5
    await registry.stop_all()
    assert first_pipeline.started is False
    assert second_pipeline.started is False


# ---------------------------------------------------------------------------
# Tests: supervisor activation gate (fail closed).
# ---------------------------------------------------------------------------


async def test_supervisor_rejects_unregistered_runtime(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    supervisor = make_supervisor()  # nothing attested
    registry = make_registry(settings, repositories, hub, supervisor=supervisor)
    binding = make_binding()

    with pytest.raises(RuntimeNotReadyError):
        await registry.build_pipeline(binding)

    assert registry.pipeline_for(binding.id) is None


async def test_supervisor_rejects_epoch_mismatch(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    supervisor = make_supervisor(epoch=2)
    await supervisor.register(
        str(uuid4()), RuntimeRef(RUNTIME_REF), 2, CapabilityRef(CAPABILITY_REF)
    )
    registry = make_registry(settings, repositories, hub, supervisor=supervisor)
    binding = make_binding(runtime_epoch=1)

    with pytest.raises(RuntimeNotReadyError):
        await registry.build_pipeline(binding)

    assert registry.pipeline_for(binding.id) is None


async def test_rejected_activation_closes_adapter(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    supervisor = make_supervisor()  # nothing attested -> gate rejects
    created: list[FakeAdapter] = []

    def factory(**kwargs: Any) -> FakeAdapter:
        adapter = FakeAdapter(**kwargs)
        created.append(adapter)
        return adapter

    registry = make_registry(
        settings, repositories, hub, supervisor=supervisor, adapter_factory=factory
    )
    binding = make_binding()

    with pytest.raises(RuntimeNotReadyError):
        await registry.build_pipeline(binding)

    assert len(created) == 1
    assert created[0].closed is True
    assert registry.pipeline_for(binding.id) is None  # never mapped


async def test_supervisor_none_skips_gate(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    registry = make_registry(settings, repositories, hub)
    binding = make_binding()

    pipeline = await registry.build_pipeline(binding)

    assert registry.pipeline_for(binding.id) is pipeline
    await registry.stop_all()


async def test_attested_runtime_passes_gate(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    supervisor = make_supervisor(epoch=1)
    await supervisor.register(
        str(uuid4()), RuntimeRef(RUNTIME_REF), 1, CapabilityRef(CAPABILITY_REF)
    )
    registry = make_registry(settings, repositories, hub, supervisor=supervisor)
    binding = make_binding()

    pipeline = await registry.build_pipeline(binding)

    assert registry.pipeline_for(binding.id) is pipeline
    assert pipeline.adapter.runtime_id == RUNTIME_REF
    await registry.stop_all()


# ---------------------------------------------------------------------------
# Tests: fail-closed bindings without a runtime.
# ---------------------------------------------------------------------------


async def test_binding_without_runtime_ref_fails_closed(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    registry = make_registry(settings, repositories, hub)

    with pytest.raises(RuntimeNotReadyError):
        await registry.build_pipeline(make_binding(runtime_ref=None, runtime_epoch=None))


async def test_binding_without_runtime_epoch_fails_closed(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    registry = make_registry(settings, repositories, hub)

    with pytest.raises(RuntimeNotReadyError):
        await registry.build_pipeline(make_binding(runtime_epoch=None))


async def test_start_all_keeps_runtimeless_binding_disabled(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    registry = make_registry(settings, repositories, hub)
    ok = make_binding()
    broken = make_binding(runtime_ref=None, runtime_epoch=None)

    await registry.start_all([ok, broken])

    assert registry.pipeline_for(ok.id) is not None
    assert registry.pipeline_for(broken.id) is None
    assert broken.id in registry.unavailable_bindings
    assert "runtime_ref" in registry.unavailable_bindings[broken.id]
    await registry.stop_all()


# ---------------------------------------------------------------------------
# Tests: pipeline/adapter lifecycle.
# ---------------------------------------------------------------------------


async def test_stop_all_stops_pipelines_and_closes_every_adapter(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    created: list[FakeAdapter] = []

    def factory(**kwargs: Any) -> FakeAdapter:
        adapter = FakeAdapter(**kwargs)
        created.append(adapter)
        return adapter

    registry = make_registry(settings, repositories, hub, adapter_factory=factory)
    await registry.start_all([make_binding(), make_binding(runtime_ref="runtime-2")])
    assert len(created) == 2
    assert all(not adapter.closed for adapter in created)

    await registry.stop_all()

    assert all(adapter.closed for adapter in created)
    assert registry.pipeline_for(uuid4()) is None


async def test_fake_adapter_receives_expected_constructor_kwargs(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    received: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeAdapter:
        received.update(kwargs)
        return FakeAdapter(**kwargs)

    registry = make_registry(settings, repositories, hub, adapter_factory=factory)
    binding = make_binding(runtime_ref=RUNTIME_REF, runtime_epoch=7)

    await registry.build_pipeline(binding)

    assert received["base_url"] == settings.agent_opencode_base_url
    assert received["directory"] == settings.agent_opencode_directory
    assert received["backend_version"] == PINNED_OPENCODE_BACKEND_VERSION
    assert received["runtime_id"] == RUNTIME_REF
    assert received["binding_capability_epoch"] == 7
    await registry.stop_all()


async def test_configured_basic_auth_is_forwarded_to_adapter_factory(
    settings: Settings, repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    received: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeAdapter:
        received.update(kwargs)
        return FakeAdapter(**kwargs)

    configured = settings.model_copy(
        update={
            "agent_opencode_username": "termflow",
            "agent_opencode_password": SecretStr("secret"),
        }
    )
    registry = make_registry(configured, repositories, hub, adapter_factory=factory)
    await registry.build_pipeline(make_binding())

    assert received["username"] == "termflow"
    assert received["password"] == "secret"
    await registry.stop_all()
