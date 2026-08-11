"""FeatureRegistry lifecycle and collection tests.

Uses recording fake plugins so call order and ownership tagging are
observable without any real plugin implementations.
"""

from __future__ import annotations

import pytest
from termflow_control_plane.plugins.protocol import (
    MigrationRevision,
    RoutePolicy,
    SubscriptionScope,
    SubscriptionScopeKind,
)
from termflow_control_plane.plugins.registry import (
    FeatureRegistry,
    PluginRegistrationError,
    PluginShutdownError,
    PluginStartupError,
)


class _FakeContext:
    auth = object()
    terms = object()
    observation = object()
    commands = object()
    persistence = object()
    lifecycle = object()
    runtime = object()


class _RecordingPlugin:
    """Minimal BFeaturePlugin-shaped fake that records lifecycle calls."""

    def __init__(
        self,
        plugin_id: str,
        *,
        version: str = "0.1.0",
        calls: list[str] | None = None,
        route_owner: str | None = None,
        fail_services: bool = False,
        fail_startup: bool = False,
        fail_shutdown: bool = False,
    ) -> None:
        self.id = plugin_id
        self.version = version
        self.requires_core_api = "0.2.0"
        self.calls = calls if calls is not None else []
        self.route_owner = route_owner if route_owner is not None else plugin_id
        self.fail_services = fail_services
        self.fail_startup = fail_startup
        self.fail_shutdown = fail_shutdown

    def register_services(self, context: object) -> None:
        self.calls.append(f"{self.id}:services")
        if self.fail_services:
            raise RuntimeError(f"{self.id} service registration boom")

    def register_routes(self, routes: object) -> None:
        self.calls.append(f"{self.id}:routes")
        routes.add_route(
            f"/api/v1/agent/{self.id}",
            method="GET",
            handler=self._handler,
            owner=self.route_owner,
            policy=RoutePolicy(),
        )

    def register_event_handlers(self, subscriptions: object) -> None:
        self.calls.append(f"{self.id}:events")
        subscriptions.subscribe(
            owner=self.route_owner,
            scope=SubscriptionScope(kind=SubscriptionScopeKind.GLOBAL),
            handler=self._on_event,
        )

    def register_migrations(self, manifest: object) -> None:
        self.calls.append(f"{self.id}:migrations")
        manifest.add_revision(
            MigrationRevision(id=f"{self.id}-0001", owner=self.route_owner)
        )

    async def startup(self, context: object) -> None:
        self.calls.append(f"{self.id}:startup")
        if self.fail_startup:
            raise RuntimeError(f"{self.id} startup boom")

    async def shutdown(self) -> None:
        self.calls.append(f"{self.id}:shutdown")
        if self.fail_shutdown:
            raise RuntimeError(f"{self.id} shutdown boom")

    def _handler(self) -> object:
        return None

    async def _on_event(self, event: object) -> None:
        return None


