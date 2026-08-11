"""Registry that owns B feature plugin registration and lifecycle.

The registry preserves registration order, tracks which plugins started,
collects routes/subscriptions/migrations from enabled plugins, and tags
every collected entry with the id of the plugin that contributed it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from termflow_control_plane.plugins.protocol import (
    BFeatureContext,
    BFeaturePlugin,
    EventSubscription,
    InMemoryEventSubscriptionRegistry,
    InMemoryFeatureRouteRegistry,
    MigrationRevision,
    RouteEntry,
    StaticMigrationManifest,
)


class PluginRegistrationError(Exception):
    """Raised when a plugin cannot be registered (duplicate or invalid)."""


class PluginStartupError(Exception):
    """Aggregate startup failure naming the plugin that failed to start."""

    def __init__(self, plugin_id: str, message: str) -> None:
        super().__init__(message)
        self.plugin_id = plugin_id


class PluginShutdownError(Exception):
    """Aggregate shutdown failure carrying every plugin that failed."""

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures: tuple[tuple[str, Exception], ...] = tuple(failures)
        names = ", ".join(plugin_id for plugin_id, _ in self.failures)
        super().__init__(f"plugins failed to shut down: {names}")


@dataclass(frozen=True, slots=True)
class _PluginRegistration:
    plugin: BFeaturePlugin
    enabled: bool


class FeatureRegistry:
    """Registration-ordered lifecycle manager for B feature plugins."""

    def __init__(self) -> None:
        self._plugins: list[_PluginRegistration] = []
        self._started: list[BFeaturePlugin] = []

    def register(self, plugin: BFeaturePlugin, *, enabled: bool = True) -> None:
        if not isinstance(plugin, BFeaturePlugin):
            raise PluginRegistrationError(
                f"plugin does not satisfy BFeaturePlugin: {plugin!r}"
            )
        for registered in self._plugins:
            if registered.plugin is plugin:
                raise PluginRegistrationError(
                    f"duplicate plugin id: plugin {plugin.id!r} is already registered"
                )
            if (
                registered.plugin.id == plugin.id
                and registered.plugin.version == plugin.version
            ):
                raise PluginRegistrationError(
                    f"duplicate plugin (id, version): {plugin.id!r} "
                    f"version {plugin.version!r} is already registered"
                )
        self._plugins.append(_PluginRegistration(plugin=plugin, enabled=enabled))

    async def startup(self, context: BFeatureContext) -> None:
        for registered in self._plugins:
            if not registered.enabled:
                continue
            try:
                registered.plugin.register_services(context)
                await registered.plugin.startup(context)
            except Exception as exc:
                raise PluginStartupError(
                    registered.plugin.id,
                    f"plugin {registered.plugin.id!r} failed during startup: {exc}",
                ) from exc
            self._started.append(registered.plugin)

    async def shutdown(self) -> None:
        failures: list[tuple[str, Exception]] = []
        for plugin in reversed(self._started):
            try:
                await plugin.shutdown()
            except Exception as exc:
                failures.append((plugin.id, exc))
        self._started.clear()
        if failures:
            raise PluginShutdownError(failures)

    @property
    def routes(self) -> tuple[RouteEntry, ...]:
        collected: list[RouteEntry] = []
        for registered in self._plugins:
            if not registered.enabled:
                continue
            routes = InMemoryFeatureRouteRegistry()
            registered.plugin.register_routes(routes)
            for entry in routes.routes():
                collected.append(replace(entry, owner=registered.plugin.id))
        return tuple(collected)

    @property
    def subscriptions(self) -> tuple[EventSubscription, ...]:
        collected: list[EventSubscription] = []
        for registered in self._plugins:
            if not registered.enabled:
                continue
            subscriptions = InMemoryEventSubscriptionRegistry()
            registered.plugin.register_event_handlers(subscriptions)
            for entry in subscriptions.subscriptions():
                collected.append(replace(entry, owner=registered.plugin.id))
        return tuple(collected)

    @property
    def migrations(self) -> tuple[MigrationRevision, ...]:
        collected: list[MigrationRevision] = []
        for registered in self._plugins:
            if not registered.enabled:
                continue
            manifest = StaticMigrationManifest()
            registered.plugin.register_migrations(manifest)
            for revision in manifest.revisions():
                collected.append(replace(revision, owner=registered.plugin.id))
        return tuple(collected)
