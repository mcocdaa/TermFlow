"""Agent Broker B feature plugin registration (plan §2.1, §3.4).

The Agent Broker is a trusted, first-party B feature plugin with explicit
enable/disable configuration.  It registers its capability-discovery route,
its administration and conversation routers, its migration ownership, and its
lifecycle hooks through the
:class:`~termflow_control_plane.plugins.protocol.BFeaturePlugin` contract.

Restart recovery (:func:`run_agent_recovery`, plan §17) is the startup
side of the lifecycle wiring: the composition root fences stale inbox claims,
marks stuck runs unknown, and retries pending cleanup tombstones before the
plugin starts its runtime services, never failing the process when a single
item cannot be recovered.

Runtime wiring (M4.5 spec §3a/§3b/§7): the composition root builds the
per-binding ``AgentRuntimeRegistry`` (agent/runtime_registry.py) and the
``WatchEngine`` (agent/watches.py)
and attaches them through :meth:`AgentBrokerPlugin.bind_runtime_services`
before the feature registry starts the plugin.  ``startup`` then runs them in
the deterministic spec order - watch engine (rebuild + subscribe) first, then
per-binding pipelines, then the deadline tick task - and ``shutdown`` stops
them in reverse.  The plugin is only started while enabled, so the
``agent_broker_enabled`` gate is automatic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.api.agent_admin import router as agent_admin_router
from termflow_control_plane.api.agent_approvals import router as agent_approvals_router
from termflow_control_plane.api.agent_capabilities import get_agent_capabilities
from termflow_control_plane.api.agent_conversations import router as agent_conversations_router
from termflow_control_plane.api.agent_stream import router as agent_stream_router
from termflow_control_plane.persistence.models import AgentBinding, AgentCleanupJob
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.inbox import (
    InboxDeliveryStateMachine,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import ApprovalPolicy
from termflow_control_plane.plugins.agent_broker.agent.runs import AgentRunStateMachine
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    AgentRuntimeRegistry,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import binding_is_closed
from termflow_control_plane.plugins.agent_broker.agent.watches import (
    FiredTrigger,
    WatchEngine,
)
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
    approvals_revoked: int = 0
    approvals_marked_unknown: int = 0


async def run_agent_recovery(
    repositories: RepositoryBundle,
    *,
    now: datetime | None = None,
    inbox_machine: InboxDeliveryStateMachine | None = None,
    run_machine: AgentRunStateMachine | None = None,
    cleanup_handlers: Mapping[str, CleanupJobHandler] | None = None,
    approval_policy: ApprovalPolicy | None = None,
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
    4. Orphaned approvals are finished (M5.2, spec §5): never-decided
       ``pending`` requests become ``revoked`` and unconsumed ``approved``
       requests become ``unknown`` - every in-flight tool call died with the
       restart, so no waiter remains and nothing is ever replayed.  The
       counts merge into the report.

    The sweep is fail-safe: every step and every item is guarded, failures
    are logged, and the process keeps starting.  ``inbox_machine`` and
    ``run_machine`` default to fresh state machines (a restart never carries
    in-memory submission state across processes); ``approval_policy`` is the
    composition root's shared policy (audit writer included) when provided.
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

    # 4) Orphaned approvals: pending -> revoked, approved -> unknown.
    if approval_policy is not None:
        try:
            revoked, unknown = await approval_policy.recover_orphaned_approvals(
                now=observed
            )
            report.approvals_revoked = revoked
            report.approvals_marked_unknown = unknown
        except Exception as exc:
            logger.exception("Agent approval recovery failed: %s", exc)

    return report


async def _list_active_bindings(
    sessions: async_sessionmaker[AsyncSession],
) -> list[AgentBinding]:
    """Bindings eligible for runtime activation: everything not revoked/disabled.

    The repository exposes per-term/per-profile lists only, so the plugin
    scans through its own session factory and filters with the same closed
    vocabulary the stream layer uses (fail closed on any doubt).
    """
    async with sessions() as session:
        rows = await session.scalars(select(AgentBinding).order_by(AgentBinding.created_at))
        return [binding for binding in rows if not binding_is_closed(binding.status)]


async def run_watch_deadline_tick(
    watch_engine: WatchEngine,
    registry: AgentRuntimeRegistry,
    *,
    now: datetime | None = None,
) -> list[FiredTrigger]:
    """One deadline sweep: fire due watches and hand each trigger to its pipeline.

    M4.5 spec §3b wiring: ``check_deadlines`` performs the atomic trigger
    transaction (``fire()`` already inserted the inbox item and delivery),
    ``build_input`` renders the typed :class:`WatchTriggeredInput` from the
    fired trigger, and the binding's pipeline registers it in-process for
    its dispatcher.  A trigger whose binding has no pipeline is logged and
    left in the inbox (the dispatcher of a later activation will fail it
    closed if it can never be rendered).
    """
    observed_at = now or datetime.now(UTC)
    triggers = await watch_engine.check_deadlines(observed_at)
    for trigger in triggers:
        input = watch_engine.build_input(trigger, observed_at=observed_at)
        if input is None:
            logger.warning(
                "Agent watch trigger %s has no typed input; nothing to deliver",
                trigger.delivery.delivery_key,
            )
            continue
        pipeline = registry.pipeline_for(trigger.watch.binding_id)
        if pipeline is None:
            logger.warning(
                "Agent watch trigger %s: binding %s has no pipeline; "
                "the inbox item stays parked",
                trigger.delivery.delivery_key,
                trigger.watch.binding_id,
            )
            continue
        await pipeline.submit_watch_input(input)
    return triggers


async def watch_deadline_loop(
    watch_engine: WatchEngine,
    registry: AgentRuntimeRegistry,
    *,
    tick_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Periodic deadline sweep task started by the plugin's ``startup``.

    The first sweep runs immediately (deadlines due during startup fire
    right away); afterwards the loop waits ``tick_seconds`` between sweeps
    and exits promptly when ``stop`` is set.  A failed sweep never kills
    the task: the error is logged and the next tick retries.
    """
    while True:
        try:
            await run_watch_deadline_tick(watch_engine, registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent watch deadline sweep failed; retrying next tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except TimeoutError:
            pass
        if stop.is_set():
            return


class AgentBrokerPlugin:
    """First-party Agent Broker feature plugin.

    The composition root registers one instance with
    ``enabled=settings.agent_broker_enabled``.  The capability-discovery
    endpoint is mounted unconditionally so C can observe the disabled state;
    the functional administration/conversation routers are only mounted (in
    app.py) while the plugin is enabled.

    Runtime services (M4.5): the composition root builds the watch engine
    and the per-binding runtime registry and attaches them through
    :meth:`bind_runtime_services` before the feature registry starts the
    plugin.  ``startup`` runs them in the spec §3a/§7 order (restart
    recovery already fenced stale runs in app.py), and ``shutdown`` stops
    them in reverse.  Because the feature registry only starts enabled
    plugins, all of this wiring is gated by ``agent_broker_enabled``.
    """

    id = "agent_broker"
    version = "0.2.0-dev.0"
    requires_core_api = "0.2.0"

    def __init__(self) -> None:
        self._watch_engine: WatchEngine | None = None
        self._runtime_registry: AgentRuntimeRegistry | None = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None
        self._watch_tick_seconds: float = 1.0
        self._watch_tick_task: asyncio.Task[None] | None = None
        self._watch_tick_stop: asyncio.Event | None = None

    def bind_runtime_services(
        self,
        *,
        watch_engine: WatchEngine,
        registry: AgentRuntimeRegistry,
        sessions: async_sessionmaker[AsyncSession],
        watch_tick_seconds: float,
    ) -> None:
        """Receive the composition-root-built runtime services (M4.5 wiring).

        Called by app.py between restart recovery and feature startup: the
        services are constructed there because they need the repositories,
        session factory, and hubs that only exist inside the lifespan.
        """
        self._watch_engine = watch_engine
        self._runtime_registry = registry
        self._sessions = sessions
        self._watch_tick_seconds = watch_tick_seconds

    def register_services(self, context: BFeatureContext) -> None:
        """Runtime services are attached by the composition root (see above)."""

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
        manifest.add_revision(
            MigrationRevision(id="0008", owner="agent_broker", dependencies=("0007",))
        )

    async def startup(self, context: BFeatureContext) -> None:
        """Start the attached runtime services in the spec §3a/§7 order.

        Restart recovery already fenced stale inbox claims and stuck runs
        (app.py runs it before feature startup), so the watch engine loads
        its deadline heap and subscribes first, then per-binding pipelines
        activate, then the deadline tick task starts - a fired trigger can
        never arrive before a pipeline can receive it.
        """
        if self._watch_engine is None or self._runtime_registry is None:
            return
        assert self._sessions is not None
        await self._watch_engine.rebuild()
        await self._watch_engine.start()
        await self._runtime_registry.start_all(await _list_active_bindings(self._sessions))
        self._watch_tick_stop = asyncio.Event()
        self._watch_tick_task = asyncio.create_task(
            watch_deadline_loop(
                self._watch_engine,
                self._runtime_registry,
                tick_seconds=self._watch_tick_seconds,
                stop=self._watch_tick_stop,
            ),
            name="agent-watch-deadlines",
        )

    async def shutdown(self) -> None:
        """Stop the runtime services in reverse start order (spec §7)."""
        tick_task, tick_stop = self._watch_tick_task, self._watch_tick_stop
        self._watch_tick_task = None
        self._watch_tick_stop = None
        if tick_task is not None and tick_stop is not None:
            tick_stop.set()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass
        if self._watch_engine is not None:
            await self._watch_engine.stop()
        if self._runtime_registry is not None:
            await self._runtime_registry.stop_all()
