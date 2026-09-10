from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from termflow_control_plane.api.agent_admin import _update_profile_and_invalidate_bindings
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentProfile,
    AgentProviderDisclosureAcceptance,
    AgentRuntimeBinding,
    AgentToken,
    PanePolicy,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle


def _create_profile(
    client: TestClient,
    headers: dict[str, str],
    display_name: str,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=headers,
        json={
            "display_name": display_name,
            "backend_kind": "opencode",
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_duplicate_profile_name_returns_conflict(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    _create_profile(client, admin_headers, "duplicate-profile")

    duplicate = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "duplicate-profile",
            "backend_kind": "opencode",
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        },
    )

    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "agent_profile_name_conflict"


def test_profile_rename_conflict_preserves_original_name(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    first = _create_profile(client, admin_headers, "first-profile")
    second = _create_profile(client, admin_headers, "second-profile")

    conflict = client.patch(
        f"/api/v1/agent/admin/profiles/{second['profile_id']}",
        headers=admin_headers,
        json={"display_name": first["display_name"]},
    )

    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "agent_profile_name_conflict"

    unchanged = client.get(
        f"/api/v1/agent/admin/profiles/{second['profile_id']}",
        headers=admin_headers,
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["display_name"] == "second-profile"


def test_profile_config_patch_atomically_invalidates_bound_runtime(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    profile_payload = _create_profile(client, admin_headers, "invalidate-profile")
    profile_id = UUID(str(profile_payload["profile_id"]))
    old_fingerprint = "a" * 64

    async def seed() -> UUID:
        repos = RepositoryBundle(client.app.state.session_factory)
        installation = await repos.installations.create("install-hash")
        term = await repos.instances.register_or_rotate(
            uuid4(), installation.id, "term-name", "term-hash"
        )
        binding = AgentBinding(
            profile_id=profile_id,
            term_id=term.id,
            status="enabled",
            runtime_ref="runtime-old",
            runtime_epoch=7,
            capability_ref="cap-old",
            config_revision=4,
        )
        async with client.app.state.session_factory() as session:
            session.add(binding)
            await session.flush()
            now = datetime.now(UTC)
            session.add(
                AgentRuntimeBinding(
                    binding_id=binding.id,
                    readiness="ready",
                    observed_runtime_ref="runtime-old",
                    observed_runtime_epoch=7,
                    observed_capability_ref="cap-old",
                    applied_revision=4,
                    config_fingerprint=old_fingerprint,
                    last_health_at=now,
                    provider_readiness="verified",
                    provider_verified_revision=4,
                    provider_last_checked_at=now,
                )
            )
            session.add(
                AgentProviderDisclosureAcceptance(
                    binding_id=binding.id,
                    disclosure_fingerprint=old_fingerprint,
                    provider_id="deepseek",
                    model_id="deepseek-v4-flash",
                    endpoint_origin="https://api.deepseek.com",
                    region="global",
                    retention_terms="30 days",
                    retention_version="v1",
                    no_training=True,
                    policy_version="v1",
                    accepted_at=now,
                    accepted_auth_epoch=1,
                    actor_kind="root",
                    actor_ref="admin",
                )
            )
            session.add(
                AgentToken(
                    binding_id=binding.id,
                    token_hash="token-hash",
                    scopes='["terminal.observe"]',
                    expiry_epoch=int((now + timedelta(days=1)).timestamp()),
                    binding_epoch=7,
                )
            )
            session.add(PanePolicy(binding_id=binding.id, pane_id="%0", allowed=True))
            await session.commit()
        return binding.id

    binding_id = client.portal.call(seed)

    class RecordingRegistry:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID, ...]] = []

        async def stop_bindings(self, binding_ids: Any) -> int:
            values = tuple(binding_ids)
            self.calls.append(values)
            return len(values)

    registry = RecordingRegistry()
    client.app.state.agent_runtime_registry = registry
    client.app.state.agent_runtime_controller = None

    response = client.patch(
        f"/api/v1/agent/admin/profiles/{profile_id}",
        headers=admin_headers,
        json={"config": '{"provider_id":"deepseek","model_id":"deepseek-reasoner"}'},
    )

    assert response.status_code == 200, response.text
    assert registry.calls == [(binding_id,)]

    async def inspect_rows() -> tuple[
        AgentBinding,
        AgentRuntimeBinding,
        AgentToken,
        AgentProviderDisclosureAcceptance,
        PanePolicy,
    ]:
        async with client.app.state.session_factory() as session:
            binding = await session.get(AgentBinding, binding_id)
            runtime = await session.scalar(
                select(AgentRuntimeBinding).where(AgentRuntimeBinding.binding_id == binding_id)
            )
            token = await session.scalar(
                select(AgentToken).where(AgentToken.binding_id == binding_id)
            )
            disclosure = await session.scalar(
                select(AgentProviderDisclosureAcceptance).where(
                    AgentProviderDisclosureAcceptance.binding_id == binding_id
                )
            )
            pane = await session.scalar(
                select(PanePolicy).where(PanePolicy.binding_id == binding_id)
            )
            assert binding is not None and runtime is not None and token is not None
            assert disclosure is not None and pane is not None
            return binding, runtime, token, disclosure, pane

    binding, runtime, token, disclosure, pane = client.portal.call(inspect_rows)
    assert binding.config_revision == 5
    assert binding.runtime_epoch == 7
    assert binding.runtime_ref == "runtime-old"
    assert binding.capability_ref == "cap-old"
    assert pane.pane_id == "%0" and pane.allowed is True
    assert token.binding_epoch == 7 and token.token_hash == "token-hash"
    assert disclosure.revoked_at is not None
    assert runtime.readiness == "blocked"
    assert runtime.reason_code == "binding_disclosure_stale"
    assert runtime.provider_readiness == "configured_unverified"
    assert runtime.provider_verified_revision is None
    assert runtime.provider_last_checked_at is None
    assert runtime.provider_reason_code is None


def test_profile_config_and_authority_invalidation_roll_back_together_on_name_conflict(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    first = _create_profile(client, admin_headers, "rollback-owner")
    second = _create_profile(client, admin_headers, "rollback-target")
    profile_id = UUID(str(second["profile_id"]))
    fingerprint = "b" * 64

    async def seed() -> UUID:
        repos = RepositoryBundle(client.app.state.session_factory)
        installation = await repos.installations.create("rollback-install-hash")
        term = await repos.instances.register_or_rotate(
            uuid4(), installation.id, "rollback-term", "rollback-term-hash"
        )
        binding = AgentBinding(
            profile_id=profile_id,
            term_id=term.id,
            status="enabled",
            runtime_ref="runtime-rollback",
            runtime_epoch=11,
            capability_ref="cap-rollback",
            config_revision=8,
        )
        now = datetime.now(UTC)
        async with client.app.state.session_factory() as session:
            session.add(binding)
            await session.flush()
            session.add(
                AgentRuntimeBinding(
                    binding_id=binding.id,
                    readiness="ready",
                    reason_code=None,
                    observed_runtime_ref="runtime-rollback",
                    observed_runtime_epoch=11,
                    observed_capability_ref="cap-rollback",
                    applied_revision=8,
                    config_fingerprint=fingerprint,
                    provider_readiness="verified",
                    provider_verified_revision=8,
                    provider_last_checked_at=now,
                )
            )
            session.add(
                AgentProviderDisclosureAcceptance(
                    binding_id=binding.id,
                    disclosure_fingerprint=fingerprint,
                    provider_id="deepseek",
                    model_id="deepseek-v4-flash",
                    endpoint_origin="https://api.deepseek.com",
                    region="global",
                    retention_terms="30 days",
                    retention_version="v1",
                    no_training=True,
                    policy_version="v1",
                    accepted_at=now,
                    accepted_auth_epoch=1,
                    actor_kind="root",
                    actor_ref="admin",
                )
            )
            await session.commit()
        return binding.id

    binding_id = client.portal.call(seed)
    conflict = client.patch(
        f"/api/v1/agent/admin/profiles/{profile_id}",
        headers=admin_headers,
        json={
            "display_name": first["display_name"],
            "config": '{"provider_id":"deepseek","model_id":"deepseek-reasoner"}',
        },
    )
    assert conflict.status_code == 409, conflict.text

    async def inspect() -> tuple[AgentProfile, AgentBinding, AgentRuntimeBinding, object]:
        async with client.app.state.session_factory() as session:
            profile = await session.get(AgentProfile, profile_id)
            binding = await session.get(AgentBinding, binding_id)
            runtime = await session.scalar(
                select(AgentRuntimeBinding).where(
                    AgentRuntimeBinding.binding_id == binding_id
                )
            )
            disclosure = await session.scalar(
                select(AgentProviderDisclosureAcceptance).where(
                    AgentProviderDisclosureAcceptance.binding_id == binding_id,
                    AgentProviderDisclosureAcceptance.revoked_at.is_(None),
                )
            )
            assert profile is not None and binding is not None and runtime is not None
            return profile, binding, runtime, disclosure

    profile, binding, runtime, disclosure = client.portal.call(inspect)
    assert profile.display_name == "rollback-target"
    assert profile.config == second["config"]
    assert binding.config_revision == 8
    assert runtime.readiness == "ready"
    assert runtime.provider_readiness == "verified"
    assert runtime.provider_verified_revision == 8
    assert disclosure is not None


def test_concurrent_profile_config_updates_preserve_revision_and_stale_authority(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    profile = _create_profile(client, admin_headers, "concurrent-config")
    profile_id = UUID(str(profile["profile_id"]))

    async def exercise() -> tuple[int, int, str, AgentRuntimeBinding]:
        repos = RepositoryBundle(client.app.state.session_factory)
        installation = await repos.installations.create("concurrent-install-hash")
        term = await repos.instances.register_or_rotate(
            uuid4(), installation.id, "concurrent-term", "concurrent-term-hash"
        )
        binding = AgentBinding(
            profile_id=profile_id,
            term_id=term.id,
            status="enabled",
            config_revision=4,
        )
        async with client.app.state.session_factory() as session:
            session.add(binding)
            await session.commit()

        updates = await asyncio.gather(
            _update_profile_and_invalidate_bindings(
                client.app.state.session_factory,
                profile_id,
                display_name=None,
                config='{"model_id":"deepseek-reasoner","provider_id":"deepseek"}',
            ),
            _update_profile_and_invalidate_bindings(
                client.app.state.session_factory,
                profile_id,
                display_name=None,
                config='{"model_id":"deepseek-chat","provider_id":"deepseek"}',
            ),
        )
        async with client.app.state.session_factory() as session:
            stored_profile = await session.get(AgentProfile, profile_id)
            stored_binding = await session.get(AgentBinding, binding.id)
            runtime = await session.scalar(
                select(AgentRuntimeBinding).where(
                    AgentRuntimeBinding.binding_id == binding.id
                )
            )
            assert stored_profile is not None and stored_binding is not None
            assert runtime is not None
            return (
                sum(bool(binding_ids) for _, binding_ids in updates),
                stored_binding.config_revision,
                stored_profile.config,
                runtime,
            )

    applied_count, revision, config, runtime = client.portal.call(exercise)
    assert applied_count == 2
    assert revision == 4 + applied_count
    assert config in {
        '{"model_id":"deepseek-reasoner","provider_id":"deepseek"}',
        '{"model_id":"deepseek-chat","provider_id":"deepseek"}',
    }
    assert runtime.readiness == "blocked"
    assert runtime.reason_code == "binding_disclosure_stale"
    assert runtime.provider_readiness == "configured_unverified"
