"""Typed contract definitions for B feature plugins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from termflow_control_plane.plugins.protocol import (
    MAX_EVIDENCE_ITEMS,
    AgentRuntimeSupervisor,
    BackendOperationOutcome,
    BackendOperationResult,
    BFeatureContext,
    BFeaturePlugin,
    DrainStatus,
    EventSubscriptionRegistry,
    EvidenceRecord,
    FeatureRouteRegistry,
    InMemoryEventSubscriptionRegistry,
    InMemoryFeatureRouteRegistry,
    MigrationManifest,
    MigrationRevision,
    RoutePolicy,
    RuntimeHealth,
    RuntimeStatus,
    StaticMigrationManifest,
    SubscriptionScope,
    SubscriptionScopeKind,
)


class _FakePlugin:
    id = "fake-agent-broker"
    version = "0.2.0"
    requires_core_api = "0.2.0"

    def register_services(self, context: BFeatureContext) -> None: ...

    def register_routes(self, routes: FeatureRouteRegistry) -> None: ...

    def register_event_handlers(self, subscriptions: EventSubscriptionRegistry) -> None: ...

    def register_migrations(self, manifest: MigrationManifest) -> None: ...

    async def startup(self, context: BFeatureContext) -> None: ...

    async def shutdown(self) -> None: ...


class _FakeContext:
    auth = object()
    terms = object()
    observation = object()
    commands = object()
    persistence = object()
    lifecycle = object()
    runtime = object()


class _FakeSupervisor:
    async def register(self, binding_id, runtime_ref, epoch, capability_ref) -> None: ...

    async def health(self, runtime_ref) -> RuntimeHealth:
        return RuntimeHealth(
            status=RuntimeStatus.READY,
            epoch=1,
            observed_at=datetime.now(UTC),
        )

    async def quiesce(self, runtime_ref, deadline) -> DrainStatus:
        return DrainStatus.DRAINED

    async def restart(self, runtime_ref, epoch, capability_ref) -> RuntimeHealth:
        return RuntimeHealth(
            status=RuntimeStatus.NOT_READY,
            epoch=epoch,
            observed_at=datetime.now(UTC),
        )

    async def cleanup(self, runtime_ref) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOperationOutcome.CONFIRMED)


def _noop_handler(message: object) -> None: ...


class TestProtocolStructuralChecks:
    def test_plugin_protocols_are_runtime_checkable(self) -> None:
        assert isinstance(_FakePlugin(), BFeaturePlugin)
        assert isinstance(_FakeContext(), BFeatureContext)
        assert isinstance(_FakeSupervisor(), AgentRuntimeSupervisor)

    def test_protocol_rejects_structurally_incomplete_objects(self) -> None:
        assert not isinstance(object(), BFeaturePlugin)
        assert not isinstance(object(), BFeatureContext)

        class _MissingShutdown:
            id = "fake"
            version = "0.2.0"
            requires_core_api = "0.2.0"

            def register_services(self, context: BFeatureContext) -> None: ...

            def register_routes(self, routes: FeatureRouteRegistry) -> None: ...

            def register_event_handlers(
                self, subscriptions: EventSubscriptionRegistry
            ) -> None: ...

            def register_migrations(self, manifest: MigrationManifest) -> None: ...

            async def startup(self, context: BFeatureContext) -> None: ...

        assert not isinstance(_MissingShutdown(), BFeaturePlugin)


class TestFeatureRouteRegistry:
    def test_accepts_routes_inside_agent_namespace(self) -> None:
        registry = InMemoryFeatureRouteRegistry()
        policy = RoutePolicy(auth_required=True, acls=("agent:runs:write",), csrf_required=True)
        registry.add_route(
            "/api/v1/agent",
            method="GET",
            handler=lambda: None,
            owner="fake-agent-broker",
            policy=policy,
        )
        registry.add_route(
            "/api/v1/agent/runs",
            method="POST",
            handler=lambda: None,
            owner="fake-agent-broker",
            policy=policy,
        )
        assert len(registry.routes()) == 2
        entry = registry.routes()[0]
        assert entry.owner == "fake-agent-broker"
        assert entry.policy == policy
        assert entry.path == "/api/v1/agent"

    @pytest.mark.parametrize(
        "path",
        ["/admin", "/api/v1/terminal/panes", "/", "/api/v1/agent_legacy"],
    )
    def test_rejects_routes_outside_agent_namespace(self, path: str) -> None:
        registry = InMemoryFeatureRouteRegistry()
        with pytest.raises(ValueError):
            registry.add_route(
                path,
                method="GET",
                handler=lambda: None,
                owner="fake-agent-broker",
                policy=RoutePolicy(),
            )

    def test_rejects_empty_owner(self) -> None:
        registry = InMemoryFeatureRouteRegistry()
        with pytest.raises(ValueError):
            registry.add_route(
                "/api/v1/agent/runs",
                method="GET",
                handler=lambda: None,
                owner="",
                policy=RoutePolicy(),
            )


class TestEventSubscriptionRegistry:
    def test_requires_owner_and_typed_scope(self) -> None:
        registry = InMemoryEventSubscriptionRegistry()
        scope = SubscriptionScope(kind=SubscriptionScopeKind.BINDING, binding_id="binding-1")
        subscription = registry.subscribe(
            owner="fake-agent-broker",
            scope=scope,
            handler=_noop_handler,
        )
        assert subscription.owner == "fake-agent-broker"
        assert subscription.scope == scope
        assert registry.subscriptions() == (subscription,)

    def test_rejects_empty_owner(self) -> None:
        registry = InMemoryEventSubscriptionRegistry()
        with pytest.raises(ValueError):
            registry.subscribe(
                owner="",
                scope=SubscriptionScope(kind=SubscriptionScopeKind.GLOBAL),
                handler=_noop_handler,
            )

    def test_rejects_untyped_scope(self) -> None:
        registry = InMemoryEventSubscriptionRegistry()
        with pytest.raises(TypeError):
            registry.subscribe(owner="fake-agent-broker", scope=object(), handler=_noop_handler)

    def test_binding_scope_requires_binding_id(self) -> None:
        with pytest.raises(ValueError):
            SubscriptionScope(kind=SubscriptionScopeKind.BINDING)


class TestMigrationManifest:
    def test_records_revisions_with_owner_and_dependencies(self) -> None:
        manifest = StaticMigrationManifest()
        first = MigrationRevision(id="0001", owner="fake-agent-broker")
        second = MigrationRevision(id="0002", owner="fake-agent-broker", dependencies=("0001",))
        manifest.add_revision(first)
        manifest.add_revision(second)
        assert manifest.revisions() == (first, second)
        assert manifest.revisions()[1].dependencies == ("0001",)

    def test_rejects_duplicate_revision_ids(self) -> None:
        manifest = StaticMigrationManifest()
        manifest.add_revision(MigrationRevision(id="0001", owner="fake-agent-broker"))
        with pytest.raises(ValueError):
            manifest.add_revision(MigrationRevision(id="0001", owner="another-owner"))

    def test_rejects_empty_revision_id_or_owner(self) -> None:
        with pytest.raises(ValueError):
            MigrationRevision(id="", owner="fake-agent-broker")
        with pytest.raises(ValueError):
            MigrationRevision(id="0001", owner=" ")


class TestOutcomeValidation:
    def test_runtime_status_accepts_explicit_values_only(self) -> None:
        assert RuntimeStatus("ready") is RuntimeStatus.READY
        assert RuntimeStatus("not_ready") is RuntimeStatus.NOT_READY
        assert RuntimeStatus("unknown") is RuntimeStatus.UNKNOWN
        with pytest.raises(ValueError):
            RuntimeStatus("degraded")

    def test_drain_status_accepts_explicit_values_only(self) -> None:
        assert DrainStatus("drained") is DrainStatus.DRAINED
        assert DrainStatus("drain_timeout") is DrainStatus.DRAIN_TIMEOUT
        assert DrainStatus("not_attempted") is DrainStatus.NOT_ATTEMPTED
        with pytest.raises(ValueError):
            DrainStatus("flushed")

    def test_runtime_health_rejects_invalid_status(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeHealth(status="degraded", epoch=1, observed_at=datetime.now(UTC))

    def test_backend_operation_result_requires_explicit_outcome(self) -> None:
        result = BackendOperationResult(outcome="retryable", message="provider over capacity")
        assert result.outcome is BackendOperationOutcome.RETRYABLE
        assert result.message == "provider over capacity"
        with pytest.raises(ValidationError):
            BackendOperationResult(outcome="unrecognized_outcome")

    def test_backend_operation_result_bounds_evidence(self) -> None:
        unbounded = tuple(
            EvidenceRecord(key=f"key-{index}", value="value")
            for index in range(MAX_EVIDENCE_ITEMS + 1)
        )
        with pytest.raises(ValidationError):
            BackendOperationResult(outcome=BackendOperationOutcome.CONFIRMED, evidence=unbounded)
        bounded = unbounded[:MAX_EVIDENCE_ITEMS]
        result = BackendOperationResult(outcome=BackendOperationOutcome.CONFIRMED, evidence=bounded)
        assert len(result.evidence) == MAX_EVIDENCE_ITEMS

    def test_runtime_health_epoch_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeHealth(
                status=RuntimeStatus.READY,
                epoch=0,
                observed_at=datetime.now(UTC),
            )

    @pytest.mark.asyncio
    async def test_quiesce_deadline_typing(self) -> None:
        supervisor = _FakeSupervisor()
        deadline = datetime.now(UTC) + timedelta(seconds=30)
        assert isinstance(await supervisor.quiesce("runtime-ref", deadline), DrainStatus)
