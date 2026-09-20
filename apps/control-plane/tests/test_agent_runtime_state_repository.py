from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentRuntimeBinding
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    repositories: RepositoryBundle
    sessions: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def repository_context(tmp_path) -> AsyncIterator[RepositoryContext]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime-state.db'}")
    await database.initialize()
    try:
        yield RepositoryContext(
            repositories=RepositoryBundle(database.session_factory),
            sessions=database.session_factory,
        )
    finally:
        await database.dispose()


async def _create_binding(
    context: RepositoryContext,
    name: str,
    *,
    status: str | None = "enabled",
    runtime_ref: str = "runtime-a",
    runtime_epoch: int = 7,
    capability_ref: str = "capability-a",
) -> UUID:
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
        config='{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
    )
    binding_kwargs: dict[str, object] = {
        "profile_id": profile.id,
        "term_id": term.id,
        "runtime_ref": runtime_ref,
        "runtime_epoch": runtime_epoch,
        "capability_ref": capability_ref,
    }
    # ``None`` intentionally means omit the argument so this helper can
    # exercise the repository's server-side creation default.
    if status is not None:
        binding_kwargs["status"] = status
    binding = await repositories.agent_bindings.create(**binding_kwargs)
    return binding.id


def _observed_snapshot(runtime: AgentRuntimeBinding) -> tuple[object, ...]:
    return (
        runtime.readiness,
        runtime.reason_code,
        runtime.observed_runtime_ref,
        runtime.observed_runtime_epoch,
        runtime.observed_capability_ref,
        runtime.applied_revision,
        runtime.config_fingerprint,
        runtime.last_health_at,
        runtime.transition_started_at,
        runtime.provider_readiness,
        runtime.provider_verified_revision,
        runtime.provider_last_checked_at,
        runtime.provider_reason_code,
        runtime.created_at,
        runtime.updated_at,
    )


async def test_update_desired_runtime_model_only_advances_revision_without_rotating_epoch(
    repository_context: RepositoryContext,
) -> None:
    repositories = repository_context.repositories
    binding_id = await _create_binding(repository_context, "model-only")

    updated = await repositories.agent_bindings.update_desired_runtime(
        binding_id,
        "runtime-a",
        "capability-a",
        1,
        False,
    )

    assert updated is not None
    assert updated.config_revision == 2
    assert updated.runtime_epoch == 7
    assert updated.runtime_ref == "runtime-a"
    assert updated.capability_ref == "capability-a"
    accepted_snapshot = (
        updated.runtime_ref,
        updated.capability_ref,
        updated.runtime_epoch,
        updated.config_revision,
        updated.updated_at,
    )

    stale = await repositories.agent_bindings.update_desired_runtime(
        binding_id,
        "runtime-a",
        "capability-a",
        1,
        False,
    )
    assert stale is None
    persisted = await repositories.agent_bindings.get_by_id(binding_id)
    assert persisted is not None
    assert (
        persisted.runtime_ref,
        persisted.capability_ref,
        persisted.runtime_epoch,
        persisted.config_revision,
        persisted.updated_at,
    ) == accepted_snapshot


async def test_update_desired_runtime_revoked_binding_is_an_atomic_noop(
    repository_context: RepositoryContext,
) -> None:
    repositories = repository_context.repositories
    binding_id = await _create_binding(
        repository_context,
        "revoked-runtime",
        status="revoked",
        runtime_ref="runtime-a",
        runtime_epoch=7,
        capability_ref="capability-a",
    )
    before = await repositories.agent_bindings.get_by_id(binding_id)
    assert before is not None
    before_snapshot = (
        before.status,
        before.runtime_ref,
        before.capability_ref,
        before.runtime_epoch,
        before.config_revision,
        before.updated_at,
    )

    updated = await repositories.agent_bindings.update_desired_runtime(
        binding_id,
        "runtime-b",
        "capability-b",
        1,
        True,
    )

    assert updated is None
    after = await repositories.agent_bindings.get_by_id(binding_id)
    assert after is not None
    assert (
        after.status,
        after.runtime_ref,
        after.capability_ref,
        after.runtime_epoch,
        after.config_revision,
        after.updated_at,
    ) == before_snapshot


