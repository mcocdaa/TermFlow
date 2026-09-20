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
from typing import cast
from uuid import UUID

from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.api.agent_admin import router as agent_admin_router
from termflow_control_plane.api.agent_approvals import router as agent_approvals_router
from termflow_control_plane.api.agent_capabilities import get_agent_capabilities
from termflow_control_plane.api.agent_cleanup import router as agent_cleanup_router
from termflow_control_plane.api.agent_conversations import router as agent_conversations_router
from termflow_control_plane.api.agent_stream import router as agent_stream_router
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentCleanupJob,
)
from termflow_control_plane.persistence.models import (
    BackendConversationRef as BackendConversationRefRow,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.inbox import (
    InboxDeliveryStateMachine,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import ApprovalPolicy
from termflow_control_plane.plugins.agent_broker.agent.runs import AgentRunStateMachine
from termflow_control_plane.plugins.agent_broker.agent.runtime_controller import (
    AgentRuntimeController,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    AgentRuntimeRegistry,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import binding_is_closed
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendConversationRef,
    BackendOperationResult,
    BackendOutcome,
    ProviderRef,
)
from termflow_control_plane.plugins.agent_broker.agent.watches import (
    FiredTrigger,
    WatchEngine,
)
from termflow_control_plane.plugins.agent_broker.startup import (
    AgentStartupCoordinator,
    AgentStartupState,
    Hook,
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

#: Agent deletion tombstone target kinds (plan §15/§17).  ``term`` and
#: ``installation`` jobs are created by the core Term/Computer delete paths;
#: the conversation/binding/profile kinds are created by the Agent Broker's
#: own delete endpoints so a backend (OpenCode) session is never silently
#: orphaned.
CLEANUP_TARGET_KINDS = ("term", "installation", "conversation", "binding", "profile")

#: Default interval between cleanup-retry sweeps while the plugin is running
#: (the loop also sweeps immediately at startup and on every shutdown-safe
#: stop event).
_CLEANUP_TICK_SECONDS = 30.0


class BackendRuntimeUnavailableError(RuntimeError):
    """A backend session exists but no runtime pipeline is mapped, so the
    backend deletion cannot be proven; the rows must stay so the tombstone
    retry can attempt again (fail closed, plan §17)."""


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
    critical_failures: tuple[str, ...] = ()


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
        recovered = await (
            inbox_machine or InboxDeliveryStateMachine(repositories.agent_inbox)
        ).recover_stale(now=observed)
        for envelope in recovered:
            if envelope.delivery_state == "pending":
                report.inbox_recovered += 1
            elif envelope.delivery_state == "delivery_unknown":
                report.inbox_delivery_unknown += 1
            elif envelope.delivery_state in ("dispatched", "delivered"):
                # Reconciliation proves the outcome for both a never-started
                # claim (``dispatched``) and a started submission whose run
                # terminated (``delivered``, the persisted terminal state).
                report.inbox_reconciled += 1
    except Exception as exc:
        logger.exception("Agent inbox recovery failed: %s", exc)
        report.critical_failures += ("inbox_fencing",)

    # 2) Runs stuck mid-flight are fenced to unknown.
    run_state_machine = run_machine or AgentRunStateMachine(repositories.agent_runs)
    try:
        for run in await repositories.agent_runs.list_by_state("running"):
            try:
                await run_state_machine.mark_unknown(run.id)
                report.runs_marked_unknown += 1
            except Exception as exc:
                logger.exception("Agent run %s could not be marked unknown: %s", run.id, exc)
                report.critical_failures += ("run_fencing",)
    except Exception as exc:
        logger.exception("Agent run recovery scan failed: %s", exc)
        report.critical_failures += ("run_scan",)

    # 3) Pending cleanup tombstones are retried (or completed) in order.
    try:
        retried, completed = await run_cleanup_retry(
            repositories,
            handlers=cleanup_handlers or {},
            now=observed,
        )
        report.cleanup_jobs_retried = retried
        report.cleanup_jobs_completed = completed
    except Exception as exc:
        logger.exception("Agent cleanup job scan failed: %s", exc)

    # 4) Orphaned approvals: pending -> revoked, approved -> unknown.
    if approval_policy is not None:
        try:
            revoked, unknown = await approval_policy.recover_orphaned_approvals(now=observed)
            report.approvals_revoked = revoked
            report.approvals_marked_unknown = unknown
        except Exception as exc:
            logger.exception("Agent approval recovery failed: %s", exc)
            report.critical_failures += ("approval_fencing",)

    return report


async def run_cleanup_retry(
    repositories: RepositoryBundle,
    *,
    handlers: Mapping[str, CleanupJobHandler],
    now: datetime | None = None,
    retry_unhandled: bool = False,
) -> tuple[int, int]:
    """Retry pending cleanup tombstones through the registered handlers.

    Returns ``(retried, completed)``.  A job whose ``target_kind`` has no
    handler, or whose handler raises, records an attempt with a retry
    backoff and stays visible as ``deletion_pending`` (plan §15/§17).
    ``retry_unhandled`` additionally picks up jobs whose previous attempt
    failed with "no cleanup handler registered" even while their backoff has
    not elapsed - the plugin's first sweep after startup uses it because the
    composition root's pre-startup recovery ran without the handlers, which
    now exist.
    """
    observed = now or datetime.now(UTC)
    if retry_unhandled:
        jobs = await repositories.cleanup_jobs.list_pending_retry_now(now=observed)
    else:
        jobs = await repositories.cleanup_jobs.list_pending(now=observed)
    retried = 0
    completed = 0
    for job in jobs:
        if not await repositories.cleanup_jobs.has_actionable_receipts(job.id):
            # Every pending receipt needs deployment evidence through the
            # cleanup-helper API (provider retention, database backup,
            # runtime/container artifacts).  The job is waiting on the
            # operator, not on B work, so leave its recorded reason intact
            # instead of logging a fake "no cleanup handler" failure and
            # retrying the target handler every tick.
            continue
        handler = handlers.get(job.target_kind)
        if handler is None:
            # The pre-startup recovery runs before the plugin registers its
            # handlers.  Record the retry marker its first sweep looks for,
            # but this hand-off is designed, not a failure: log without a
            # traceback.
            logger.info(
                "Agent cleanup job %s (%s %s) awaits handler registration",
                job.id,
                job.target_kind,
                job.target_ref,
            )
            await repositories.cleanup_jobs.record_attempt(
                job.id,
                next_attempt_at=observed + _AGENT_CLEANUP_RETRY_BACKOFF,
                last_error=(f"no cleanup handler registered for target_kind={job.target_kind!r}"),
            )
            retried += 1
            continue
        try:
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
            retried += 1
        else:
            # A handler is B-owned evidence for every pending receipt in the
            # legacy target-level tombstone path.  Mark those receipts before
            # asking the repository to advance the aggregate; ``complete``
            # now refuses a zero/pending-receipt false success.
            await repositories.cleanup_jobs.confirm_pending_internal(
                job.id,
                now=observed,
            )
            if await repositories.cleanup_jobs.complete(job.id, now=observed) is not None:
                completed += 1
            else:
                retried += 1
    return retried, completed


async def cleanup_retry_loop(
    repositories: RepositoryBundle,
    handlers: Mapping[str, CleanupJobHandler],
    *,
    tick_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Periodic cleanup-tombstone retry task started by the plugin ``startup``.

    The first sweep runs immediately and re-attempts tombstones whose
    previous attempt predates the handler registration ("no cleanup handler
    registered"); afterwards the loop waits ``tick_seconds`` between sweeps
    and exits promptly when ``stop`` is set.  A failed sweep never kills the
    task: the error is logged and the next tick retries.
    """
    first_sweep = True
    while True:
        try:
            retried, completed = await run_cleanup_retry(
                repositories,
                handlers=handlers,
                retry_unhandled=first_sweep,
            )
            first_sweep = False
            if retried or completed:
                logger.info(
                    "Agent cleanup retry: %s tombstones completed, %s retried",
                    completed,
                    retried,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent cleanup retry sweep failed; retrying next tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except TimeoutError:
            pass
        if stop.is_set():
            return


def backend_ref_from_row(row: BackendConversationRefRow) -> BackendConversationRef:
    """Project a persisted backend ref row onto the neutral turns model."""
    return BackendConversationRef(
        backend_kind=row.backend_kind,
        backend_version=cast(str, row.backend_version),
        runtime_id=row.runtime_id,
        binding_capability_epoch=row.binding_capability_epoch,
        provider_ref=ProviderRef(row.provider_ref),
    )


async def record_cleanup_failure(
    job: AgentCleanupJob,
    repositories: RepositoryBundle,
    error: str,
    *,
    now: datetime | None = None,
) -> AgentCleanupJob | None:
    """Record a failed cleanup attempt with the retry backoff (plan §17).

    The tombstone stays ``pending`` and remains visible as
    ``deletion_pending``; the next recovery/retry sweep re-attempts it.
    """
    observed = now or datetime.now(UTC)
    return await repositories.cleanup_jobs.record_attempt(
        job.id,
        next_attempt_at=observed + _AGENT_CLEANUP_RETRY_BACKOFF,
        last_error=error,
    )


async def cancel_conversation_runs(
    repositories: RepositoryBundle,
    conversation_id: UUID,
) -> int:
    """Cancel a conversation's queued/running runs before deletion (plan §17).

    The durable fence is authoritative: every queued/running row is CAS'd to
    ``cancelled`` so a deletion can never race an in-flight dispatch.  The
    rows themselves are removed by the cascade when the conversation row is
    deleted; this transition is the durable cancellation proof that survives
    even when the row deletion is deferred to the tombstone retry.  Runs are
    paged so the sweep is complete beyond the repository's default page.
    """
    cancelled = 0
    offset = 0
    while True:
        page = await repositories.agent_runs.list_for_conversation(
            conversation_id, limit=200, offset=offset
        )
        for run in page:
            if run.run_state in ("queued", "running"):
                updated = await repositories.agent_runs.set_state(
                    run.id, "cancelled", expected_state=run.run_state
                )
                if updated is not None:
                    cancelled += 1
        if len(page) < 200:
            break
        offset += 200
    return cancelled


async def delete_backend_conversation(
    repositories: RepositoryBundle,
    registry: AgentRuntimeRegistry | None,
    conversation_id: UUID,
) -> BackendOperationResult | None:
    """Prove the backend session for ``conversation_id`` is deleted.

    Returns ``None`` when no backend session was ever created (nothing to
    delete).  Raises :class:`BackendRuntimeUnavailableError` when a session
    exists but no runtime pipeline is mapped for the conversation's binding
    (including when no runtime registry is wired at all): the rows must stay
    so the tombstone retry can attempt again.  Otherwise returns the
    adapter's outcome (``CONFIRMED`` or not).
    """
    ref_row = await repositories.agent_backend_conversations.get_by_conversation(conversation_id)
    if ref_row is None:
        return None
    conversation = await repositories.agent_conversations.get_by_id(conversation_id)
    pipeline = (
        registry.pipeline_for(conversation.binding_id)
        if conversation is not None and registry is not None
        else None
    )
    if pipeline is None:
        raise BackendRuntimeUnavailableError(
            f"conversation {conversation_id} has a backend session but no mapped runtime pipeline"
        )
    return await pipeline.adapter.delete_conversation(backend_ref_from_row(ref_row))


async def delete_binding_backend_sessions(
    repositories: RepositoryBundle,
    registry: AgentRuntimeRegistry | None,
    binding_id: UUID,
) -> list[str]:
    """Delete every backend session of a binding's conversations.

    Returns the unconfirmed failure messages (empty means everything is
    confirmed or there is nothing to delete).  Raises
    :class:`BackendRuntimeUnavailableError` when sessions exist but no
    runtime pipeline is mapped (including when no runtime registry is wired
    at all).
    """
    refs: list[BackendConversationRef] = []
    offset = 0
    while True:
        page = await repositories.agent_conversations.list_for_binding(
            binding_id, limit=200, offset=offset
        )
        for conversation in page:
            ref_row = await repositories.agent_backend_conversations.get_by_conversation(
                conversation.id
            )
            if ref_row is not None:
                refs.append(backend_ref_from_row(ref_row))
        if len(page) < 200:
            break
        offset += 200
    if not refs:
        return []
    pipeline = registry.pipeline_for(binding_id) if registry is not None else None
    if pipeline is None:
        raise BackendRuntimeUnavailableError(
            f"binding {binding_id} has backend sessions but no mapped runtime pipeline"
        )
    failures: list[str] = []
    for ref in refs:
        result = await pipeline.adapter.delete_conversation(ref)
        if result.outcome is not BackendOutcome.CONFIRMED:
            failures.append(
                result.message or f"backend session {ref.provider_ref} deletion unconfirmed"
            )
    return failures


async def _sweep_binding_agent_rows(repositories: RepositoryBundle, binding_id: UUID) -> None:
    """Defensive sweep for the tombstone retry path: cancel a binding's
    watches (plan §17 cancels them on Term deletion; the retry path repeats
    the cancellation so a crash mid-delete never leaves a live watch)."""
    for watch in await repositories.watches.list_for_binding(binding_id):
        await repositories.watches.cancel(watch.id)


async def _conversation_cleanup_handler(
    job: AgentCleanupJob,
    repositories: RepositoryBundle,
    registry: AgentRuntimeRegistry,
) -> None:
    """Complete a conversation deletion tombstone (plan §15/§17).

    The B-side rows are only deleted after the backend session deletion is
    proven (or after no backend session ever existed), so an OpenCode
    session is never silently orphaned.  Missing rows mean an earlier
    attempt already finished the durable work.
    """
    conversation_id = UUID(job.target_ref)
    result = await delete_backend_conversation(repositories, registry, conversation_id)
    if result is not None and result.outcome is not BackendOutcome.CONFIRMED:
        raise RuntimeError(result.message or "backend deletion unconfirmed")
    await repositories.agent_conversations.delete(conversation_id)


async def _binding_cleanup_handler(
    job: AgentCleanupJob,
    repositories: RepositoryBundle,
    registry: AgentRuntimeRegistry,
) -> None:
    """Complete a binding deletion tombstone (plan §15/§17).

    The binding row is only deleted after every backend session of its
    conversations is proven deleted; a missing binding row means an earlier
    attempt already finished the durable work.
    """
    binding_id = UUID(job.target_ref)
    binding = await repositories.agent_bindings.get_by_id(binding_id)
    if binding is None:
        return
    failures = await delete_binding_backend_sessions(repositories, registry, binding_id)
    if failures:
        raise RuntimeError("; ".join(failures))
    await repositories.agent_bindings.delete(binding_id)


async def _profile_cleanup_handler(
    job: AgentCleanupJob,
    repositories: RepositoryBundle,
    registry: AgentRuntimeRegistry,
) -> None:
    """Complete a profile deletion tombstone (plan §15/§17).

    Every binding of the profile must prove its backend sessions deleted
    before the profile row is removed (cascading the bindings); a missing
    profile row means an earlier attempt already finished the durable work.
    """
    profile_id = UUID(job.target_ref)
    if await repositories.agent_profiles.get_by_id(profile_id) is None:
        return
    failures: list[str] = []
    for binding in await repositories.agent_bindings.list_for_profile(profile_id):
        try:
            failures.extend(
                await delete_binding_backend_sessions(repositories, registry, binding.id)
            )
        except BackendRuntimeUnavailableError as exc:
            failures.append(str(exc))
    if failures:
        raise RuntimeError("; ".join(failures))
    await repositories.agent_profiles.delete(profile_id)


async def _term_cleanup_handler(
    job: AgentCleanupJob,
    repositories: RepositoryBundle,
    registry: AgentRuntimeRegistry,
) -> None:
    """Complete a Term deletion tombstone (created by the core delete path).

    Every surviving Agent binding of the Term is swept (watch cancellation)
    and deleted; normally the cascade already removed them, so the handler
    mostly verifies emptiness.  Backend session/volume/log/provider cleanup
    for the deleted Term cannot be enumerated from B rows after the cascade
    and stays owned by the deployment layer (plan §16).
    """
    del registry  # Term cleanup needs no runtime registry: rows are cascaded.
    term_id = UUID(job.target_ref)
    for binding in await repositories.agent_bindings.list_for_term(term_id):
        await _sweep_binding_agent_rows(repositories, binding.id)
        await repositories.agent_bindings.delete(binding.id)


async def _installation_cleanup_handler(
    job: AgentCleanupJob,
    repositories: RepositoryBundle,
    registry: AgentRuntimeRegistry,
) -> None:
    """Complete an installation deletion tombstone (core delete path).

    Sweeps the Agent rows of every surviving Term of the installation; the
    core path deletes the Terms themselves, and normally the cascade already
    removed the Agent rows.  Deployment-level volume/log/provider cleanup
    stays owned by the deployment layer (plan §16).
    """
    del registry  # Installation cleanup needs no runtime registry: rows are cascaded.
    installation_id = UUID(job.target_ref)
    for instance in await repositories.instances.list_for_installation(installation_id):
        for binding in await repositories.agent_bindings.list_for_term(instance.id):
            await _sweep_binding_agent_rows(repositories, binding.id)
            await repositories.agent_bindings.delete(binding.id)


def build_agent_cleanup_handlers(
    repositories: RepositoryBundle,
    registry: AgentRuntimeRegistry,
) -> Mapping[str, CleanupJobHandler]:
    """The registered deletion-cleanup handlers for every tombstone kind.

    Each handler performs one target_kind's durable deletion work and raises
    on failure so :func:`run_cleanup_retry` records an attempt with a
    backoff.  Registered through the plugin's lifecycle hooks so the
    tombstones created by the delete endpoints (and by the core
    Term/Computer delete paths) can always complete.
    """

    def _bind(
        work: Callable[
            [AgentCleanupJob, RepositoryBundle, AgentRuntimeRegistry],
            Awaitable[None],
        ],
    ) -> CleanupJobHandler:
        async def _run(job: AgentCleanupJob) -> None:
            await work(job, repositories, registry)

        return _run

    return {
        "term": _bind(_term_cleanup_handler),
        "installation": _bind(_installation_cleanup_handler),
        "conversation": _bind(_conversation_cleanup_handler),
        "binding": _bind(_binding_cleanup_handler),
        "profile": _bind(_profile_cleanup_handler),
    }


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


async def deliver_fired_trigger(
    watch_engine: WatchEngine,
    registry: AgentRuntimeRegistry,
    trigger: FiredTrigger,
    *,
    observed_at: datetime,
) -> None:
    """Deliver one fired trigger to its binding's pipeline (M4.5 spec §3b).

    ``fire()`` already committed the inbox row and delivery receipt inside
    the trigger transaction; this bridge renders the typed
    ``WatchTriggeredInput`` and registers it in-process so the binding's
    dispatcher can render the turn.  A trigger whose binding has no
    pipeline is logged and left in the inbox (the dispatcher keeps such an
    item visible pending until a payload can be registered).
    """
    input = watch_engine.build_input(trigger, observed_at=observed_at)
    if input is None:
        logger.warning(
            "Agent watch trigger %s has no typed input; nothing to deliver",
            trigger.delivery.delivery_key,
        )
        return
    pipeline = registry.pipeline_for(trigger.watch.binding_id)
    if pipeline is None:
        logger.warning(
            "Agent watch trigger %s: binding %s has no pipeline; the inbox item stays pending",
            trigger.delivery.delivery_key,
            trigger.watch.binding_id,
        )
        return
    await pipeline.submit_watch_input(input)


def build_watch_trigger_sink(
    watch_engine: WatchEngine,
    registry: AgentRuntimeRegistry,
    *,
    now: Callable[[], datetime] | None = None,
) -> Callable[[FiredTrigger], Awaitable[None]]:
    """Build the watch engine's trigger sink port (M4.5 spec §3b wiring).

    Returns the ``on_fired`` callback the composition root attaches to the
    :class:`WatchEngine` so every fired trigger - live output, deadline,
    topology, or gap snapshot - is delivered to its binding's pipeline.
    """
    clock = now or (lambda: datetime.now(UTC))

    async def on_fired(trigger: FiredTrigger) -> None:
        await deliver_fired_trigger(watch_engine, registry, trigger, observed_at=clock())

    return on_fired


async def run_watch_deadline_tick(
    watch_engine: WatchEngine,
    *,
    now: datetime | None = None,
) -> list[FiredTrigger]:
    """One deadline sweep (M4.5 spec §3b).

    ``check_deadlines`` performs the atomic trigger transaction and the
    engine's trigger sink (``on_fired``) delivers every fired trigger to
    its binding's pipeline; the tick only runs the sweep.
    """
    observed_at = now or datetime.now(UTC)
    return await watch_engine.check_deadlines(observed_at)


async def watch_deadline_loop(
    watch_engine: WatchEngine,
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
            await run_watch_deadline_tick(watch_engine)
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


async def runtime_health_loop(
    controller: AgentRuntimeController,
    *,
    tick_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Periodically reconcile runtime health and authority drift.

    The controller's health cycle also revisits transiently unavailable
    mappings after OpenCode/MCP reconnects.  It owns the fail-closed ordering
    (unpublish, fence, then persist observed state).  This task remains a thin
    retry loop so one unreachable runtime cannot kill the Agent plugin or the
    core B process.
    """
    while True:
        try:
            await controller.run_health_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent runtime health sweep failed; retrying next tick")
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

    Deletion cleanup (plan §15/§17): ``register_services`` registers the
    durable deletion-cleanup handlers for every tombstone target kind, and
    ``startup`` spawns the periodic cleanup-retry loop that drives them.
    The composition root's pre-startup ``run_agent_recovery`` runs without
    these handlers, so the loop's first sweep re-attempts any tombstone
    whose previous attempt failed with "no cleanup handler registered".
    """

    id = "agent_broker"
    version = "0.2.0-dev.0"
    requires_core_api = "0.2.0"

    def __init__(self, *, cleanup_tick_seconds: float = _CLEANUP_TICK_SECONDS) -> None:
        self._watch_engine: WatchEngine | None = None
        self._runtime_registry: AgentRuntimeRegistry | None = None
        self._runtime_controller: AgentRuntimeController | None = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None
        self._watch_tick_seconds: float = 1.0
        self._watch_tick_task: asyncio.Task[None] | None = None
        self._watch_tick_stop: asyncio.Event | None = None
        self._runtime_health_tick_seconds: float = 5.0
        self._runtime_health_tick_task: asyncio.Task[None] | None = None
        self._runtime_health_tick_stop: asyncio.Event | None = None
        self._runtime_degraded_reason: str | None = None
        self.startup_coordinator: AgentStartupCoordinator | None = None
        self._startup_fencing: Hook | None = None
        self._repositories: RepositoryBundle | None = None
        self._cleanup_handlers: dict[str, CleanupJobHandler] = {}
        self._cleanup_tick_seconds: float = cleanup_tick_seconds
        self._cleanup_tick_task: asyncio.Task[None] | None = None
        self._cleanup_tick_stop: asyncio.Event | None = None

    def bind_runtime_services(
        self,
        *,
        watch_engine: WatchEngine,
        registry: AgentRuntimeRegistry,
        controller: AgentRuntimeController | None = None,
        sessions: async_sessionmaker[AsyncSession],
        watch_tick_seconds: float,
        runtime_health_tick_seconds: float = 5.0,
    ) -> None:
        """Receive the composition-root-built runtime services (M4.5 wiring).

        Called by app.py between restart recovery and feature startup: the
        services are constructed there because they need the repositories,
        session factory, and hubs that only exist inside the lifespan.
        """
        self._watch_engine = watch_engine
        self._runtime_registry = registry
        self._runtime_controller = controller
        self._sessions = sessions
        self._watch_tick_seconds = watch_tick_seconds
        if runtime_health_tick_seconds <= 0:
            raise ValueError("runtime_health_tick_seconds must be positive")
        self._runtime_health_tick_seconds = runtime_health_tick_seconds
        # The cleanup handlers need the persistence bundle; it is rebuilt
        # from the same session factory the composition root used, so it
        # shares the same database (repositories are stateless wrappers).
        self._repositories = RepositoryBundle(sessions)

    @property
    def cleanup_handlers(self) -> Mapping[str, CleanupJobHandler]:
        """The deletion-cleanup handlers registered for this plugin instance."""
        return dict(self._cleanup_handlers)

    def register_services(self, context: BFeatureContext) -> None:
        """Register the durable deletion-cleanup handlers (plan §15/§17).

        Runtime services themselves are attached by the composition root
        (see :meth:`bind_runtime_services`); this hook registers the
        tombstone handlers so every pending cleanup job - including the
        ``term``/``installation`` jobs the core delete paths create - has a
        handler that can complete it.
        """
        if self._repositories is not None and self._runtime_registry is not None:
            self._cleanup_handlers = dict(
                build_agent_cleanup_handlers(self._repositories, self._runtime_registry)
            )

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
            agent_cleanup_router,
            agent_conversations_router,
            agent_stream_router,
            agent_approvals_router,
        ):
            for route in feature_router.routes:
                if not isinstance(route, APIRoute):
                    raise TypeError(f"Agent Broker router contains unsupported route {route!r}")
                if route.methods is None:
                    raise TypeError(f"Agent Broker route {route.path!r} has no HTTP method")
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
        manifest.add_revision(MigrationRevision(id="0006", owner="agent_broker", dependencies=()))
        manifest.add_revision(
            MigrationRevision(id="0007", owner="agent_broker", dependencies=("0006",))
        )
        manifest.add_revision(
            MigrationRevision(id="0008", owner="agent_broker", dependencies=("0007",))
        )
        manifest.add_revision(
            MigrationRevision(id="0009", owner="agent_broker", dependencies=("0008",))
        )
        manifest.add_revision(
            MigrationRevision(id="0010", owner="agent_broker", dependencies=("0009",))
        )
        manifest.add_revision(
            MigrationRevision(id="0011", owner="agent_broker", dependencies=("0010",))
        )
        manifest.add_revision(
            MigrationRevision(id="0012", owner="agent_broker", dependencies=("0011",))
        )
        manifest.add_revision(
            MigrationRevision(id="0013", owner="agent_broker", dependencies=("0012",))
        )
        manifest.add_revision(
            MigrationRevision(id="0014", owner="agent_broker", dependencies=("0013",))
        )

    def bind_startup_fencing(self, fencing: Hook) -> None:
        self._startup_fencing = fencing

    async def startup(self, context: BFeatureContext) -> None:
        if (
            self._cleanup_handlers
            and self._repositories is not None
            and self._cleanup_tick_task is None
        ):
            self._cleanup_tick_stop = asyncio.Event()
            self._cleanup_tick_task = asyncio.create_task(
                cleanup_retry_loop(
                    self._repositories,
                    self._cleanup_handlers,
                    tick_seconds=self._cleanup_tick_seconds,
                    stop=self._cleanup_tick_stop,
                ),
                name="agent-cleanup-retry",
            )
        if self.startup_coordinator is None:

            async def fence() -> object:
                if self._startup_fencing is not None:
                    return await self._startup_fencing()
                if self._repositories is not None:
                    return await run_agent_recovery(self._repositories)
                return None

            async def activate() -> object:
                await self._activate_runtime_services(context)
                return None

            self.startup_coordinator = AgentStartupCoordinator(fence, backend=activate)
        await self.startup_coordinator.recover()
        result = await self.startup_coordinator.activate()
        self._runtime_degraded_reason = result.reason_code
        if result.state == AgentStartupState.DEGRADED and self._runtime_registry is not None:
            await self._runtime_registry.stop_all()

    async def _activate_runtime_services(self, context: BFeatureContext) -> None:
        """Start the attached runtime services in the spec §3a/§7 order.

        Restart recovery already fenced stale inbox claims and stuck runs
        (app.py runs it before feature startup), so the watch engine loads
        its deadline heap and subscribes first, then per-binding pipelines
        activate, then the deadline tick task starts - a fired trigger can
        never arrive before a pipeline can receive it.  The cleanup-retry
        loop starts last: its first sweep re-attempts tombstones that the
        pre-startup recovery could not complete because the handlers did not
        exist yet.
        """
        if self._watch_engine is None or self._runtime_registry is None:
            return
        assert self._sessions is not None
        self._runtime_degraded_reason = None
        if self._runtime_controller is not None:
            # Runtime authority is the startup gate: it performs the same
            # desired/observed CAS and fail-closed checks as an API-triggered
            # reconcile.  Individual offline runtimes become ``not_ready``;
            # an unexpected controller/database failure leaves the Agent
            # plugin degraded and prevents watch/pipeline activation.
            try:
                reconcile_results = await self._runtime_controller.reconcile_all()
                for result in reconcile_results:
                    if result.readiness != "ready":
                        # Keep the registry's diagnostic surface compatible
                        # with the pre-controller startup path.  The public
                        # API exposes the bounded reason_code from observed
                        # state; this value is internal/logging only.
                        self._runtime_registry.unavailable_bindings[result.binding_id] = (
                            f"attestation failed: {result.reason_code or result.readiness}"
                        )
            except Exception:
                self._runtime_degraded_reason = "recovery_failed"
                logger.exception("Agent runtime startup reconciliation failed")
                try:
                    await self._runtime_registry.stop_all()
                except Exception:
                    logger.exception("Agent runtime degraded cleanup failed")
                raise
        await self._watch_engine.rebuild()
        await self._watch_engine.start()
        if self._runtime_controller is None:
            # Compatibility path for isolated plugin tests/compositions that
            # have not supplied the controller yet.  Production app wiring
            # always takes the controller branch above.
            await self._runtime_registry.start_all(await _list_active_bindings(self._sessions))
        elif self._runtime_controller is not None:
            self._runtime_health_tick_stop = asyncio.Event()
            self._runtime_health_tick_task = asyncio.create_task(
                runtime_health_loop(
                    self._runtime_controller,
                    tick_seconds=self._runtime_health_tick_seconds,
                    stop=self._runtime_health_tick_stop,
                ),
                name="agent-runtime-health",
            )
        self._watch_tick_stop = asyncio.Event()
        self._watch_tick_task = asyncio.create_task(
            watch_deadline_loop(
                self._watch_engine,
                tick_seconds=self._watch_tick_seconds,
                stop=self._watch_tick_stop,
            ),
            name="agent-watch-deadlines",
        )
        if (
            self._cleanup_handlers
            and self._repositories is not None
            and self._cleanup_tick_task is None
        ):
            self._cleanup_tick_stop = asyncio.Event()
            self._cleanup_tick_task = asyncio.create_task(
                cleanup_retry_loop(
                    self._repositories,
                    self._cleanup_handlers,
                    tick_seconds=self._cleanup_tick_seconds,
                    stop=self._cleanup_tick_stop,
                ),
                name="agent-cleanup-retry",
            )

    async def shutdown(self) -> None:
        """Stop the runtime services in reverse start order (spec §7)."""
        cleanup_task, cleanup_stop = self._cleanup_tick_task, self._cleanup_tick_stop
        self._cleanup_tick_task = None
        self._cleanup_tick_stop = None
        if cleanup_task is not None and cleanup_stop is not None:
            cleanup_stop.set()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        health_task, health_stop = (
            self._runtime_health_tick_task,
            self._runtime_health_tick_stop,
        )
        self._runtime_health_tick_task = None
        self._runtime_health_tick_stop = None
        if health_task is not None and health_stop is not None:
            health_stop.set()
            try:
                await health_task
            except asyncio.CancelledError:
                pass
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
