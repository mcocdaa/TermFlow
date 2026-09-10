"""Product setup/binding API contract tests for v0.2.0.

These tests intentionally exercise the HTTP boundary rather than repository
implementation details.  Setup requests and mutation bodies are strict,
responses contain only public aggregate state, and authority-changing routes
delegate fencing/reconciliation to the runtime controller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from termflow_control_plane.api.agent_admin import router as agent_admin_router
from termflow_control_plane.api.dependencies import require_fresh_admin
from termflow_control_plane.app import create_app
from termflow_control_plane.auth.context import AdminAuthContext
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogEntry,
)
from termflow_control_plane.plugins.agent_broker.agent.provisioning import (
    AgentProvisioningService,
)


def _setup_payload(term_id: UUID, *, profile_id: UUID | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "term_id": str(term_id),
        "pane_ids": ["%0"],
        "topology_revision": 3,
        "disclosure_fingerprint": "a" * 64,
        "accepted": True,
        "idempotency_key": str(uuid4()),
    }
    if profile_id is None:
        payload["profile_display_name"] = "deepseek"
    else:
        payload["profile_id"] = str(profile_id)
    return payload


def test_setup_request_requires_exactly_one_profile_selector() -> None:
    from termflow_control_plane.api.agent_admin import AgentSetupRequest

    payload = _setup_payload(uuid4())
    with pytest.raises(ValidationError):
        AgentSetupRequest.model_validate({**payload, "profile_id": str(uuid4())})
    with pytest.raises(ValidationError):
        AgentSetupRequest.model_validate(
            {key: value for key, value in payload.items() if key != "profile_display_name"}
        )


def test_unconfigured_setup_exposes_server_selected_disclosure(
    client, admin_headers, provision_term
):
    from termflow_control_plane.agent_contracts import AgentProfileConfig

    term = provision_term(name="preview")
    catalog = ProviderCatalog(
        (
            ProviderCatalogEntry(
                provider_id="deepseek",
                model_ids=frozenset({"deepseek-v4-flash"}),
                endpoint_origin="https://api.deepseek.com",
                region="global",
                retention_terms="30 days",
                retention_version="v1",
                no_training=True,
                credential_source="DEEPSEEK_API_KEY",
                policy_version="v1",
            ),
        )
    )
    client.app.state.agent_provider_catalog = catalog
    response = client.get(
        f"/api/v1/agent/admin/setup?term_id={term.instance_id}", headers=admin_headers
    )
    assert response.status_code == 200
    disclosure = response.json()["disclosure"]
    assert disclosure is not None
    assert disclosure["binding_id"] is None and disclosure["accepted"] is False
    assert disclosure["disclosure_fingerprint"] == catalog.disclosure_fingerprint(
        AgentProfileConfig(provider_id="deepseek", model_id="deepseek-v4-flash")
    )
    client.app.state.agent_provider_catalog = ProviderCatalog(())
    unavailable = client.get(
        f"/api/v1/agent/admin/setup?term_id={term.instance_id}", headers=admin_headers
    )
    assert unavailable.json()["disclosure"] is None
    assert unavailable.json()["reason_code"] == "provider_selection_unavailable"


def test_setup_request_rejects_extra_fields_and_invalid_disclosure() -> None:
    from termflow_control_plane.api.agent_admin import AgentSetupRequest

    payload = _setup_payload(uuid4())
    with pytest.raises(ValidationError):
        AgentSetupRequest.model_validate({**payload, "bootstrap_secret": "do-not-accept"})
    with pytest.raises(ValidationError):
        AgentSetupRequest.model_validate({**payload, "disclosure_fingerprint": "x"})


def test_setup_response_never_contains_raw_token_or_provider_key() -> None:
    from termflow_control_plane.api.agent_admin import (
        AgentSetupProfileSummary,
        AgentSetupResponse,
        AgentSetupTokenSummary,
    )

    response = AgentSetupResponse(
        state="activating",
        term_id=uuid4(),
        binding_id=None,
        profile=None,
        token=AgentSetupTokenSummary(installed=True, expires_at=None),
        runtime=None,
        pane_policy=None,
        disclosure=None,
        topology_revision=3,
        profiles=[
            AgentSetupProfileSummary(
                profile_id=uuid4(),
                display_name="deepseek",
                backend_kind="opencode",
                provider_id="deepseek",
                model_id="deepseek-v4-flash",
            )
        ],
        reason_code=None,
    )
    dumped = response.model_dump(mode="json")
    forbidden = {"raw_token", "token", "api_key", "provider_key", "password", "secret"}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    # ``token`` is the safe summary field; inspect keys below its value, not
    # the public aggregate field name itself.
    assert dumped["token"] == {"installed": True, "expires_at": None}
    walk({key: value for key, value in dumped.items() if key != "token"})


def test_legacy_patch_only_accepts_safety_reducing_statuses() -> None:
    from termflow_control_plane.api.agent_admin import AgentBindingUpdateRequest

    assert AgentBindingUpdateRequest.model_validate({}).status is None
    assert AgentBindingUpdateRequest.model_validate({"status": "disabled"}).status == "disabled"
    assert AgentBindingUpdateRequest.model_validate({"status": "revoked"}).status == "revoked"

    for payload in (
        {"status": "enabled"},
        {"status": "pending"},
        {"status": "ready"},
        {
            "status": "ready",
            "runtime_ref": "runtime-1",
            "runtime_epoch": 9,
            "capability_ref": "cap-1",
        },
        {"runtime_ref": "runtime-1", "runtime_epoch": 9, "capability_ref": "cap-1"},
        {"runtime_epoch": None},
    ):
        with pytest.raises(ValidationError):
            AgentBindingUpdateRequest.model_validate(payload)

    with pytest.raises(ValidationError):
        AgentBindingUpdateRequest.model_validate({"runtime_epoch": 9})
    with pytest.raises(ValidationError):
        AgentBindingUpdateRequest.model_validate({"observed_runtime_epoch": 9})


def test_runtime_update_request_does_not_accept_epoch() -> None:
    from termflow_control_plane.api.agent_admin import AgentBindingRuntimeUpdateRequest

    with pytest.raises(ValidationError):
        AgentBindingRuntimeUpdateRequest.model_validate(
            {
                "runtime_ref": "runtime-1",
                "capability_ref": "cap-1",
                "expected_revision": 1,
                "runtime_epoch": 2,
            }
        )

    with pytest.raises(ValidationError):
        AgentBindingRuntimeUpdateRequest.model_validate(
            {
                "runtime_ref": "runtime-1",
                "capability_ref": "cap-1",
                "expected_revision": 1,
                "rotate_epoch": True,
            }
        )


def test_disclosure_accept_request_is_strict_and_true_only() -> None:
    from termflow_control_plane.api.agent_admin import AgentDisclosureAcceptRequest

    with pytest.raises(ValidationError):
        AgentDisclosureAcceptRequest.model_validate(
            {"disclosure_fingerprint": "a" * 64, "accepted": False}
        )
    with pytest.raises(ValidationError):
        AgentDisclosureAcceptRequest.model_validate(
            {"disclosure_fingerprint": "a" * 64, "accepted": True, "secret": "x"}
        )


class _FenceRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, bool]] = []

    async def fence(self, binding_id: UUID, rotate_epoch: bool = False) -> object:
        self.calls.append((binding_id, rotate_epoch))
        return object()


def test_fence_recorder_shape_is_synchronous_contract() -> None:
    # Kept as a tiny executable contract so endpoint tests can use the same
    # controller seam without depending on a concrete runtime implementation.
    recorder = _FenceRecorder()
    binding_id = uuid4()
    import asyncio

    asyncio.run(recorder.fence(binding_id, False))
    assert recorder.calls == [(binding_id, False)]


class _Controller:
    def __init__(self) -> None:
        self.fence_calls: list[tuple[UUID, bool]] = []
        self.reconcile_calls: list[UUID] = []

    async def fence(self, binding_id: UUID, rotate_epoch: bool = False) -> object:
        self.fence_calls.append((binding_id, rotate_epoch))
        return object()

    async def reconcile(self, binding_id: UUID) -> object:
        self.reconcile_calls.append(binding_id)
        return type("Result", (), {"readiness": "ready", "reason_code": None})()


def _integration_settings(path: Any) -> Settings:
    return Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{path / 'setup-api.db'}",
        allow_insecure_loopback=True,
        agent_broker_enabled=False,
        agent_opencode_mcp_token="bootstrap-token-for-setup-tests",
        agent_provider_deepseek_region="global",
        agent_provider_deepseek_retention_terms="no more than 30 days",
        agent_provider_deepseek_retention_version="2026-09-01",
        agent_provider_deepseek_no_training=True,
        agent_provider_deepseek_credential_source="DEEPSEEK_API_KEY",
        agent_provider_deepseek_policy_version="2026-09-01",
    )


@pytest.fixture
def setup_client(tmp_path: Any) -> Any:
    settings = _integration_settings(tmp_path)
    database = Database(settings.database_url)
    app = create_app(settings=settings, database=database)
    app.include_router(agent_admin_router)
    controller = _Controller()
    catalog = ProviderCatalog(
        [
            ProviderCatalogEntry(
                provider_id="deepseek",
                model_ids=frozenset({"deepseek-v4-flash"}),
                endpoint_origin="https://api.deepseek.com",
                region="global",
                retention_terms="no more than 30 days",
                retention_version="2026-09-01",
                no_training=True,
                credential_source="DEEPSEEK_API_KEY",
                policy_version="2026-09-01",
            )
        ]
    )
    app.state.agent_provider_catalog = catalog
    app.state.agent_runtime_controller = controller
    app.state.agent_topology_provider = lambda _term_id: {
        "revision": 3,
        "pane_ids": ["%0", "%1"],
    }
    app.dependency_overrides[require_fresh_admin] = lambda: AdminAuthContext(
        "root", "root", 1, datetime.now(UTC)
    )
    with TestClient(app) as client:
        # The disabled-plugin test seam still has a fully initialized
        # repository bundle after lifespan startup.
        app.state.agent_provisioning = AgentProvisioningService(
            repositories=app.state.repositories,
            sessions=app.state.session_factory,
            topology=app.state.agent_topology_provider,
            catalog=catalog,
            bootstrap_secret="bootstrap-token-for-setup-tests",
            controller=controller,
        )
        yield client, controller, catalog
    app.dependency_overrides.clear()


def _seed_term_sync(client: TestClient) -> UUID:
    async def seed() -> UUID:
        repos = RepositoryBundle(client.app.state.session_factory)
        installation = await repos.installations.create(digest_secret("setup-api-install"))
        term = await repos.instances.register_or_rotate(
            uuid4(), installation.id, "setup-api-term", digest_secret("setup-api-term")
        )
        return term.id

    return client.portal.call(seed)


def test_setup_http_returns_activation_without_secret_fields(setup_client: Any) -> None:
    client, _controller, catalog = setup_client
    term_id = _seed_term_sync(client)
    profile = client.post(
        "/api/v1/agent/admin/profiles",
        headers={"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"},
        json={
            "display_name": "setup-api-profile",
            "backend_kind": "opencode",
            "config": '{"provider_id":"deepseek","model_id":"deepseek-v4-flash"}',
        },
    )
    assert profile.status_code == 201, profile.text
    profile_id = UUID(profile.json()["profile_id"])
    fingerprint = catalog.disclosure_fingerprint(
        type("Config", (), {"provider_id": "deepseek", "model_id": "deepseek-v4-flash"})()
    )
    response = client.post(
        "/api/v1/agent/admin/setup",
        headers={"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"},
        json={
            **_setup_payload(term_id, profile_id=profile_id),
            "disclosure_fingerprint": fingerprint,
        },
    )
    assert response.status_code in {200, 202}, response.text
    dumped = response.json()
    assert dumped["term_id"] == str(term_id)
    assert "raw_token" not in response.text
    assert "bootstrap-token-for-setup-tests" not in response.text
    assert "DEEPSEEK_API_KEY" in response.text


def test_disable_http_fences_before_returning(setup_client: Any) -> None:
    client, controller, _catalog = setup_client
    term_id = _seed_term_sync(client)
    profile = client.post(
        "/api/v1/agent/admin/profiles",
        headers={"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"},
        json={
            "display_name": "disable-api-profile",
            "backend_kind": "opencode",
            "config": '{"provider_id":"deepseek","model_id":"deepseek-v4-flash"}',
        },
    )
    profile_id = UUID(profile.json()["profile_id"])
    binding = client.post(
        "/api/v1/agent/admin/bindings",
        headers={"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"},
        json={"profile_id": str(profile_id), "term_id": str(term_id)},
    )
    binding_id = UUID(binding.json()["binding_id"])
    response = client.post(
        f"/api/v1/agent/admin/bindings/{binding_id}/disable",
        headers={"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"},
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"
    assert controller.fence_calls[-1] == (binding_id, False)


def test_runtime_update_validates_server_policy_and_rotates_identity_once(
    setup_client: Any,
) -> None:
    client, controller, _catalog = setup_client
    term_id = _seed_term_sync(client)
    headers = {"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"}
    profile = client.post(
        "/api/v1/agent/admin/profiles",
        headers=headers,
        json={
            "display_name": "runtime-update-profile",
            "backend_kind": "opencode",
            "config": '{"provider_id":"deepseek","model_id":"deepseek-v4-flash"}',
        },
    )
    binding = client.post(
        "/api/v1/agent/admin/bindings",
        headers=headers,
        json={"profile_id": profile.json()["profile_id"], "term_id": str(term_id)},
    )
    binding_id = UUID(binding.json()["binding_id"])
    path = f"/api/v1/agent/admin/bindings/{binding_id}/runtime"
    identity = {
        "runtime_ref": "opencode-agent",
        "capability_ref": f"termflow-mcp:{binding_id}",
    }

    first = client.put(path, headers=headers, json={**identity, "expected_revision": 1})
    assert first.status_code == 202, first.text
    assert first.json()["runtime_epoch"] == 1
    assert first.json()["config_revision"] == 2

    same = client.put(path, headers=headers, json={**identity, "expected_revision": 2})
    assert same.status_code == 202, same.text
    assert same.json()["runtime_epoch"] == 1
    assert same.json()["config_revision"] == 3

    forged = client.put(
        path,
        headers=headers,
        json={
            "runtime_ref": "client-invented",
            "capability_ref": "client-invented",
            "expected_revision": 3,
        },
    )
    assert forged.status_code == 409, forged.text
    assert forged.json()["error"]["code"] == "runtime_assignment_conflict"
    assert controller.fence_calls[-1] == (binding_id, False)