async def test_binding_creation_defaults_disabled_and_active_lookup_covers_non_revoked(
    repository_context: RepositoryContext,
) -> None:
    repositories = repository_context.repositories

    default_id = await _create_binding(
        repository_context,
        "default-disabled",
        status=None,
    )
    default = await repositories.agent_bindings.get_by_id(default_id)
    assert default is not None
    assert default.status == "disabled"
    assert (
        await repositories.agent_bindings.active_binding_for(
            default.profile_id,
            default.term_id,
        )
    ).id == default_id

    enabled_id = await _create_binding(
        repository_context,
        "active-enabled",
        status="enabled",
    )
    enabled = await repositories.agent_bindings.get_by_id(enabled_id)
    assert enabled is not None
    assert (
        await repositories.agent_bindings.active_binding_for(
            enabled.profile_id,
            enabled.term_id,
        )
    ).id == enabled_id

    revoked_id = await _create_binding(
        repository_context,
        "inactive-revoked",
        status="revoked",
    )
    revoked = await repositories.agent_bindings.get_by_id(revoked_id)
    assert revoked is not None
    assert (
        await repositories.agent_bindings.active_binding_for(
            revoked.profile_id,
            revoked.term_id,
        )
        is None
    )


async def test_update_desired_runtime_authority_change_advances_revision_and_rotates_epoch_once(
    repository_context: RepositoryContext,
) -> None:
    repositories = repository_context.repositories
    binding_id = await _create_binding(repository_context, "authority-change")

    updated = await repositories.agent_bindings.update_desired_runtime(
        binding_id,
        "runtime-b",
        "capability-b",
        1,
        True,
    )
    assert updated is not None
    assert updated.runtime_ref == "runtime-b"
    assert updated.capability_ref == "capability-b"
    assert updated.runtime_epoch == 8
    assert updated.config_revision == 2
    accepted_snapshot = (
        updated.runtime_ref,
        updated.capability_ref,
        updated.runtime_epoch,
        updated.config_revision,
        updated.updated_at,
    )

    replay = await repositories.agent_bindings.update_desired_runtime(
        binding_id,
        "runtime-c",
        "capability-c",
        1,
        True,
    )
    unrotated_identity_change = await repositories.agent_bindings.update_desired_runtime(
        binding_id,
        "runtime-c",
        "capability-c",
        2,
        False,
    )
    assert replay is None
    assert unrotated_identity_change is None
    persisted = await repositories.agent_bindings.get_by_id(binding_id)
    assert persisted is not None
    assert (
        persisted.runtime_ref,
        persisted.capability_ref,
        persisted.runtime_epoch,
        persisted.config_revision,
        persisted.updated_at,
    ) == accepted_snapshot


