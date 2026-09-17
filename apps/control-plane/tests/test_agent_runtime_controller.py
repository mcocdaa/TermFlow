"""Runtime-controller orchestration tests for v0.2.0 Task 8."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentBinding, AgentProfile
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogEntry,
    canonicalize_profile_config,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_controller import (
    AgentRuntimeController,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    AgentRuntimeRegistry,
    RuntimeCandidate,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    SupervisorConnector,
)
from termflow_control_plane.plugins.protocol import RuntimeHealth, RuntimeRef, RuntimeStatus

CATALOG = ProviderCatalog(
    (
        ProviderCatalogEntry(
            provider_id="deepseek",
            model_ids=frozenset({"deepseek-reasoner", "deepseek-v4-flash"}),
            endpoint_origin="https://api.deepseek.com",
            region="global",
            retention_terms="no more than 30 days",
            retention_version="2026-09-01",
            no_training=True,
            credential_source="DEEPSEEK_API_KEY",
            policy_version="2026-09-01",
        ),
    )
)
PROFILE_CONFIG = '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}'


@dataclass(slots=True)
class _FakePipeline:
    events: list[str]
    started: bool = False
    stop_calls: int = 0
    fail_stop: bool = False

    async def stop(self) -> None:
        self.events.append("pipeline_stop")
        self.stop_calls += 1
        self.started = False
        if self.fail_stop:
            raise RuntimeError("pipeline-stop-private-detail")


@dataclass(slots=True)
class _FakeCandidate:
    binding: AgentBinding
    pipeline: _FakePipeline


class _FakeRegistry:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.live: dict[UUID, _FakePipeline] = {}
        self.candidates: dict[UUID, _FakeCandidate] = {}
        self.built: list[_FakeCandidate] = []
        self.start_calls = 0
        self.publish_calls = 0
        self.on_start: Callable[[AgentBinding], Awaitable[None]] | None = None

    async def build_candidate(self, binding: AgentBinding) -> RuntimeCandidate:
        self.events.append("build_candidate")
        candidate = _FakeCandidate(binding=binding, pipeline=_FakePipeline(self.events))
        self.candidates[binding.id] = candidate
        self.built.append(candidate)
        await asyncio.sleep(0)
        return cast(RuntimeCandidate, candidate)

    async def start_candidate(self, candidate: RuntimeCandidate) -> None:
        typed = cast(_FakeCandidate, candidate)
        self.events.append("start_candidate")
        self.start_calls += 1
        if self.on_start is not None:
            await self.on_start(typed.binding)
        typed.pipeline.started = True
        await asyncio.sleep(0)

    async def publish_started(
        self,
        binding_id: UUID,
        candidate: RuntimeCandidate,
    ) -> _FakePipeline:
        typed = cast(_FakeCandidate, candidate)
        self.events.append("publish_started")
        self.publish_calls += 1
        assert typed.pipeline.started
        self.candidates.pop(binding_id, None)
        self.live[binding_id] = typed.pipeline
        return typed.pipeline

    async def discard_candidate(self, candidate: RuntimeCandidate) -> None:
        typed = cast(_FakeCandidate, candidate)
        self.events.append("discard_candidate")
        self.candidates.pop(typed.binding.id, None)
        await typed.pipeline.stop()

    async def unpublish(self, binding_id: UUID) -> _FakePipeline | None:
        self.events.append("unpublish")
        return self.live.pop(binding_id, None)

    def pipeline_for(self, binding_id: UUID) -> _FakePipeline | None:
        return self.live.get(binding_id)

    def release_supervisor_binding(
        self, binding_id: UUID, runtime_ref: str | None
    ) -> None:
        self.events.append(f"release_supervisor:{binding_id}:{runtime_ref}")


class _FakeSupervisor:
    def __init__(self) -> None:
        self.status = RuntimeStatus.READY
        self.epoch = 1
        self.detail = ""
        self.health_calls: list[RuntimeRef] = []
        self.restart_calls: list[tuple[object, ...]] = []

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth:
        self.health_calls.append(runtime_ref)
        return RuntimeHealth(
            status=self.status,
            epoch=self.epoch,
            observed_at=datetime.now(UTC),
            detail=self.detail,
        )

    async def restart(self, *args: object) -> RuntimeHealth:
        self.restart_calls.append(args)
        raise AssertionError("runtime reconciliation must not restart B or OpenCode")


@dataclass(frozen=True, slots=True)
class _Context:
    repositories: RepositoryBundle
    sessions: async_sessionmaker[AsyncSession]
    registry: _FakeRegistry
    supervisor: _FakeSupervisor
    events: list[str]


@pytest_asyncio.fixture
async def context(tmp_path) -> AsyncIterator[_Context]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'controller.db'}")
    await database.initialize()
    events: list[str] = []
    try:
        yield _Context(
            repositories=RepositoryBundle(database.session_factory),
            sessions=database.session_factory,
            registry=_FakeRegistry(events),
            supervisor=_FakeSupervisor(),
            events=events,
        )
    finally:
        await database.dispose()


async def _seed_binding(
    context: _Context,
    name: str,
    *,
    runtime_ref: str = "runtime-1",
    runtime_epoch: int = 1,
) -> AgentBinding:
    repositories = context.repositories
    installation = await repositories.installations.create(digest_secret(f"installation-{name}"))
    term = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        f"term-{name}",
        digest_secret(f"term-{name}"),
    )
    profile = await repositories.agent_profiles.create(
        display_name=f"profile-{name}",
        backend_kind="opencode",
        config=PROFILE_CONFIG,
    )
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="enabled",
        runtime_ref=runtime_ref,
        runtime_epoch=runtime_epoch,
        capability_ref=f"capability-{name}",
    )
    # Calculate through the same canonical Profile contract as production.
    profile_config, _ = canonicalize_profile_config(PROFILE_CONFIG)
    fingerprint = CATALOG.disclosure_fingerprint(profile_config)
    entry = CATALOG.resolve(profile_config)
    await repositories.agent_provider_disclosures.create(
        binding_id=binding.id,
        disclosure_fingerprint=fingerprint,
        provider_id=entry.provider_id,
        model_id=profile_config.model_id,
        endpoint_origin=entry.endpoint_origin,
        region=entry.region,
        retention_terms=entry.retention_terms,
        retention_version=entry.retention_version,
        no_training=entry.no_training,
        policy_version=entry.policy_version,
        accepted_at=datetime.now(UTC),
        accepted_auth_epoch=1,
        actor_kind="admin",
        actor_ref="test",
    )
    await repositories.pane_policies.set_policy(
        binding_id=binding.id,
        pane_id="%0",
        allowed=True,
    )
    return binding


def _controller(context: _Context, **kwargs: Any) -> AgentRuntimeController:
    return AgentRuntimeController(
        repositories=context.repositories,
        registry=cast(AgentRuntimeRegistry, context.registry),
        supervisor=cast(SupervisorConnector, context.supervisor),
        provider_catalog=CATALOG,
        **kwargs,
    )


async def test_activate_without_b_restart_publishes_ready_pipeline(
    context: _Context,
) -> None:
    binding = await _seed_binding(context, "activate")
    controller = _controller(context)

    result = await controller.reconcile(binding.id)

    assert result.readiness == "ready"
    assert result.reason_code is None
    assert result.applied_revision == 1
    assert context.registry.pipeline_for(binding.id) is not None
    assert context.registry.publish_calls == 1
    assert context.supervisor.restart_calls == []


async def test_stale_revision_never_publishes_candidate(context: _Context) -> None:
    binding = await _seed_binding(context, "stale")

    async def advance_revision(candidate_binding: AgentBinding) -> None:
        updated = await context.repositories.agent_bindings.update_desired_runtime(
            candidate_binding.id,
            candidate_binding.runtime_ref or "",
            candidate_binding.capability_ref or "",
            candidate_binding.config_revision,
            False,
        )
        assert updated is not None and updated.config_revision == 2

    context.registry.on_start = advance_revision
    controller = _controller(context)

    result = await controller.reconcile(binding.id)

    assert result.readiness == "not_ready"
    assert result.reason_code == "runtime_assignment_conflict"
    assert context.registry.pipeline_for(binding.id) is None
    assert context.registry.publish_calls == 0
    assert len(context.registry.built) == 1
    assert context.registry.built[0].pipeline.stop_calls == 1
    assert context.events.index("start_candidate") < context.events.index("discard_candidate")


async def test_stale_profile_fingerprint_never_publishes_candidate(
    context: _Context,
) -> None:
    binding = await _seed_binding(context, "stale-profile")

    async def replace_profile(candidate_binding: AgentBinding) -> None:
        async with context.sessions() as session:
            await session.execute(
                update(AgentProfile)
                .where(AgentProfile.id == candidate_binding.profile_id)
                .values(config=('{"model_id":"deepseek-reasoner","provider_id":"deepseek"}'))
            )
            await session.commit()

    context.registry.on_start = replace_profile
    controller = _controller(context)

    result = await controller.reconcile(binding.id)

    assert result.readiness == "blocked"
    assert result.reason_code == "binding_disclosure_stale"
    assert context.registry.pipeline_for(binding.id) is None
    assert context.registry.publish_calls == 0
    assert context.registry.built[0].pipeline.stop_calls == 1


async def test_health_drift_unmaps_and_persists_not_ready(context: _Context) -> None:
    binding = await _seed_binding(context, "health")
    token_hash = digest_secret("health-drift-same-epoch-token")
    await context.repositories.agent_tokens.create(
        binding_id=binding.id,
        token_hash=token_hash,
        scopes=("terminal.observe", "terminal.write"),
        expiry_epoch=int(datetime.now(UTC).timestamp()) + 3600,
        binding_epoch=binding.runtime_epoch or 1,
    )
    controller = _controller(context)
    assert (await controller.reconcile(binding.id)).readiness == "ready"
    context.events.clear()
    context.supervisor.status = RuntimeStatus.NOT_READY
    context.supervisor.detail = "mcp_not_connected"
    original_mark_unavailable = context.repositories.agent_runtime_bindings.mark_unavailable

    async def recording_mark_unavailable(
        binding_id: UUID,
        readiness: str,
        reason_code: str | None,
    ):
        context.events.append(f"persist_{readiness}")
        return await original_mark_unavailable(binding_id, readiness, reason_code)

    context.repositories.agent_runtime_bindings.mark_unavailable = (  # type: ignore[method-assign]
        recording_mark_unavailable
    )

    results = await controller.run_health_cycle()

    assert [result.binding_id for result in results] == [binding.id]
    assert results[0].readiness == "not_ready"
    assert results[0].reason_code == "mcp_not_connected"
    assert context.registry.pipeline_for(binding.id) is None
    assert context.events.index("unpublish") < context.events.index("persist_not_ready")
    persisted = await context.repositories.agent_runtime_bindings.get_by_binding(binding.id)
    assert persisted is not None
    assert persisted.readiness == "not_ready"
    assert persisted.reason_code == "mcp_not_connected"
    # A transient health failure is fenced by the unpublished routing map and
    # supervisor readiness gate.  Preserve the same-epoch bootstrap token so
    # the deployment can prove healthy and reconcile without an impossible
    # same-secret reissue against the unique token digest.
    assert await context.repositories.agent_tokens.get_by_hash(token_hash) is not None


async def test_health_cycle_recovers_transiently_unmapped_runtime(
    context: _Context,
) -> None:
    """A later health tick must republish after runtime/MCP reachability returns."""

    binding = await _seed_binding(context, "health-recovery")
    controller = _controller(context)
    assert (await controller.reconcile(binding.id)).readiness == "ready"

    context.supervisor.status = RuntimeStatus.NOT_READY
    context.supervisor.detail = "mcp_not_connected"
    drift = await controller.run_health_cycle()
    assert [result.reason_code for result in drift] == ["mcp_not_connected"]
    assert context.registry.pipeline_for(binding.id) is None

    context.supervisor.status = RuntimeStatus.READY
    context.supervisor.detail = ""
    recovered = await controller.run_health_cycle()

    assert [result.readiness for result in recovered] == ["ready"]
    assert context.registry.pipeline_for(binding.id) is not None
    assert context.registry.start_calls == 2
    assert context.registry.publish_calls == 2


async def test_health_drift_persists_not_ready_when_pipeline_stop_fails(
    context: _Context,
) -> None:
    binding = await _seed_binding(context, "health-stop-failure")
    controller = _controller(context)
    assert (await controller.reconcile(binding.id)).readiness == "ready"
    pipeline = context.registry.pipeline_for(binding.id)
    assert pipeline is not None
    pipeline.fail_stop = True
    context.supervisor.status = RuntimeStatus.NOT_READY

    results = await controller.run_health_cycle()

    assert len(results) == 1
    assert results[0].readiness == "not_ready"
    assert results[0].reason_code == "runtime_unreachable"
    assert context.registry.pipeline_for(binding.id) is None
    persisted = await context.repositories.agent_runtime_bindings.get_by_binding(binding.id)
    assert persisted is not None
    assert persisted.readiness == "not_ready"
    assert "private-detail" not in (persisted.reason_code or "")


async def test_reconcile_is_idempotent_and_serialized_per_binding(
    context: _Context,
) -> None:
    binding = await _seed_binding(context, "serialized")
    controller = _controller(context)

    first, second = await asyncio.gather(
        controller.reconcile(binding.id),
        controller.reconcile(binding.id),
    )

    assert first.readiness == second.readiness == "ready"
    assert context.registry.start_calls == 1
    assert context.registry.publish_calls == 1
    assert context.registry.pipeline_for(binding.id) is not None


async def test_fence_rotates_epoch_and_closes_authority(context: _Context) -> None:
    binding = await _seed_binding(context, "fence")
    controller = _controller(context)
    assert (await controller.reconcile(binding.id)).readiness == "ready"
    fenced: list[tuple[str, UUID]] = []

    async def revoke_approvals(binding_id: UUID) -> None:
        fenced.append(("approvals", binding_id))

    async def close_streams(binding_id: UUID) -> None:
        fenced.append(("streams", binding_id))

    controller = _controller(
        context,
        approval_fencer=revoke_approvals,
        stream_fencer=close_streams,
    )

    result = await controller.fence(binding.id, rotate_epoch=True)

    persisted = await context.repositories.agent_bindings.get_by_id(binding.id)
    assert persisted is not None
    assert persisted.runtime_epoch == 2
    assert persisted.config_revision == 2
    assert result.readiness == "not_ready"
    assert context.registry.pipeline_for(binding.id) is None
    assert fenced == [("approvals", binding.id), ("streams", binding.id)]


async def test_fence_releases_the_supervisor_runtime_for_closed_bindings(
    context: _Context,
) -> None:
    binding = await _seed_binding(context, "release")
    controller = _controller(context)
    assert (await controller.reconcile(binding.id)).readiness == "ready"

    await context.repositories.agent_bindings.set_status(
        binding.id, "revoked", advance_runtime_epoch=True
    )
    result = await controller.fence(binding.id, rotate_epoch=False)

    assert result.readiness == "disabled"
    assert f"release_supervisor:{binding.id}:runtime-1" in context.registry.events


async def test_reconcile_all_visits_every_persisted_binding(context: _Context) -> None:
    first = await _seed_binding(context, "all-first", runtime_ref="runtime-1")
    second = await _seed_binding(context, "all-second", runtime_ref="runtime-2")
    context.supervisor.epoch = 1
    controller = _controller(context)

    results = await controller.reconcile_all()

    assert {result.binding_id for result in results} == {first.id, second.id}
    assert all(result.readiness == "ready" for result in results)
    assert context.registry.start_calls == 2
