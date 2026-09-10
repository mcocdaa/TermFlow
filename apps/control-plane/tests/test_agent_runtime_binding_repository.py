from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentRuntimeBinding
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime-binding.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


async def _binding(repositories: RepositoryBundle, name: str) -> UUID:
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
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="disabled",
    )
    return binding.id


async def test_upsert_replaces_one_bindings_observed_identity_in_place(
    repositories: RepositoryBundle,
) -> None:
    binding_id = await _binding(repositories, "replacement")
    first = await repositories.agent_runtime_bindings.upsert(
        binding_id=binding_id,
        runtime_ref="runtime-before",
        runtime_epoch=1,
        readiness="not_ready",
    )

    replacement = await repositories.agent_runtime_bindings.upsert(
        binding_id=binding_id,
        runtime_ref="runtime-after",
        runtime_epoch=2,
        readiness="reconciling",
    )

    assert replacement.id == first.id
    assert replacement.binding_id == binding_id
    assert replacement.observed_runtime_ref == "runtime-after"
    assert replacement.observed_runtime_epoch == 2
    assert replacement.readiness == "reconciling"
    async with repositories.session_factory() as session:  # type: ignore[attr-defined]
        assert (
            await session.scalar(
                select(func.count(AgentRuntimeBinding.id)).where(
                    AgentRuntimeBinding.binding_id == binding_id
                )
            )
            == 1
        )


async def test_upsert_rejects_observed_identity_owned_by_another_binding(
    repositories: RepositoryBundle,
) -> None:
    first_binding_id = await _binding(repositories, "identity-owner")
    second_binding_id = await _binding(repositories, "identity-contender")
    first = await repositories.agent_runtime_bindings.upsert(
        binding_id=first_binding_id,
        runtime_ref="owned-runtime",
        runtime_epoch=7,
    )
    second = await repositories.agent_runtime_bindings.upsert(
        binding_id=second_binding_id,
        runtime_ref="other-runtime",
        runtime_epoch=3,
    )

    with pytest.raises(IntegrityError):
        await repositories.agent_runtime_bindings.upsert(
            binding_id=second_binding_id,
            runtime_ref="owned-runtime",
            runtime_epoch=7,
        )

    unchanged_first = await repositories.agent_runtime_bindings.get_by_binding(first_binding_id)
    unchanged_second = await repositories.agent_runtime_bindings.get_by_binding(second_binding_id)
    assert unchanged_first is not None
    assert unchanged_first.id == first.id
    assert unchanged_first.observed_runtime_ref == "owned-runtime"
    assert unchanged_first.observed_runtime_epoch == 7
    assert unchanged_second is not None
    assert unchanged_second.id == second.id
    assert unchanged_second.observed_runtime_ref == "other-runtime"
    assert unchanged_second.observed_runtime_epoch == 3
