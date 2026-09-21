"""AgentToken authentication dependency tests (plan §10, task M2.3).

``require_agent_token`` accepts only a hashed AgentToken bound to one
binding/Term/runtime epoch.  Revoked, expired, epoch-mismatched, and
missing tokens fail closed; there is no generic admin/native token
fallback.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.persistence.models import AgentToken, Base
from termflow_control_plane.persistence.repositories import (
    RepositoryBundle,
    digest_secret,
)
from termflow_control_plane.plugins.agent_broker.auth import (
    SCOPE_TERMINAL_OBSERVE,
    SCOPE_TERMINAL_WRITE,
    AgentTokenAuthenticator,
    AgentTokenAuthError,
    AgentTokenPrincipal,
    require_agent_token,
)


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    bundle = RepositoryBundle(session_factory)
    # Test seam: let tests open ad-hoc sessions against the same database.
    bundle.session_factory = session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await engine.dispose()


async def _seed_term(repositories: RepositoryBundle) -> tuple[object, object]:
    display_name = f"term-{uuid4().hex[:8]}"
    installation = await repositories.installations.create(digest_secret(f"computer-{uuid4().hex}"))
    term = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    return installation, term


async def _seed_binding(
    repositories: RepositoryBundle,
    *,
    runtime_epoch: int | None = 2,
    status: str = "ready",
):
    profile = await repositories.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    _, term = await _seed_term(repositories)
    return await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status=status,
        runtime_ref="runtime-1" if runtime_epoch is not None else None,
        runtime_epoch=runtime_epoch,
        capability_ref="cap-1" if runtime_epoch is not None else None,
    )


async def _seed_token(
    repositories: RepositoryBundle,
    binding,
    *,
    binding_epoch: int | None = None,
    expires_in: timedelta = timedelta(hours=1),
    scopes: tuple[str, ...] = (SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE),
) -> str:
    raw_token = f"mcp-{uuid4().hex}"
    epoch = binding.runtime_epoch if binding_epoch is None else binding_epoch
    await repositories.agent_tokens.create(
        binding_id=binding.id,
        token_hash=hash_token(raw_token),
        scopes=scopes,
        expiry_epoch=int((datetime.now(UTC) + expires_in).timestamp()),
        binding_epoch=epoch,
    )
    return raw_token


@pytest.mark.asyncio
async def test_valid_token_authenticates_with_binding_scope(repositories) -> None:
    binding = await _seed_binding(repositories, runtime_epoch=2)
    raw_token = await _seed_token(repositories, binding, binding_epoch=2)
    authenticator = AgentTokenAuthenticator(repositories)

    principal = await authenticator.authenticate(raw_token)

    assert isinstance(principal, AgentTokenPrincipal)
    assert principal.binding_id == binding.id
    assert principal.instance_id == binding.term_id
    assert principal.runtime_epoch == 2
    assert principal.scopes == frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE})

    # The HTTP dependency wraps the same authenticator without admin fallback.
    dependency = require_agent_token(authenticator)
    assert (await dependency(raw_token)).binding_id == binding.id
    with pytest.raises(AgentTokenAuthError):
        await dependency("mcp-not-issued")


@pytest.mark.asyncio
async def test_enabled_binding_authenticates_with_binding_scope(repositories) -> None:
    """The 0011 desired state ``enabled`` is an active token state.

    Setup provisions enabled Bindings before the runtime controller has
    published readiness; the token gate must not retain the pre-0011-only
    ``pending``/``ready`` allowlist.
    """
    binding = await _seed_binding(repositories, runtime_epoch=2, status="enabled")
    raw_token = await _seed_token(repositories, binding, binding_epoch=2)

    principal = await AgentTokenAuthenticator(repositories).authenticate(raw_token)

    assert principal.binding_id == binding.id
    assert principal.runtime_epoch == 2


@pytest.mark.asyncio
async def test_invalid_token_variants_fail_closed(repositories) -> None:
    authenticator = AgentTokenAuthenticator(repositories)

    async def expect_rejected(raw_token: str) -> None:
        with pytest.raises(AgentTokenAuthError) as caught:
            await authenticator.authenticate(raw_token)
        assert caught.value.status_code == 401

    # Missing/unknown tokens.
    for raw in ("", "mcp-never-issued"):
        await expect_rejected(raw)

    # Revoked token.
    binding = await _seed_binding(repositories)
    revoked = await _seed_token(repositories, binding)
    assert await repositories.agent_tokens.revoke(hash_token(revoked)) is True
    await expect_rejected(revoked)

    # Expired token.
    expired_binding = await _seed_binding(repositories)
    expired = await _seed_token(repositories, expired_binding, expires_in=timedelta(seconds=-60))
    await expect_rejected(expired)

    # Token minted at an earlier binding epoch is stale after rotation.
    epoch_binding = await _seed_binding(repositories, runtime_epoch=3)
    stale = await _seed_token(repositories, epoch_binding, binding_epoch=1)
    await expect_rejected(stale)

    # Binding with an unprovisioned runtime.
    unprovisioned_binding = await _seed_binding(repositories, runtime_epoch=None, status="pending")
    unprovisioned = await _seed_token(repositories, unprovisioned_binding, binding_epoch=1)
    await expect_rejected(unprovisioned)

    # Non-ready binding.
    disabled_binding = await _seed_binding(repositories, runtime_epoch=2, status="disabled")
    disabled = await _seed_token(repositories, disabled_binding, binding_epoch=2)
    await expect_rejected(disabled)

    # Deleted binding.
    deleted_binding = await _seed_binding(repositories, runtime_epoch=2)
    deleted = await _seed_token(repositories, deleted_binding, binding_epoch=2)
    assert await repositories.agent_bindings.delete(deleted_binding.id) is True
    await expect_rejected(deleted)

    # Malformed persisted scopes fail closed rather than crashing.
    malformed_binding = await _seed_binding(repositories, runtime_epoch=2)
    malformed = await _seed_token(repositories, malformed_binding, binding_epoch=2)
    token = await repositories.agent_tokens.get_by_hash(hash_token(malformed))
    assert token is not None
    async with repositories.session_factory() as session:  # type: ignore[attr-defined]
        await session.execute(
            update(AgentToken).where(AgentToken.id == token.id).values(scopes='"not-a-list"')
        )
        await session.commit()
    await expect_rejected(malformed)