class TestRegistration:
    def test_duplicate_plugin_id_rejected(self) -> None:
        registry = FeatureRegistry()
        plugin = _RecordingPlugin("alpha")
        registry.register(plugin)
        with pytest.raises(PluginRegistrationError, match="already registered"):
            registry.register(plugin)

    def test_duplicate_id_version_rejected(self) -> None:
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", version="1.0.0"))
        with pytest.raises(PluginRegistrationError):
            registry.register(_RecordingPlugin("alpha", version="1.0.0"))

    def test_same_id_different_version_accepted(self) -> None:
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", version="1.0.0"))
        registry.register(_RecordingPlugin("alpha", version="2.0.0"))
        # Neither registration raised; both plugins are collected in order.
        assert [route.owner for route in registry.routes] == ["alpha", "alpha"]


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_startup_in_registration_order_shutdown_in_reverse(self) -> None:
        calls: list[str] = []
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", calls=calls))
        registry.register(_RecordingPlugin("beta", calls=calls))
        registry.register(_RecordingPlugin("gamma", calls=calls))

        await registry.startup(_FakeContext())

        assert calls == [
            "alpha:services",
            "alpha:startup",
            "beta:services",
            "beta:startup",
            "gamma:services",
            "gamma:startup",
        ]

        calls.clear()
        await registry.shutdown()

        assert calls == ["gamma:shutdown", "beta:shutdown", "alpha:shutdown"]

    @pytest.mark.asyncio
    async def test_disabled_plugin_not_started_and_contributes_nothing(self) -> None:
        calls: list[str] = []
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", calls=calls), enabled=True)
        registry.register(_RecordingPlugin("beta", calls=calls), enabled=False)

        await registry.startup(_FakeContext())

        assert calls == ["alpha:services", "alpha:startup"]
        assert [route.owner for route in registry.routes] == ["alpha"]
        assert [sub.owner for sub in registry.subscriptions] == ["alpha"]
        assert [rev.owner for rev in registry.migrations] == ["alpha"]

        calls.clear()
        await registry.shutdown()

        assert calls == ["alpha:shutdown"]

    @pytest.mark.asyncio
    async def test_register_services_failure_is_startup_error(self) -> None:
        calls: list[str] = []
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", calls=calls))
        registry.register(_RecordingPlugin("boom", calls=calls, fail_services=True))

        with pytest.raises(PluginStartupError) as excinfo:
            await registry.startup(_FakeContext())

        assert excinfo.value.plugin_id == "boom"
        assert "boom" in str(excinfo.value)
        assert calls == ["alpha:services", "alpha:startup", "boom:services"]

    @pytest.mark.asyncio
    async def test_shutdown_after_partial_startup_only_shuts_down_started(self) -> None:
        calls: list[str] = []
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", calls=calls))
        registry.register(_RecordingPlugin("boom", calls=calls, fail_startup=True))
        registry.register(_RecordingPlugin("zeta", calls=calls))

        with pytest.raises(PluginStartupError):
            await registry.startup(_FakeContext())

        calls.clear()
        await registry.shutdown()

        assert calls == ["alpha:shutdown"]

    @pytest.mark.asyncio
    async def test_startup_failure_names_plugin_and_stops_later_plugins(self) -> None:
        calls: list[str] = []
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", calls=calls))
        registry.register(_RecordingPlugin("boom", calls=calls, fail_startup=True))
        registry.register(_RecordingPlugin("zeta", calls=calls))

        with pytest.raises(PluginStartupError) as excinfo:
            await registry.startup(_FakeContext())

        assert excinfo.value.plugin_id == "boom"
        assert "boom" in str(excinfo.value)
        assert calls == [
            "alpha:services",
            "alpha:startup",
            "boom:services",
            "boom:startup",
        ]
        assert not any(call.startswith("zeta") for call in calls)

    @pytest.mark.asyncio
    async def test_shutdown_failure_aggregates_but_shuts_down_all(self) -> None:
        calls: list[str] = []
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", calls=calls))
        registry.register(_RecordingPlugin("boom", calls=calls, fail_shutdown=True))
        registry.register(_RecordingPlugin("zeta", calls=calls))

        await registry.startup(_FakeContext())
        calls.clear()

        with pytest.raises(PluginShutdownError) as excinfo:
            await registry.shutdown()

        assert calls == ["zeta:shutdown", "boom:shutdown", "alpha:shutdown"]
        assert [plugin_id for plugin_id, _ in excinfo.value.failures] == ["boom"]
        assert "boom" in str(excinfo.value)


class TestCollections:
    def test_routes_subscriptions_migrations_tagged_with_plugin_ids(self) -> None:
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha"))
        registry.register(_RecordingPlugin("beta"))

        assert [route.owner for route in registry.routes] == ["alpha", "beta"]
        assert [sub.owner for sub in registry.subscriptions] == ["alpha", "beta"]
        assert [rev.owner for rev in registry.migrations] == ["alpha", "beta"]

    def test_entries_retagged_with_owner_plugin_id(self) -> None:
        registry = FeatureRegistry()
        registry.register(_RecordingPlugin("alpha", route_owner="mismatched-owner"))

        assert [route.owner for route in registry.routes] == ["alpha"]
        assert [sub.owner for sub in registry.subscriptions] == ["alpha"]
        assert [rev.owner for rev in registry.migrations] == ["alpha"]