async def test_compare_and_set_ready_rejects_stale_revision_without_mutating_observed_state(
    repository_context: RepositoryContext,
) -> None:
    repositories = repository_context.repositories
    binding_id = await _create_binding(repository_context, "ready-cas")
    fingerprint = "a" * 64
    accepted_at = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    await repositories.agent_provider_disclosures.create(
        binding_id=binding_id,
        disclosure_fingerprint=fingerprint,
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        endpoint_origin="https://api.deepseek.com",
        region="global",
        retention_terms="provider-policy",
        retention_version="v1",
        no_training=True,
        policy_version="v1",
        accepted_at=accepted_at,
        accepted_auth_epoch=4,
        actor_kind="admin",
        actor_ref="admin:test",
    )
    reconciling = await repositories.agent_runtime_bindings.mark_reconciling(
        binding_id,
        1,
        fingerprint,
    )
    assert reconciling is not None
    before = _observed_snapshot(reconciling)

    desired = await repositories.agent_bindings.update_desired_runtime(
        binding_id,
        "runtime-a",
        "capability-a",
        1,
        False,
    )
    assert desired is not None
    assert desired.config_revision == 2

    stale = await repositories.agent_runtime_bindings.compare_and_set_ready(
        binding_id,
        1,
        fingerprint,
        7,
    )
    assert stale is False
    unchanged = await repositories.agent_runtime_bindings.get_by_binding(binding_id)
    assert unchanged is not None
    assert _observed_snapshot(unchanged) == before

    current = await repositories.agent_runtime_bindings.mark_reconciling(
        binding_id,
        2,
        fingerprint,
    )
    assert current is not None
    current_snapshot = _observed_snapshot(current)
    assert not await repositories.agent_runtime_bindings.compare_and_set_ready(
        binding_id,
        2,
        fingerprint,
        8,
    )
    wrong_epoch = await repositories.agent_runtime_bindings.get_by_binding(binding_id)
    assert wrong_epoch is not None
    assert _observed_snapshot(wrong_epoch) == current_snapshot

    disabled = await repositories.agent_bindings.set_status(binding_id, "disabled")
    assert disabled is not None
    assert not await repositories.agent_runtime_bindings.compare_and_set_ready(
        binding_id,
        2,
        fingerprint,
        7,
    )
    disabled_observed = await repositories.agent_runtime_bindings.get_by_binding(binding_id)
    assert disabled_observed is not None
    assert _observed_snapshot(disabled_observed) == current_snapshot
    enabled = await repositories.agent_bindings.set_status(binding_id, "enabled")
    assert enabled is not None

    assert await repositories.agent_runtime_bindings.compare_and_set_ready(
        binding_id,
        2,
        fingerprint,
        7,
    )
    ready = await repositories.agent_runtime_bindings.get_by_binding(binding_id)
    assert ready is not None
    assert ready.readiness == "ready"
    assert ready.reason_code is None
    assert ready.observed_runtime_ref == "runtime-a"
    assert ready.observed_runtime_epoch == 7
    assert ready.observed_capability_ref == "capability-a"
    assert ready.applied_revision == 2
    assert ready.config_fingerprint == fingerprint

    next_desired = await repositories.agent_bindings.update_desired_runtime(
        binding_id,
        "runtime-a",
        "capability-a",
        2,
        False,
    )
    assert next_desired is not None
    next_reconciling = await repositories.agent_runtime_bindings.mark_reconciling(
        binding_id,
        3,
        fingerprint,
    )
    assert next_reconciling is not None
    await repositories.agent_provider_disclosures.revoke_all_current(binding_id)
    revoked_snapshot = _observed_snapshot(next_reconciling)
    assert not await repositories.agent_runtime_bindings.compare_and_set_ready(
        binding_id,
        3,
        fingerprint,
        7,
    )
    revoked = await repositories.agent_runtime_bindings.get_by_binding(binding_id)
    assert revoked is not None
    assert _observed_snapshot(revoked) == revoked_snapshot


