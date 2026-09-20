from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import func, select
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentProviderDisclosureAcceptance,
    AgentRuntimeBinding,
    AgentSetupReceipt,
    AgentToken,
    PanePolicy,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogEntry,
    canonicalize_profile_config,
)
from termflow_control_plane.plugins.agent_broker.agent.provisioning import (
    AgentProvisioningService,
    AgentSetupCommand,
)


@pytest_asyncio.fixture
async def ctx(tmp_path) -> AsyncIterator[tuple[Database, RepositoryBundle, object, object, object]]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'setup.db'}")
    await db.initialize()
    repos = RepositoryBundle(db.session_factory)
    installation = await repos.installations.create(digest_secret("install"))
    term_id = uuid4()
    await repos.instances.register_or_rotate(
        term_id, installation.id, "term", digest_secret("term")
    )
    profile = await repos.agent_profiles.create(
        display_name="profile",
        backend_kind="opencode",
        config='{"provider_id":"deepseek","model_id":"deepseek-v4-flash"}',
    )
    catalog = ProviderCatalog(
        (
            ProviderCatalogEntry(
                provider_id="deepseek",
                model_ids=frozenset({"deepseek-v4-flash", "deepseek-reasoner"}),
                endpoint_origin="https://api.deepseek.com",
                region="global",
                retention_terms="none",
                retention_version="1",
                no_training=True,
                credential_source="DEEPSEEK_API_KEY",
                policy_version="1",
            ),
        )
    )
    config, _ = canonicalize_profile_config(profile.config)
    fingerprint = catalog.disclosure_fingerprint(config)
    yield db, repos, term_id, profile, (catalog, fingerprint)
    await db.dispose()


def _command(term_id, profile_id, fingerprint, *, key=None, panes=("%0",)):
    return AgentSetupCommand(
        term_id=term_id,
        profile_id=profile_id,
        profile_display_name=None,
        pane_ids=panes,
        topology_revision=3,
        disclosure_fingerprint=fingerprint,
        accepted=True,
        idempotency_key=key or uuid4(),
    )


@pytest.mark.asyncio
async def test_setup_is_atomic_and_idempotent(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    service = AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret="bootstrap",
    )
    command = _command(term_id, profile.id, fingerprint)
    first = await service.setup(
        command, {"credential_kind": "root", "actor_ref": "admin", "auth_epoch": 1}
    )
    second = await service.setup(
        command, {"credential_kind": "root", "actor_ref": "admin", "auth_epoch": 1}
    )
    assert first == second
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentBinding)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(AgentProviderDisclosureAcceptance)
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(AgentToken)) == 1
        assert await session.scalar(select(func.count()).select_from(PanePolicy)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentRuntimeBinding)) == 1
        receipt = await session.scalar(select(AgentSetupReceipt))
        assert receipt is not None
        assert receipt.state == "activating"
        assert receipt.request_digest
        assert "bootstrap" not in receipt.request_digest
        token = await session.scalar(select(AgentToken))
        assert token is not None
        assert json.loads(token.scopes) == ["terminal.observe", "terminal.write"]


@pytest.mark.asyncio
async def test_setup_receipt_is_durable_across_services_and_digest_conflicts(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    command = _command(term_id, profile.id, fingerprint)

    first = await AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret="bootstrap",
    ).setup(command, {})
    second = await AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 999, "pane_ids": []}},
        catalog=catalog,
        bootstrap_secret=None,
    ).setup(command, {})
    assert second == first

    conflicting = _command(
        term_id,
        profile.id,
        fingerprint,
        key=command.idempotency_key,
        panes=("%1",),
    )
    with pytest.raises(Exception) as exc:
        await AgentProvisioningService(
            repos,
            topology={term_id: {"revision": 3, "pane_ids": ["%1"]}},
            catalog=catalog,
            bootstrap_secret="bootstrap",
        ).setup(conflicting, {})
    assert getattr(exc.value, "code", None) == "idempotency_conflict"

    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentSetupReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentBinding)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentToken)) == 1


