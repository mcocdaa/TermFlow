from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from termflow_control_plane.agent_contracts import AgentProfileConfig
from termflow_control_plane.config import Settings
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.plugins.agent_broker.agent.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogEntry,
    canonicalize_profile_config,
)

CANONICAL_CONFIG = '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}'


def _catalog_entry(**overrides: object) -> ProviderCatalogEntry:
    values: dict[str, object] = {
        "provider_id": "deepseek",
        "model_ids": frozenset({"deepseek-v4-flash", "deepseek-reasoner"}),
        "endpoint_origin": "https://api.deepseek.com",
        "region": "global",
        "retention_terms": "prompts retained for no more than 30 days",
        "retention_version": "2026-09-01",
        "no_training": True,
        "credential_source": "OPENAI_API_KEY",
        "policy_version": "2026-08-01",
    }
    values.update(overrides)
    return ProviderCatalogEntry(**values)  # type: ignore[arg-type]


def _complete_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "admin_token": "admin-token-that-is-long-enough-for-tests",
        "agent_provider_deepseek_endpoint_origin": "https://api.deepseek.com",
        "agent_provider_deepseek_model_ids": ("deepseek-v4-flash",),
        "agent_provider_deepseek_region": "global",
        "agent_provider_deepseek_retention_terms": "no more than 30 days",
        "agent_provider_deepseek_retention_version": "2026-09-01",
        "agent_provider_deepseek_no_training": True,
        "agent_provider_deepseek_credential_source": "OPENAI_API_KEY",
        "agent_provider_deepseek_policy_version": "2026-08-01",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _assert_stable_error(call, code: str, status_code: int = 422) -> TermFlowError:
    with pytest.raises(TermFlowError) as captured:
        call()
    assert captured.value.code == code
    assert captured.value.status_code == status_code
    return captured.value


def test_profile_config_accepts_only_canonical_provider_and_model() -> None:
    parsed, encoded = canonicalize_profile_config(
        {"provider_id": "deepseek", "model_id": "deepseek-v4-flash"}
    )

    assert parsed.provider_id == "deepseek"
    assert parsed.model_id == "deepseek-v4-flash"
    assert encoded == CANONICAL_CONFIG


def test_profile_config_accepts_an_existing_json_string() -> None:
    parsed, encoded = canonicalize_profile_config(
        '{ "provider_id": "deepseek", "model_id": "deepseek-v4-flash" }'
    )

    assert parsed == AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )
    assert encoded == CANONICAL_CONFIG


def test_profile_config_is_frozen() -> None:
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    with pytest.raises(ValidationError):
        config.model_id = "deepseek-reasoner"


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "{}",
        "null",
        {"provider_id": "", "model_id": "deepseek-v4-flash"},
        {"provider_id": "deepseek", "model_id": ""},
        {
            "provider_id": "deepseek",
            "model_id": "deepseek-v4-flash",
            "endpoint": "https://attacker.invalid",
        },
    ],
)
def test_invalid_profile_config_maps_to_one_stable_error(value: object) -> None:
    error = _assert_stable_error(
        lambda: canonicalize_profile_config(value), "invalid_profile_config"
    )

    assert "attacker.invalid" not in error.message
    assert "not-json" not in error.message


def test_profile_config_enforces_field_length_bounds() -> None:
    _assert_stable_error(
        lambda: canonicalize_profile_config(
            {"provider_id": "p" * 65, "model_id": "model"}
        ),
        "invalid_profile_config",
    )
    _assert_stable_error(
        lambda: canonicalize_profile_config(
            {"provider_id": "provider", "model_id": "m" * 129}
        ),
        "invalid_profile_config",
    )


def test_provider_catalog_resolves_known_provider_and_model() -> None:
    entry = _catalog_entry()
    catalog = ProviderCatalog([entry])
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    assert catalog.resolve(config) is entry


def test_provider_catalog_rejects_duplicate_provider_ids() -> None:
    with pytest.raises(ValueError, match="duplicate provider_id"):
        ProviderCatalog([_catalog_entry(), _catalog_entry()])


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_id": ""},
        {"provider_id": " deepseek"},
        {"provider_id": "deepseek "},
        {"provider_id": "p" * 65},
        {"model_ids": frozenset()},
        {"model_ids": frozenset({""})},
        {"model_ids": frozenset({" deepseek-v4-flash"})},
        {"model_ids": frozenset({"deepseek-v4-flash "})},
        {"model_ids": frozenset({"m" * 129})},
    ],
)
def test_provider_catalog_rejects_invalid_direct_provider_or_models(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        ProviderCatalog([_catalog_entry(**overrides)])


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        (field_name, value)
        for field_name in (
            "region",
            "retention_terms",
            "retention_version",
            "policy_version",
            "credential_source",
        )
        for value in ("", " leading", "trailing ")
    ],
)
def test_provider_catalog_rejects_invalid_direct_disclosure_text(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        ProviderCatalog([_catalog_entry(**{field_name: value})])


@pytest.mark.parametrize(
    "endpoint_origin",
    [
        "ftp://api.deepseek.com",
        "https://user:placeholder@api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com?token=placeholder",
        "https://api.deepseek.com#fragment",
    ],
)
def test_provider_catalog_rejects_invalid_direct_endpoint_origin(
    endpoint_origin: str,
) -> None:
    with pytest.raises(ValueError) as captured:
        ProviderCatalog([_catalog_entry(endpoint_origin=endpoint_origin)])

    assert "placeholder" not in str(captured.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"no_training": False},
        {"credential_source": "CUSTOM_PROVIDER_TOKEN"},
        {"credential_source": "literal-secret-placeholder"},
    ],
)
def test_provider_catalog_rejects_unsafe_direct_policy(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError) as captured:
        ProviderCatalog([_catalog_entry(**overrides)])

    assert "literal-secret-placeholder" not in str(captured.value)


