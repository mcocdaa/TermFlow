"""FeatureContext construction and fake agent runtime supervisor tests.

``FeatureContext`` is the concrete dependency surface handed to B feature
plugins; the fake supervisor pins down the fail-closed lifecycle semantics
the broker relies on (epoch attestation, drain-timeout activation blocking,
late tool-call rejection, ready-gated activation) without any real backend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from termflow_control_plane.plugins.context import FeatureContext, build_feature_context
from termflow_control_plane.plugins.protocol import (
    AgentRuntimeSupervisor,
    BackendOperationOutcome,
    BFeatureContext,
    CapabilityRef,
    DrainStatus,
    RuntimeRef,
    RuntimeStatus,
)
from termflow_control_plane.plugins.registry import FeatureRegistry

from .fakes import FakeAgentRuntimeSupervisor

RT_ONE = RuntimeRef("runtime-1")
CAP_ONE = CapabilityRef("capability-1")


class TestFeatureContext:
    def test_exposes_all_seven_ports(self) -> None:
        auth, terms, observation, commands = object(), object(), object(), object()
        persistence, lifecycle, runtime = object(), object(), object()

        context = FeatureContext(
            auth=auth,
            terms=terms,
            observation=observation,
            commands=commands,
            persistence=persistence,
            lifecycle=lifecycle,
            runtime=runtime,
        )

        assert context.auth is auth
        assert context.terms is terms
        assert context.observation is observation
        assert context.commands is commands
        assert context.persistence is persistence
        assert context.lifecycle is lifecycle
        assert context.runtime is runtime
        assert isinstance(context, BFeatureContext)

    def test_factory_wires_all_ports(self) -> None:
        auth, terms, observation, commands = object(), object(), object(), object()
        persistence, lifecycle, runtime = object(), object(), object()

        context = build_feature_context(
            auth=auth,
            terms=terms,
            observation=observation,
            commands=commands,
            persistence=persistence,
            lifecycle=lifecycle,
            runtime=runtime,
        )

        assert context.auth is auth
        assert context.terms is terms
        assert context.observation is observation
        assert context.commands is commands
        assert context.persistence is persistence
        assert context.lifecycle is lifecycle
        assert context.runtime is runtime


class TestFakeAgentRuntimeSupervisor:
    @pytest.mark.asyncio
    async def test_satisfies_agent_runtime_supervisor_protocol(self) -> None:
        assert isinstance(FakeAgentRuntimeSupervisor(), AgentRuntimeSupervisor)

    @pytest.mark.asyncio
    async def test_register_records_attested_epoch_and_health_reports_ready(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor()

        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)

        health = await supervisor.health(RT_ONE)
        assert health.status is RuntimeStatus.READY
        assert health.epoch == 1

    @pytest.mark.asyncio
    async def test_restart_with_new_epoch_rotates_attested_epoch(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor()
        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)

        restarted = await supervisor.restart(RT_ONE, epoch=2, capability_ref=CAP_ONE)

        assert restarted.epoch == 2
        assert restarted.status is RuntimeStatus.READY
        health = await supervisor.health(RT_ONE)
        assert health.epoch == 2
        assert health.status is RuntimeStatus.READY

    @pytest.mark.asyncio
    async def test_stale_registration_rejected_and_recorded(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor()
        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)
        await supervisor.restart(RT_ONE, epoch=2, capability_ref=CAP_ONE)

        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)

        assert supervisor.rejected_registrations == [("binding-1", RT_ONE, 1, 2)]
        # The attested epoch is unchanged, so a stale runtime's own epoch no
        # longer matches what the supervisor reports.
        health = await supervisor.health(RT_ONE)
        assert health.epoch == 2

    @pytest.mark.asyncio
    async def test_quiesce_beyond_deadline_blocks_activation_fail_closed(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor()
        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)

        overdue = datetime.now(UTC) - timedelta(seconds=1)
        result = await supervisor.quiesce(RT_ONE, overdue)

        assert result is DrainStatus.DRAIN_TIMEOUT
        assert supervisor.accept_activation(RT_ONE, epoch=1) is False
        assert supervisor.rejected_activations

    @pytest.mark.asyncio
    async def test_quiesce_within_deadline_allows_activation(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor()
        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)

        future = datetime.now(UTC) + timedelta(seconds=30)
        result = await supervisor.quiesce(RT_ONE, future)

        assert result is DrainStatus.DRAINED
        assert supervisor.accept_activation(RT_ONE, epoch=1) is True

    @pytest.mark.asyncio
    async def test_late_old_epoch_tool_call_rejected_and_recorded(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor()
        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)
        await supervisor.restart(RT_ONE, epoch=2, capability_ref=CAP_ONE)

        assert supervisor.accept_tool_call(RT_ONE, epoch=1) is False

        assert supervisor.rejected_tool_calls == [(RT_ONE, 1, 2)]
        # The current-epoch runtime still accepts tool calls; nothing is
        # reassigned to the stale runtime.
        assert supervisor.accept_tool_call(RT_ONE, epoch=2) is True

    @pytest.mark.asyncio
    async def test_activation_denied_when_runtime_not_ready(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor(ready=False)
        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)

        health = await supervisor.health(RT_ONE)
        assert health.status is RuntimeStatus.NOT_READY
        assert supervisor.accept_activation(RT_ONE, epoch=1) is False
        assert supervisor.rejected_activations

    @pytest.mark.asyncio
    async def test_activation_allowed_when_ready_with_correct_epoch(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor(ready=False)
        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)

        restarted = await supervisor.restart(RT_ONE, epoch=2, capability_ref=CAP_ONE)

        assert restarted.status is RuntimeStatus.READY
        assert supervisor.accept_activation(RT_ONE, epoch=2) is True
        # A stale epoch is still denied even though the runtime is ready.
        assert supervisor.accept_activation(RT_ONE, epoch=1) is False

    @pytest.mark.asyncio
    async def test_cleanup_returns_confirmed_backend_operation_result(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor()
        await supervisor.register("binding-1", RT_ONE, epoch=1, capability_ref=CAP_ONE)

        result = await supervisor.cleanup(RT_ONE)

        assert result.outcome is BackendOperationOutcome.CONFIRMED
        assert supervisor.cleaned_up == [RT_ONE]


class _FakePlugin:
    """BFeaturePlugin-shaped fake that captures the context it receives."""

    id = "fake-agent-broker"
    version = "0.2.0"
    requires_core_api = "0.2.0"

    def __init__(self) -> None:
        self.received_context: FeatureContext | None = None

    def register_services(self, context: FeatureContext) -> None:
        self.received_context = context

    def register_routes(self, routes: object) -> None: ...

    def register_event_handlers(self, subscriptions: object) -> None: ...

    def register_migrations(self, manifest: object) -> None: ...

    async def startup(self, context: FeatureContext) -> None: ...

    async def shutdown(self) -> None: ...


class TestFeatureRegistryRoundTrip:
    @pytest.mark.asyncio
    async def test_registry_wires_fake_supervisor_as_runtime_port(self) -> None:
        supervisor = FakeAgentRuntimeSupervisor(ready=True, epoch=1)
        context = build_feature_context(
            auth=object(),
            terms=object(),
            observation=object(),
            commands=object(),
            persistence=object(),
            lifecycle=object(),
            runtime=supervisor,
        )
        plugin = _FakePlugin()
        registry = FeatureRegistry()
        registry.register(plugin)

        await registry.startup(context)

        assert isinstance(plugin.received_context, FeatureContext)
        assert plugin.received_context is context
        assert plugin.received_context.runtime is supervisor
        assert isinstance(context, BFeatureContext)