@pytest.mark.asyncio
async def test_setup_receipt_claim_is_atomic_across_concurrent_services(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    command = _command(term_id, profile.id, fingerprint)

    def make_service():
        return AgentProvisioningService(
            repos,
            topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
            catalog=catalog,
            bootstrap_secret="bootstrap",
        )

    results = await asyncio.gather(
        make_service().setup(command, {}),
        make_service().setup(command, {}),
    )
    assert results[0] == results[1]
    async with db.session_factory() as session:
        for model in (
            AgentSetupReceipt,
            AgentBinding,
            AgentProviderDisclosureAcceptance,
            AgentToken,
            PanePolicy,
            AgentRuntimeBinding,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.asyncio
async def test_setup_result_is_persisted_for_restart(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx

    class Ready:
        readiness = "ready"
        reason_code = None

    async def reconcile(_binding_id):
        return Ready()

    command = _command(term_id, profile.id, fingerprint)
    first = await AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret="bootstrap",
        controller=reconcile,
    ).setup(command, {})
    restarted = await AgentProvisioningService(
        repos,
        topology=None,
        catalog=None,
        bootstrap_secret=None,
    ).setup(command, {})
    assert first.state == "ready"
    assert restarted == first
    async with db.session_factory() as session:
        receipt = await session.scalar(select(AgentSetupReceipt))
        assert receipt is not None
        assert receipt.state == "ready"
        assert receipt.reason_code is None


@pytest.mark.asyncio
async def test_setup_rejects_stale_topology_before_writing_any_rows(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    service = AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 4, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret="bootstrap",
    )
    with pytest.raises(Exception) as exc:
        await service.setup(_command(term_id, profile.id, fingerprint), {})
    assert getattr(exc.value, "code", None) == "stale_topology"
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentBinding)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentSetupReceipt)) == 0


@pytest.mark.asyncio
async def test_setup_missing_bootstrap_secret_writes_nothing(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    service = AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret=None,
    )
    with pytest.raises(Exception) as exc:
        await service.setup(_command(term_id, profile.id, fingerprint), {})
    assert getattr(exc.value, "code", None) == "deployment_required"
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentBinding)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentSetupReceipt)) == 0


@pytest.mark.asyncio
async def test_reconcile_failure_keeps_consistent_activating_state(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx

    async def fail(_binding_id):
        raise RuntimeError("offline")

    service = AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret="bootstrap",
        controller=fail,
    )
    result = await service.setup(_command(term_id, profile.id, fingerprint), {})
    assert result.binding_id is not None
    assert result.reason_code == "runtime_unreachable"
    async with db.session_factory() as session:
        binding = await session.get(AgentBinding, result.binding_id)
        runtime = await session.scalar(
            select(AgentRuntimeBinding).where(AgentRuntimeBinding.binding_id == result.binding_id)
        )
        assert binding is not None and binding.status == "enabled"
        assert runtime is not None and runtime.readiness == "not_ready"
        assert runtime.reason_code == "runtime_unreachable"
        receipt = await session.scalar(select(AgentSetupReceipt))
        assert receipt is not None
        assert receipt.state == "unavailable"
        assert receipt.reason_code == "runtime_unreachable"


@pytest.mark.asyncio
async def test_setup_resolves_secretstr_from_async_bootstrap_provider(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx

    class AsyncBootstrap:
        calls = 0

        async def get_secret(self, requested_term_id):
            assert requested_term_id == term_id
            self.calls += 1
            return SecretStr("async-bootstrap-secret")

    bootstrap = AsyncBootstrap()
    result = await AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret=bootstrap,
    ).setup(_command(term_id, profile.id, fingerprint), {})

    assert result.state == "activating"
    assert bootstrap.calls == 1
    async with db.session_factory() as session:
        token = await session.scalar(select(AgentToken))
        assert token is not None
        assert token.token_hash == digest_secret("async-bootstrap-secret")


