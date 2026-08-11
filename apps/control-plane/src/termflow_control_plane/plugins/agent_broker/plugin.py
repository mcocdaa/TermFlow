"""Agent Broker B feature plugin registration (plan §2.1, §3.4).

The Agent Broker is a trusted, first-party B feature plugin with explicit
enable/disable configuration.  It registers its capability-discovery route,
its administration and conversation routers, its migration ownership, and its
lifecycle hooks through the
:class:`~termflow_control_plane.plugins.protocol.BFeaturePlugin` contract.

Services (M1.3+), event handlers (M3), and background lifecycle tasks
(M1.7) land in later milestones; this module deliberately stays minimal
(YAGNI) and only declares what the composition root needs today.
"""

from __future__ import annotations

from termflow_control_plane.api.agent_admin import router as agent_admin_router
from termflow_control_plane.api.agent_capabilities import get_agent_capabilities
from termflow_control_plane.api.agent_conversations import router as agent_conversations_router
from termflow_control_plane.plugins.protocol import (
    BFeatureContext,
    EventSubscriptionRegistry,
    FeatureRouteRegistry,
    MigrationManifest,
    MigrationRevision,
    RoutePolicy,
)

_ADMIN_POLICY = RoutePolicy(auth_required=True)


class AgentBrokerPlugin:
    """First-party Agent Broker feature plugin.

    The composition root registers one instance with
    ``enabled=settings.agent_broker_enabled``.  The capability-discovery
    endpoint is mounted unconditionally so C can observe the disabled state;
    the functional administration/conversation routers are only mounted (in
    app.py) while the plugin is enabled.
    """

    id = "agent_broker"
    version = "0.2.0-dev.0"
    requires_core_api = "0.2.0"

    def register_services(self, context: BFeatureContext) -> None:
        """No Agent services yet; they land with M1.3+."""

    def register_routes(self, routes: FeatureRouteRegistry) -> None:
        routes.add_route(
            "/api/v1/agent/capabilities",
            method="GET",
            handler=get_agent_capabilities,
            owner=self.id,
            # Unauthenticated by design: C must be able to discover that the
            # plugin is disabled without relying on Agent API 404 responses.
            policy=RoutePolicy(auth_required=False, csrf_required=False),
        )
        for feature_router in (agent_admin_router, agent_conversations_router):
            for route in feature_router.routes:
                for method in route.methods:
                    routes.add_route(
                        route.path,
                        method=method,
                        handler=route.endpoint,
                        owner=self.id,
                        policy=_ADMIN_POLICY,
                    )

    def register_event_handlers(self, subscriptions: EventSubscriptionRegistry) -> None:
        """No event handlers yet; watch/inbox handlers land with M3."""

    def register_migrations(self, manifest: MigrationManifest) -> None:
        manifest.add_revision(
            MigrationRevision(id="0006", owner="agent_broker", dependencies=())
        )

    async def startup(self, context: BFeatureContext) -> None:
        """No startup work yet; repositories and lifecycle tasks land with M1.4+."""

    async def shutdown(self) -> None:
        """No shutdown work yet."""