@pytest.mark.parametrize(
    "config",
    [
        AgentProfileConfig(provider_id="unknown", model_id="deepseek-v4-flash"),
        AgentProfileConfig(provider_id="deepseek", model_id="unknown-model"),
    ],
)
def test_provider_catalog_fails_closed_for_unknown_provider_or_model(
    config: AgentProfileConfig,
) -> None:
    catalog = ProviderCatalog([_catalog_entry()])

    error = _assert_stable_error(lambda: catalog.resolve(config), "unknown_provider")

    assert config.provider_id not in error.message
    assert config.model_id not in error.message


@pytest.mark.parametrize(
    ("field_name", "new_value"),
    [
        ("endpoint_origin", "https://new-api.deepseek.com"),
        ("region", "apac"),
        ("retention_terms", "prompts are not retained"),
        ("retention_version", "2026-09-02"),
        ("credential_source", "DEEPSEEK_API_KEY"),
        ("policy_version", "2026-09-02"),
    ],
)
def test_disclosure_fingerprint_changes_for_every_server_owned_disclosure_field(
    field_name: str,
    new_value: object,
) -> None:
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )
    entry = _catalog_entry()
    first = ProviderCatalog([entry]).disclosure_fingerprint(config)
    changed = replace(entry, **{field_name: new_value})

    assert ProviderCatalog([changed]).disclosure_fingerprint(config) != first


def test_disclosure_fingerprint_changes_with_catalog_provider_and_models() -> None:
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )
    first_entry = _catalog_entry()
    first = ProviderCatalog([first_entry]).disclosure_fingerprint(config)

    renamed_config = AgentProfileConfig(
        provider_id="deepseek-renamed", model_id="deepseek-v4-flash"
    )
    renamed_entry = replace(first_entry, provider_id="deepseek-renamed")
    expanded_entry = replace(
        first_entry,
        model_ids=first_entry.model_ids | {"deepseek-chat"},
    )

    assert ProviderCatalog([renamed_entry]).disclosure_fingerprint(renamed_config) != first
    assert ProviderCatalog([expanded_entry]).disclosure_fingerprint(config) != first


def test_disclosure_fingerprint_changes_with_selected_model() -> None:
    catalog = ProviderCatalog([_catalog_entry()])
    flash = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )
    reasoner = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-reasoner"
    )

    assert catalog.disclosure_fingerprint(flash) != catalog.disclosure_fingerprint(reasoner)


def test_fingerprint_includes_credential_source_name_but_no_credential() -> None:
    raw_credential = "raw-provider-secret-must-never-leak"
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )
    first = ProviderCatalog(
        [_catalog_entry(credential_source="OPENAI_API_KEY")]
    ).disclosure_fingerprint(config)
    second = ProviderCatalog(
        [_catalog_entry(credential_source="DEEPSEEK_API_KEY")]
    ).disclosure_fingerprint(config)

    assert first != second
    assert raw_credential not in first
    assert not hasattr(_catalog_entry(), "credential")


@pytest.mark.parametrize("no_training", [None, False])
def test_settings_catalog_is_fail_closed_until_disclosure_is_complete(
    no_training: bool | None,
) -> None:
    incomplete = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        agent_provider_deepseek_region="global",
        agent_provider_deepseek_retention_terms="no more than 30 days",
        agent_provider_deepseek_retention_version="2026-09-01",
        agent_provider_deepseek_no_training=no_training,
    )
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    _assert_stable_error(
        lambda: ProviderCatalog.from_settings(incomplete).resolve(config),
        "unknown_provider",
    )


def test_provider_policy_version_has_no_default_claim() -> None:
    settings = Settings(admin_token="admin-token-that-is-long-enough-for-tests")

    assert settings.agent_provider_deepseek_policy_version is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        (field_name, value)
        for field_name in (
            "agent_provider_deepseek_region",
            "agent_provider_deepseek_retention_terms",
            "agent_provider_deepseek_retention_version",
            "agent_provider_deepseek_policy_version",
            "agent_provider_deepseek_credential_source",
        )
        for value in (None, "", "   ")
    ],
)
def test_settings_catalog_fails_closed_for_incomplete_disclosure_text(
    field_name: str,
    value: str | None,
) -> None:
    settings = _complete_settings(**{field_name: value})
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    _assert_stable_error(
        lambda: ProviderCatalog.from_settings(settings).resolve(config),
        "unknown_provider",
    )