@pytest.mark.asyncio
async def test_setup_reconfiguration_reuses_binding_and_advances_revision(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    topology = {term_id: {"revision": 3, "pane_ids": ["%0", "%1"]}}
    service = AgentProvisioningService(
        repos,
        topology=topology,
        catalog=catalog,
        bootstrap_secret="bootstrap",
    )

    first_command = _command(term_id, profile.id, fingerprint, panes=("%0",))
    first = await service.setup(first_command, {})
    assert first.binding_id is not None

    second = await service.setup(
        _command(
            term_id,
            profile.id,
            fingerprint,
            panes=("%1",),
            key=uuid4(),
        ),
        {},
    )
    assert second.binding_id == first.binding_id

    changed_entry = replace(next(iter(catalog._entries.values())), retention_version="2")
    changed_catalog = ProviderCatalog((changed_entry,))
    changed_fingerprint = changed_catalog.disclosure_fingerprint(
        canonicalize_profile_config(profile.config)[0]
    )
    service._catalog = changed_catalog
    third = await service.setup(
        _command(
            term_id,
            profile.id,
            changed_fingerprint,
            panes=("%1",),
            key=uuid4(),
        ),
        {},
    )
    assert third.binding_id == first.binding_id

    profile.config = '{"model_id":"deepseek-reasoner","provider_id":"deepseek"}'
    async with db.session_factory() as session:
        persisted_profile = await session.get(type(profile), profile.id)
        assert persisted_profile is not None
        persisted_profile.config = profile.config
        await session.commit()
    changed_profile_config, _ = canonicalize_profile_config(profile.config)
    changed_profile_fingerprint = changed_catalog.disclosure_fingerprint(changed_profile_config)
    fourth = await service.setup(
        _command(
            term_id,
            profile.id,
            changed_profile_fingerprint,
            panes=("%1",),
            key=uuid4(),
        ),
        {},
    )
    assert fourth.binding_id == first.binding_id

    async with db.session_factory() as session:
        binding = await session.get(AgentBinding, first.binding_id)
        assert binding is not None
        assert binding.profile_id == profile.id
        assert binding.config_revision == 4
        assert binding.runtime_ref == "opencode-agent"
        assert binding.runtime_epoch == 1
        assert binding.capability_ref == f"termflow-mcp:{binding.id}"
        policies = list(
            await session.scalars(select(PanePolicy).where(PanePolicy.binding_id == binding.id))
        )
        assert {policy.pane_id for policy in policies if policy.allowed} == {"%1"}
        disclosures = list(
            await session.scalars(
                select(AgentProviderDisclosureAcceptance).where(
                    AgentProviderDisclosureAcceptance.binding_id == binding.id
                )
            )
        )
        assert len(disclosures) == 3
        assert sum(row.revoked_at is None for row in disclosures) == 1
        assert any(
            row.disclosure_fingerprint == fingerprint and row.revoked_at is not None
            for row in disclosures
        )
        assert any(
            row.disclosure_fingerprint == changed_fingerprint and row.revoked_at is not None
            for row in disclosures
        )

    # A controller that started against revision 1 can no longer publish its
    # candidate after setup committed the revision-4 reconfiguration.
    assert (
        await repos.agent_runtime_bindings.compare_and_set_ready(
            first.binding_id,
            1,
            changed_profile_fingerprint,
            1,
        )
        is False
    )


@pytest.mark.asyncio
async def test_setup_rearms_revoked_capability_after_binding_revoke(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    service = AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret="bootstrap",
    )
    first = await service.setup(_command(term_id, profile.id, fingerprint), {})
    assert first.binding_id is not None
    closed = await repos.agent_bindings.set_status(
        first.binding_id, "revoked", advance_runtime_epoch=True
    )
    assert closed is not None

    # The deployment bootstrap secret is re-issued for the new Binding; the
    # unique token hash forces the revoked row to be re-armed instead of
    # inserting a duplicate (previously a 500 UNIQUE violation).
    second = await service.setup(_command(term_id, profile.id, fingerprint, key=uuid4()), {})
    assert second.binding_id is not None
    assert second.binding_id != first.binding_id

    async with db.session_factory() as session:
        tokens = list(await session.scalars(select(AgentToken)))
        assert len(tokens) == 1
        binding = await session.get(AgentBinding, second.binding_id)
        assert binding is not None
        assert tokens[0].binding_id == second.binding_id
        assert tokens[0].revoked_at is None
        assert tokens[0].binding_epoch == binding.runtime_epoch
        assert tokens[0].token_hash == digest_secret("bootstrap")


@pytest.mark.asyncio
async def test_setup_allocates_a_fresh_runtime_epoch_after_revoke(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    service = AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret="bootstrap",
    )
    first = await service.setup(_command(term_id, profile.id, fingerprint), {})
    assert first.binding_id is not None
    assert (
        await repos.agent_runtime_bindings.compare_and_set_ready(
            first.binding_id, 1, fingerprint, 1
        )
        is True
    )
    await repos.agent_bindings.set_status(first.binding_id, "revoked", advance_runtime_epoch=True)

    second = await service.setup(_command(term_id, profile.id, fingerprint, key=uuid4()), {})
    assert second.binding_id is not None
    async with db.session_factory() as session:
        binding = await session.get(AgentBinding, second.binding_id)
        assert binding is not None
        assert binding.runtime_epoch == 3
    # The fresh epoch must not collide with the closed Binding's observed row.
    assert (
        await repos.agent_runtime_bindings.compare_and_set_ready(
            second.binding_id, 1, fingerprint, 3
        )
        is True
    )


@pytest.mark.asyncio
async def test_setup_rejects_a_capability_active_for_another_binding(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx
    service = AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret="bootstrap",
    )
    first = await service.setup(_command(term_id, profile.id, fingerprint), {})
    assert first.binding_id is not None

    other = await repos.agent_profiles.create(
        display_name="other",
        backend_kind="opencode",
        config='{"model_id":"deepseek-reasoner","provider_id":"deepseek"}',
    )
    other_config, _ = canonicalize_profile_config(other.config)
    other_fingerprint = catalog.disclosure_fingerprint(other_config)
    with pytest.raises(Exception) as exc:
        await service.setup(_command(term_id, other.id, other_fingerprint, key=uuid4()), {})
    assert getattr(exc.value, "code", None) == "capability_conflict"


@pytest.mark.asyncio
async def test_setup_accepts_injected_server_owned_runtime_assignment(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx

    async def assignment(requested_term_id):
        assert requested_term_id == term_id
        return ("runtime-from-server", 7, "capability-from-server")

    result = await AgentProvisioningService(
        repos,
        topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
        catalog=catalog,
        bootstrap_secret=SecretStr("bootstrap"),
        runtime_assignment=assignment,
    ).setup(_command(term_id, profile.id, fingerprint), {})

    assert result.binding_id is not None
    async with db.session_factory() as session:
        binding = await session.get(AgentBinding, result.binding_id)
        assert binding is not None
        assert binding.runtime_ref == "runtime-from-server"
        assert binding.runtime_epoch == 7
        assert binding.capability_ref == "capability-from-server"


@pytest.mark.asyncio
async def test_setup_requires_capability_rotation_for_same_secret_on_new_epoch(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx

    def service(epoch: int, secret: str) -> AgentProvisioningService:
        return AgentProvisioningService(
            repos,
            topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
            catalog=catalog,
            bootstrap_secret=secret,
            runtime_assignment=("runtime-from-server", epoch, "capability-from-server"),
        )

    first = await service(1, "bootstrap").setup(_command(term_id, profile.id, fingerprint), {})
    assert first.binding_id is not None

    with pytest.raises(Exception) as exc:
        await service(2, "bootstrap").setup(
            _command(term_id, profile.id, fingerprint, key=uuid4()), {}
        )
    assert getattr(exc.value, "code", None) == "capability_rotation_required"

    async with db.session_factory() as session:
        binding = await session.get(AgentBinding, first.binding_id)
        tokens = list(
            await session.scalars(
                select(AgentToken).where(AgentToken.binding_id == first.binding_id)
            )
        )
        assert binding is not None and binding.runtime_epoch == 1
        assert len(tokens) == 1 and tokens[0].binding_epoch == 1
        assert tokens[0].revoked_at is None

    rotated = await service(2, "new-bootstrap").setup(
        _command(term_id, profile.id, fingerprint, key=uuid4()), {}
    )
    assert rotated.binding_id == first.binding_id
    async with db.session_factory() as session:
        binding = await session.get(AgentBinding, first.binding_id)
        tokens = list(
            await session.scalars(
                select(AgentToken)
                .where(AgentToken.binding_id == first.binding_id)
                .order_by(AgentToken.created_at)
            )
        )
        assert binding is not None and binding.runtime_epoch == 2
        assert len(tokens) == 2
        assert sum(token.revoked_at is None for token in tokens) == 1
        active = next(token for token in tokens if token.revoked_at is None)
        assert active.binding_epoch == 2
        assert active.token_hash == digest_secret("new-bootstrap")


@pytest.mark.asyncio
async def test_invalid_runtime_assignment_rolls_back_the_setup_claim(ctx):
    db, repos, term_id, profile, (catalog, fingerprint) = ctx

    async def invalid_assignment(_requested_term_id):
        return {"runtime_ref": "", "runtime_epoch": 0, "capability_ref": None}

    with pytest.raises(Exception) as exc:
        await AgentProvisioningService(
            repos,
            topology={term_id: {"revision": 3, "pane_ids": ["%0"]}},
            catalog=catalog,
            bootstrap_secret="bootstrap",
            runtime_assignment=invalid_assignment,
        ).setup(_command(term_id, profile.id, fingerprint), {})
    assert getattr(exc.value, "code", None) == "runtime_assignment_conflict"
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentSetupReceipt)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentBinding)) == 0
