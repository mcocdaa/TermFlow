"""Agent Broker B feature plugin registration (plan §2.1, §3.4).

The Agent Broker is a trusted, first-party B feature plugin with explicit
enable/disable configuration.  It registers its capability-discovery route,
its administration and conversation routers, its migration ownership, and its
lifecycle hooks through the
:class:`~termflow_control_plane.plugins.protocol.BFeaturePlugin` contract.

Services (M1.3+), event handlers (M3), and background lifecycle tasks
(M1.7) land in later milestones; this module deliberately stays minimal
(YAGNI) and only declares what the composition root needs today.

Restart recovery (:func:`run_agent_recovery`, plan §17) is the startup
side of the lifecycle wiring: after the plugin registry starts, the
composition root fences stale inbox claims, marks stuck runs unknown, and
retries pending cleanup tombstones in deterministic order, never failing
the process when a single item cannot be recovered.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from termflow_control_plane.api.agent_admin import router as agent_admin_router
from termflow_control_plane.api.agent_approvals import router as agent_approvals_router
from termflow_control_plane.api.agent_capabilities import get_agent_capabilities
from termflow_control_plane.api.agent_conversations import router as agent_conversations_router
from termflow_control_plane.api.agent_stream import router as agent_stream_router
from termflow_control_plane.persistence.models import AgentCleanupJob
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.inbox import (
    InboxDeliveryStateMachine,
)
from termflow_control_plane.plugins.agent_broker.agent.runs import AgentRunStateMachine
from termflow_control_plane.plugins.protocol import (
    BFeatureContext,
    EventSubscriptionRegistry,
    FeatureRouteRegistry,
    MigrationManifest,
    MigrationRevision,
    RoutePolicy,
)

logger = logging.getLogger(__name__)

_ADMIN_POLICY = RoutePolicy(auth_required=True)

#: Backoff before a failed cleanup tombstone is retried by the next recovery.
_AGENT_CLEANUP_RETRY_BACKOFF = timedelta(minutes=5)

#: A cleanup handler performs one target_kind's durable deletion work; it
#: raises when the attempt failed and the job must be retried.
CleanupJobHandler = Callable[[AgentCleanupJob], Awaitable[None]]


@dataclass
class AgentRecoveryReport:
    """Counts from one restart-recovery sweep (plan §17: B restarts)."""

    inbox_recovered: int = 0
    inbox_delivery_unknown: int = 0
    inbox_reconciled: int = 0
    runs_marked_unknown: int = 0
    cleanup_jobs_retried: int = 0
    cleanup_jobs_completed: int = 0


async def run_agent_recovery(
    repositories: RepositoryBundle,
    *,
    now: datetime | None = None,
    inbox_machine: InboxDeliveryStateMachine | None = None,
    run_machine: AgentRunStateMachine | None = None,
    cleanup_handlers: Mapping[str, CleanupJobHandler] | None = None,
) -> AgentRecoveryReport:
    """Deterministic restart recovery in the plan §17 order.

    1. ``recover_stale`` fences inbox claims: expired claims are restored to
       ``pending`` (provably not started) or parked ``delivery_unknown``
       unless reconciliation proves the outcome.
    2. Runs stuck in ``running`` are fenced to ``unknown``.
    3. Pending cleanup tombstones are retried: a registered handler for the
       job's ``target_kind`` performs the work (success completes the job),
       and any failure - including a missing handler - records an attempt
       with a retry backoff so the tombstone stays visible as
       ``deletion_pending``.

    The sweep is fail-safe: every step and every item is guarded, failures
    are logged, and the process keeps starting.  ``inbox_machine`` and
    ``run_machine`` default to fresh state machines (a restart never carries
    in-memory submission state across processes).
    """
    observed = now or datetime.now(UTC)
    report = AgentRecoveryReport()

    # 1) Stale inbox claims: expired leases are fenced before anything else.
    try:
        recovered = await (inbox_machine or InboxDeliveryStateMachine(
            repositories.agent_inbox
        )).recover_stale(now=observed)
        for envelope in recovered:
            if envelope.delivery_state == "pending":
                report.inbox_recovered += 1
            elif envelope.delivery_state == "delivery_unknown":
                report.inbox_delivery_unknown += 1
            elif envelope.delivery_state == "dispatched":
                report.inbox_reconciled += 1
    except Exception as exc:
        logger.exception("Agent inbox recovery failed: %s", exc)

    # 2) Runs stuck mid-flight are fenced to unknown.
    run_state_machine = run_machine or AgentRunStateMachine(repositories.agent_runs)
    try:
        for run in await repositories.agent_runs.list_by_state("running"):
            try:
                await run_state_machine.mark_unknown(run.id)
                report.runs_marked_unknown += 1
            except Exception as exc:
                logger.exception(
                    "Agent run %s could not be marked unknown: %s", run.id, exc
                )
    except Exception as exc:
        logger.exception("Agent run recovery scan failed: %s", exc)

    # 3) Pending cleanup tombstones are retried (or completed) in order.
    handlers = cleanup_handlers or {}
    try:
        for job in await repositories.cleanup_jobs.list_pending(now=observed):
            handler = handlers.get(job.target_kind)
            try:
                if handler is None:
                    raise RuntimeError(
                        "no cleanup handler registered for "
                        f"target_kind={job.target_kind!r}"
                    )
                await handler(job)
            except Exception as exc:
                logger.exception(
                    "Agent cleanup job %s (%s %s) retry failed; scheduling backoff: %s",
                    job.id,
                    job.target_kind,
                    job.target_ref,
                    exc,
                )
                await repositories.cleanup_jobs.record_attempt(
                    job.id,
                    next_attempt_at=observed + _AGENT_CLEANUP_RETRY_BACKOFF,
                    last_error=str(exc) or exc.__class__.__name__,
                )
                report.cleanup_jobs_retried += 1
            else:
                await repositories.cleanup_jobs.complete(job.id, now=observed)
                report.cleanup_jobs_completed += 1
    except Exception as exc:
        logger.exception("Agent cleanup job scan failed: %s", exc)

    return report


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
        for feature_router in (
            agent_admin_router,
            agent_conversations_router,
            agent_stream_router,
            agent_approvals_router,
        ):
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
        manifest.add_revision(
            MigrationRevision(id="0007", owner="agent_broker", dependencies=("0006",))
        )

    async def startup(self, context: BFeatureContext) -> None:
        """No startup work yet; repositories and lifecycle tasks land with M1.4+."""

    async def shutdown(self) -> None:
        """No shutdown work yet."""
