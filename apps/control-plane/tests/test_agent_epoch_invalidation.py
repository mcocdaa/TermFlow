from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient
from termflow_control_plane.persistence.models import AgentBinding
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    RuntimeShutdownError,
)


def _profile(client: TestClient, headers: dict[str, str]) -> UUID:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=headers,
        json={
            "display_name": "epoch-profile",
            "backend_kind": "opencode",
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["profile_id"])


def _binding(
    client: TestClient,
    headers: dict[str, str],
    provision_term,
    *,
    profile_id: UUID,
    epoch: int,
) -> UUID:
    term = provision_term(name="epoch-term")
    created = client.post(
        "/api/v1/agent/admin/bindings",
        headers=headers,
        json={"profile_id": str(profile_id), "term_id": str(term.instance_id)},
    )
    assert created.status_code == 201, created.text
    binding_id = UUID(created.json()["binding_id"])

    async def seed_runtime_authority() -> None:
        # Runtime identity is server-owned.  These epoch-focused tests seed
        # the repository aggregate directly instead of reviving the legacy
        # PATCH authority surface that production now rejects.
        async with client.app.state.session_factory() as session:
            binding = await session.get(AgentBinding, binding_id)
            assert binding is not None
            binding.status = "enabled"
            binding.runtime_ref = f"runtime-{binding_id}"
            binding.runtime_epoch = epoch
            binding.capability_ref = f"capability-{binding_id}"
            await session.commit()

    client.portal.call(seed_runtime_authority)
    return binding_id


def test_closing_binding_advances_runtime_epoch_once(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> None:
    profile_id = _profile(client, admin_headers)
    binding_id = _binding(
        client,
        admin_headers,
        provision_term,
        profile_id=profile_id,
        epoch=7,
    )

    revoked = client.patch(
        f"/api/v1/agent/admin/bindings/{binding_id}",
        headers=admin_headers,
        json={"status": "revoked"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["runtime_epoch"] == 8

    repeated = client.patch(
        f"/api/v1/agent/admin/bindings/{binding_id}",
        headers=admin_headers,
        json={"status": "revoked"},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["runtime_epoch"] == 8


def test_closing_binding_cannot_replace_the_runtime_epoch_in_the_same_patch(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> None:
    profile_id = _profile(client, admin_headers)
    binding_id = _binding(
        client,
        admin_headers,
        provision_term,
        profile_id=profile_id,
        epoch=4,
    )

    response = client.patch(
        f"/api/v1/agent/admin/bindings/{binding_id}",
        headers=admin_headers,
        json={
            "status": "revoked",
            "runtime_ref": f"runtime-{binding_id}",
            "runtime_epoch": 99,
            "capability_ref": f"capability-{binding_id}",
        },
    )
    assert response.status_code == 422, response.text
    unchanged = client.get(
        f"/api/v1/agent/admin/bindings/{binding_id}",
        headers=admin_headers,
    )
    assert unchanged.json()["status"] == "enabled"
    assert unchanged.json()["runtime_epoch"] == 4


def test_profile_config_change_and_rename_preserve_runtime_epoch(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> None:
    profile_id = _profile(client, admin_headers)
    binding_id = _binding(
        client,
        admin_headers,
        provision_term,
        profile_id=profile_id,
        epoch=3,
    )

    configured = client.patch(
        f"/api/v1/agent/admin/profiles/{profile_id}",
        headers=admin_headers,
        json={"config": '{"model_id":"deepseek-reasoner","provider_id":"deepseek"}'},
    )
    assert configured.status_code == 200, configured.text
    after_config = client.get(
        f"/api/v1/agent/admin/bindings/{binding_id}",
        headers=admin_headers,
    )
    assert after_config.status_code == 200, after_config.text
    assert after_config.json()["runtime_epoch"] == 3

    renamed = client.patch(
        f"/api/v1/agent/admin/profiles/{profile_id}",
        headers=admin_headers,
        json={"display_name": "renamed-profile"},
    )
    assert renamed.status_code == 200, renamed.text
    after_rename = client.get(
        f"/api/v1/agent/admin/bindings/{binding_id}",
        headers=admin_headers,
    )
    assert after_rename.status_code == 200, after_rename.text
    assert after_rename.json()["runtime_epoch"] == 3


def test_profile_config_update_stops_all_affected_bindings_in_one_batch(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> None:
    class RecordingRegistry:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID, ...]] = []

        async def stop_bindings(self, binding_ids) -> int:
            captured = tuple(binding_ids)
            self.calls.append(captured)
            return len(captured)

        async def stop_binding(self, binding_id: UUID) -> bool:
            raise AssertionError(f"single stop must not be called for {binding_id}")

    profile_id = _profile(client, admin_headers)
    first = _binding(
        client,
        admin_headers,
        provision_term,
        profile_id=profile_id,
        epoch=3,
    )
    second = _binding(
        client,
        admin_headers,
        provision_term,
        profile_id=profile_id,
        epoch=4,
    )
    registry = RecordingRegistry()
    client.app.state.agent_runtime_registry = registry
    client.app.state.agent_runtime_controller = None

    response = client.patch(
        f"/api/v1/agent/admin/profiles/{profile_id}",
        headers=admin_headers,
        json={"config": '{"model_id":"deepseek-reasoner","provider_id":"deepseek"}'},
    )

    assert response.status_code == 200, response.text
    assert len(registry.calls) == 1
    assert set(registry.calls[0]) == {first, second}


def test_profile_config_update_returns_stable_error_when_batch_shutdown_fails(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> None:
    class FailingRegistry:
        async def stop_bindings(self, binding_ids) -> int:
            assert tuple(binding_ids)
            raise RuntimeShutdownError(1)

    profile_id = _profile(client, admin_headers)
    _binding(
        client,
        admin_headers,
        provision_term,
        profile_id=profile_id,
        epoch=3,
    )
    client.app.state.agent_runtime_registry = FailingRegistry()
    client.app.state.agent_runtime_controller = None

    response = client.patch(
        f"/api/v1/agent/admin/profiles/{profile_id}",
        headers=admin_headers,
        json={"config": '{"model_id":"deepseek-reasoner","provider_id":"deepseek"}'},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "runtime_shutdown_failed"
    assert "secret" not in response.text