@pytest.mark.parametrize(
    "credential_source",
    ["CUSTOM_PROVIDER_TOKEN", "literal-secret-placeholder"],
)
def test_settings_catalog_fails_closed_for_unknown_or_raw_credential_source(
    credential_source: str,
) -> None:
    settings = _complete_settings(
        agent_provider_deepseek_credential_source=credential_source
    )
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    _assert_stable_error(
        lambda: ProviderCatalog.from_settings(settings).resolve(config),
        "unknown_provider",
    )


def test_settings_catalog_normalizes_server_owned_disclosure_text() -> None:
    settings = _complete_settings(
        agent_provider_deepseek_region=" global ",
        agent_provider_deepseek_retention_terms=" no more than 30 days ",
        agent_provider_deepseek_retention_version=" 2026-09-01 ",
        agent_provider_deepseek_credential_source=" OPENAI_API_KEY ",
        agent_provider_deepseek_policy_version=" 2026-08-01 ",
    )
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    entry = ProviderCatalog.from_settings(settings).resolve(config)

    assert entry.region == "global"
    assert entry.retention_terms == "no more than 30 days"
    assert entry.retention_version == "2026-09-01"
    assert entry.credential_source == "OPENAI_API_KEY"
    assert entry.policy_version == "2026-08-01"


def test_settings_catalog_normalizes_and_deduplicates_model_ids() -> None:
    settings = _complete_settings(
        agent_provider_deepseek_model_ids=(
            " deepseek-v4-flash ",
            "deepseek-v4-flash",
        )
    )
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    entry = ProviderCatalog.from_settings(settings).resolve(config)

    assert entry.model_ids == frozenset({"deepseek-v4-flash"})


@pytest.mark.parametrize("model_ids", [(), ("",), ("   ",)])
def test_settings_catalog_fails_closed_for_empty_model_ids(
    model_ids: tuple[str, ...],
) -> None:
    settings = _complete_settings(agent_provider_deepseek_model_ids=model_ids)
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    _assert_stable_error(
        lambda: ProviderCatalog.from_settings(settings).resolve(config),
        "unknown_provider",
    )


def test_settings_constructs_complete_server_owned_deepseek_catalog_entry() -> None:
    settings = _complete_settings()
    config = AgentProfileConfig(
        provider_id="deepseek", model_id="deepseek-v4-flash"
    )

    entry = ProviderCatalog.from_settings(settings).resolve(config)

    assert entry == _catalog_entry(
        model_ids=frozenset({"deepseek-v4-flash"}),
        retention_terms="no more than 30 days",
    )
    assert not hasattr(entry, "credential")


def test_profile_api_requires_config_in_request_and_openapi(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={"display_name": "missing-config", "backend_kind": "opencode"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    schema = client.app.openapi()["components"]["schemas"]["AgentProfileCreateRequest"]
    assert "config" in schema["required"]


def test_profile_api_create_stores_canonical_config_without_server_disclosure(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "canonical-create",
            "backend_kind": "opencode",
            "config": '{ "provider_id": "deepseek", "model_id": "deepseek-v4-flash" }',
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["config"] == CANONICAL_CONFIG
    assert "api.deepseek.com" not in response.text
    assert "OPENAI_API_KEY" not in response.text

    profile_id = UUID(response.json()["profile_id"])
    stored = client.portal.call(
        client.app.state.repositories.agent_profiles.get_by_id, profile_id
    )
    assert stored is not None
    assert stored.config == CANONICAL_CONFIG


def test_profile_api_update_stores_canonical_config(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    created = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "canonical-update",
            "backend_kind": "opencode",
            "config": CANONICAL_CONFIG,
        },
    )
    assert created.status_code == 201, created.text
    profile_id = UUID(created.json()["profile_id"])

    updated = client.patch(
        f"/api/v1/agent/admin/profiles/{profile_id}",
        headers=admin_headers,
        json={
            "config": '{"provider_id":"deepseek", "model_id":"deepseek-reasoner"}'
        },
    )

    assert updated.status_code == 200, updated.text
    expected = '{"model_id":"deepseek-reasoner","provider_id":"deepseek"}'
    assert updated.json()["config"] == expected
    stored = client.portal.call(
        client.app.state.repositories.agent_profiles.get_by_id, profile_id
    )
    assert stored is not None
    assert stored.config == expected


@pytest.mark.parametrize(
    "config",
    [
        "not-json",
        "{}",
        '{"provider_id":"deepseek","model_id":"","credential":"do-not-echo"}',
    ],
)
def test_profile_api_returns_stable_error_for_invalid_config(
    client: TestClient,
    admin_headers: dict[str, str],
    config: str,
) -> None:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "invalid-config",
            "backend_kind": "opencode",
            "config": config,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_profile_config"
    assert "do-not-echo" not in response.text
