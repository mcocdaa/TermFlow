from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import select
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentProviderDisclosureAcceptance
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret


@pytest_asyncio.fixture
async def repositories(tmp_path) -> AsyncIterator[RepositoryBundle]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'provider-disclosure.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


async def _binding(repositories: RepositoryBundle) -> UUID:
    installation = await repositories.installations.create(
        digest_secret("disclosure-installation")
    )
    term = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        "disclosure-term",
        digest_secret("disclosure-term"),
    )
    profile = await repositories.agent_profiles.create(
        display_name="disclosure-profile",
        backend_kind="opencode",
        config='{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
    )
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="enabled",
        runtime_ref="runtime-disclosure",
        runtime_epoch=1,
        capability_ref="capability-disclosure",
    )
    return binding.id


async def test_current_disclosure_lookup_rejects_revoked_or_nonmatching_acceptance(
    repositories: RepositoryBundle,
) -> None:
    binding_id = await _binding(repositories)
    fingerprint = "e" * 64
    accepted_at = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)

    first = await repositories.agent_provider_disclosures.create(
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
        accepted_auth_epoch=6,
        actor_kind="admin",
        actor_ref="admin:test",
    )
    current = await repositories.agent_provider_disclosures.get_current(
        binding_id,
        fingerprint,
    )
    assert current is not None
    assert current.id == first.id
    assert current.revoked_at is None
    assert (
        await repositories.agent_provider_disclosures.get_current(
            binding_id,
            "f" * 64,
        )
        is None
    )

    revoked_at = accepted_at + timedelta(minutes=1)
    assert (
        await repositories.agent_provider_disclosures.revoke_all_current(
            binding_id,
            now=revoked_at,
        )
        == 1
    )
    assert (
        await repositories.agent_provider_disclosures.get_current(
            binding_id,
            fingerprint,
        )
        is None
    )

    async with repositories.session_factory() as session:  # type: ignore[attr-defined]
        history = list(
            await session.scalars(
                select(AgentProviderDisclosureAcceptance).where(
                    AgentProviderDisclosureAcceptance.binding_id == binding_id
                )
            )
        )
    assert len(history) == 1
    assert history[0].id == first.id
    assert history[0].accepted_at == accepted_at.replace(tzinfo=None)
    assert history[0].revoked_at == revoked_at.replace(tzinfo=None)
    assert all("credential" not in column.name for column in history[0].__table__.columns)
    assert "credential" not in inspect.signature(
        repositories.agent_provider_disclosures.create
    ).parameters