async def test_mark_reconciling_requires_enabled_expected_revision_and_upserts_one_row(
    repository_context: RepositoryContext,
) -> None:
    repositories = repository_context.repositories
    binding_id = await _create_binding(
        repository_context,
        "reconciling",
        status="disabled",
    )
    fingerprint = "b" * 64
    seeded = await repositories.agent_runtime_bindings.upsert(
        binding_id=binding_id,
        runtime_ref="runtime-old",
        runtime_epoch=5,
        readiness="not_ready",
    )
    async with repository_context.sessions() as session:
        await session.execute(
            update(AgentRuntimeBinding)
            .where(AgentRuntimeBinding.id == seeded.id)
            .values(
                observed_capability_ref="capability-old",
                applied_revision=3,
                config_fingerprint="c" * 64,
            )
        )
        await session.commit()

    assert (
        await repositories.agent_runtime_bindings.mark_reconciling(
            binding_id,
            1,
            fingerprint,
        )
        is None
    )
    enabled = await repositories.agent_bindings.set_status(binding_id, "enabled")
    assert enabled is not None
    assert (
        await repositories.agent_runtime_bindings.mark_reconciling(
            binding_id,
            2,
            fingerprint,
        )
        is None
    )

    first = await repositories.agent_runtime_bindings.mark_reconciling(
        binding_id,
        1,
        fingerprint,
    )
    second = await repositories.agent_runtime_bindings.mark_reconciling(
        binding_id,
        1,
        fingerprint,
    )
    assert first is not None
    assert second is not None
    assert second.id == first.id == seeded.id
    assert second.readiness == "reconciling"
    assert second.reason_code is None
    assert second.config_fingerprint == fingerprint
    assert second.transition_started_at is not None
    assert second.observed_runtime_ref == "runtime-old"
    assert second.observed_runtime_epoch == 5
    assert second.observed_capability_ref == "capability-old"
    assert second.applied_revision == 3
    async with repository_context.sessions() as session:
        count = await session.scalar(
            select(func.count(AgentRuntimeBinding.id)).where(
                AgentRuntimeBinding.binding_id == binding_id
            )
        )
    assert count == 1

    concurrent_binding_id = await _create_binding(
        repository_context,
        "concurrent-reconciling",
    )
    concurrent = await asyncio.gather(
        repositories.agent_runtime_bindings.mark_reconciling(
            concurrent_binding_id,
            1,
            fingerprint,
        ),
        repositories.agent_runtime_bindings.mark_reconciling(
            concurrent_binding_id,
            1,
            fingerprint,
        ),
    )
    assert all(runtime is not None for runtime in concurrent)
    assert len({runtime.id for runtime in concurrent if runtime is not None}) == 1
    async with repository_context.sessions() as session:
        concurrent_count = await session.scalar(
            select(func.count(AgentRuntimeBinding.id)).where(
                AgentRuntimeBinding.binding_id == concurrent_binding_id
            )
        )
    assert concurrent_count == 1


async def test_mark_unavailable_cannot_synthesize_ready_and_preserves_applied_identity(
    repository_context: RepositoryContext,
) -> None:
    repositories = repository_context.repositories
    binding_id = await _create_binding(repository_context, "unavailable")
    runtime = await repositories.agent_runtime_bindings.upsert(
        binding_id=binding_id,
        runtime_ref="runtime-applied",
        runtime_epoch=9,
        readiness="not_ready",
    )
    async with repository_context.sessions() as session:
        await session.execute(
            update(AgentRuntimeBinding)
            .where(AgentRuntimeBinding.id == runtime.id)
            .values(
                readiness="ready",
                observed_capability_ref="capability-applied",
                applied_revision=4,
                config_fingerprint="d" * 64,
            )
        )
        await session.commit()

    unavailable = await repositories.agent_runtime_bindings.mark_unavailable(
        binding_id,
        "not_ready",
        "runtime_unreachable",
    )
    assert unavailable is not None
    assert unavailable.readiness == "not_ready"
    assert unavailable.reason_code == "runtime_unreachable"
    identity = (
        unavailable.observed_runtime_ref,
        unavailable.observed_runtime_epoch,
        unavailable.observed_capability_ref,
        unavailable.applied_revision,
        unavailable.config_fingerprint,
    )
    before_rejections = _observed_snapshot(unavailable)

    with pytest.raises(ValueError, match="ready"):
        await repositories.agent_runtime_bindings.mark_unavailable(
            binding_id,
            "ready",
            None,
        )
    with pytest.raises(ValueError, match="ready"):
        await repositories.agent_runtime_bindings.set_readiness(runtime.id, "ready")
    with pytest.raises(ValueError, match="ready"):
        await repositories.agent_runtime_bindings.upsert(
            binding_id=binding_id,
            runtime_ref="runtime-forged",
            runtime_epoch=10,
            readiness="ready",
        )

    persisted = await repositories.agent_runtime_bindings.get_by_binding(binding_id)
    assert persisted is not None
    assert _observed_snapshot(persisted) == before_rejections
    assert (
        persisted.observed_runtime_ref,
        persisted.observed_runtime_epoch,
        persisted.observed_capability_ref,
        persisted.applied_revision,
        persisted.config_fingerprint,
    ) == identity
