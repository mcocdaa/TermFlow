"""Static contracts for the durable, proxy-only agent live topology."""

# Exact Compose interpolation strings intentionally exceed the project line
# length; splitting them would weaken the literal contract under test.
# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / "deploy" / name).read_text(encoding="utf-8"))


def test_base_opencode_config_bind_never_creates_missing_host_path() -> None:
    base = _compose("compose.yaml")
    assert base["name"] == "termflow-v020-local"
    assert base["services"]["opencode-agent"]["volumes"][-1] == {
        "type": "bind",
        "source": "./opencode-config.yaml",
        "target": "/etc/termflow/opencode-config.yaml",
        "read_only": True,
        "bind": {"create_host_path": False},
    }


def test_live_opencode_has_only_two_internal_networks() -> None:
    live = _compose("compose.agent-live.yaml")
    assert live["services"]["opencode-agent"]["networks"] == [
        "agent_internal",
        "provider_egress",
    ]
    assert live["networks"]["agent_internal"] == {"internal": True}
    assert live["networks"]["provider_egress"] == {"internal": True}


def test_only_proxy_joins_provider_uplink() -> None:
    live = _compose("compose.agent-live.yaml")
    members = {
        service
        for service, definition in live["services"].items()
        if "provider_uplink" in definition.get("networks", [])
    }
    assert members == {"provider-egress-proxy"}
    assert live["networks"]["provider_uplink"] == {"internal": False}


def test_proxy_has_no_host_ports_and_no_agent_internal_membership() -> None:
    proxy = _compose("compose.agent-live.yaml")["services"]["provider-egress-proxy"]
    assert "ports" not in proxy
    assert proxy["networks"] == ["provider_egress", "provider_uplink"]
    assert "agent_internal" not in proxy["networks"]


def test_named_volumes_have_stable_explicit_names() -> None:
    volumes = _compose("compose.yaml")["volumes"]
    assert volumes == {
        "termflow-data": {"name": "termflow-v020-local-data"},
        "termflow-totp-key": {"name": "termflow-v020-local-totp-key"},
        "opencode-data": {"name": "termflow-v020-local-opencode-data"},
    }


def test_runtime_and_proxy_images_are_literal_digest_pins() -> None:
    base = _compose("compose.yaml")
    live = _compose("compose.agent-live.yaml")
    for image in (
        base["services"]["opencode-agent"]["image"],
        live["services"]["provider-egress-proxy"]["image"],
    ):
        repository, digest = image.split("@sha256:", maxsplit=1)
        assert repository and ":" in repository
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")


def test_squid_allows_only_deepseek_tls_connect_and_denies_everything_else() -> None:
    config = (ROOT / "deploy" / "provider-egress" / "squid.conf").read_text(encoding="utf-8")
    assert "pid_filename /run/squid/squid.pid" in config
    assert "acl deepseek dstdomain -n api.deepseek.com" in config
    assert "acl raw_ipv4 dstdom_regex -n" in config
    assert "acl raw_ipv6 dstdom_regex -n" in config
    assert "http_access allow manager localhost" in config
    assert "acl tls_port port 443" in config
    assert "http_access allow CONNECT deepseek tls_port" in config
    assert "http_access deny all" in config
    assert "acl Safe_ports" not in config
    assert "access_log none" in config
    assert "cache deny all" in config
    proxy_healthcheck = _compose("compose.agent-live.yaml")["services"][
        "provider-egress-proxy"
    ]["healthcheck"]["test"]
    assert proxy_healthcheck[:3] == ["CMD", "bash", "-ec"]
    assert "/dev/tcp/127.0.0.1/3128" in proxy_healthcheck[3]
    assert "case \"$$status\"" in proxy_healthcheck[3]
    assert "squidclient" not in proxy_healthcheck[3]


def test_opencode_config_selects_deepseek_without_literal_secret() -> None:
    config_text = (ROOT / "deploy" / "opencode-config.yaml").read_text(encoding="utf-8")
    config = json.loads(config_text)
    deepseek = config["provider"]["deepseek"]
    assert deepseek["options"] == {
        "baseURL": "https://api.deepseek.com",
        "apiKey": "{env:DEEPSEEK_API_KEY}",
    }
    assert set(deepseek["models"]) == {"deepseek-v4-flash"}
    assert "replace-with-deployment-secret" not in config_text
    assert "DEEPSEEK_API_KEY=" not in config_text


def test_live_environment_uses_native_secret_reference_not_rendered_value() -> None:
    environment = _compose("compose.agent-live.yaml")["services"]["opencode-agent"]["environment"]
    assert environment["DEEPSEEK_API_KEY"] == "${DEEPSEEK_API_KEY:?set DEEPSEEK_API_KEY}"
    assert environment["HTTPS_PROXY"] == "http://provider-egress-proxy:3128"
    assert environment["NO_PROXY"] == "control-plane,opencode-agent,localhost,127.0.0.1"
    assert environment["http_proxy"] == "http://provider-egress-proxy:3128"
    assert environment["https_proxy"] == "http://provider-egress-proxy:3128"
    assert environment["no_proxy"] == "control-plane,opencode-agent,localhost,127.0.0.1"
    assert all("compose-contract-only" not in str(value) for value in environment.values())


def test_live_control_plane_receives_complete_deepseek_catalog_policy() -> None:
    """The live profile must make B's server-owned catalog resolvable.

    A model/key in OpenCode alone is not a disclosure policy.  These values
    are consumed by Settings -> ProviderCatalog in B and therefore must be
    injected into the control-plane service as well.
    """
    live = _compose("compose.agent-live.yaml")
    environment = live["services"]["control-plane"]["environment"]
    required = {
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN": "${TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN:?set TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN}",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS": "${TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS:?set TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS}",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_REGION": "${TERMFLOW_AGENT_PROVIDER_DEEPSEEK_REGION:?set TERMFLOW_AGENT_PROVIDER_DEEPSEEK_REGION}",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS": "${TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS:?set TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS}",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_VERSION": "${TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_VERSION:?set TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_VERSION}",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING": "${TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING:?set TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING}",
        "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_POLICY_VERSION": "${TERMFLOW_AGENT_PROVIDER_DEEPSEEK_POLICY_VERSION:?set TERMFLOW_AGENT_PROVIDER_DEEPSEEK_POLICY_VERSION}",
    }
    assert environment == {**environment, **required}
    assert environment["TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE"] == "DEEPSEEK_API_KEY"


def test_live_mcp_capability_is_required_and_shared_by_b_and_opencode() -> None:
    base = _compose("compose.yaml")
    live = _compose("compose.agent-live.yaml")
    b_env = base["services"]["control-plane"]["environment"]
    runtime_env = base["services"]["opencode-agent"]["environment"]
    assert (
        b_env["TERMFLOW_AGENT_OPENCODE_MCP_TOKEN"]
        == "${OPENCODE_AGENT_MCP_TOKEN:?set OPENCODE_AGENT_MCP_TOKEN}"
    )
    assert (
        runtime_env["TERMFLOW_AGENT_MCP_TOKEN"]
        == "${OPENCODE_AGENT_MCP_TOKEN:?set OPENCODE_AGENT_MCP_TOKEN}"
    )
    assert (
        live["services"]["opencode-agent"]["environment"]["DEEPSEEK_API_KEY"]
        == "${DEEPSEEK_API_KEY:?set DEEPSEEK_API_KEY}"
    )


def test_control_plane_host_publish_uses_the_normal_default_network() -> None:
    base = _compose("compose.yaml")
    default = base["networks"].get("default", {})
    assert default["internal"] is False
    assert base["services"]["control-plane"]["networks"] == [
        "default",
        "agent_internal",
    ]
    assert base["services"]["control-plane"]["ports"] == [
        "${TERMFLOW_HOST_BIND:-127.0.0.1}:${TERMFLOW_HOST_PORT:-8765}:8000"
    ]


def test_container_reports_termflow_mcp_connected() -> None:
    """The opt-in runtime test must query OpenCode's authenticated MCP endpoint."""
    runtime_test = (ROOT / "tests" / "e2e" / "test_agent_opencode_container.py").read_text(
        encoding="utf-8"
    )
    assert "/mcp" in runtime_test
    assert "connected" in runtime_test


def test_disposable_agent_fixtures_clean_up_partial_compose_startup() -> None:
    """A failed startup may create only one resource class; it still belongs to the fixture."""

    for relative_path in (
        "tests/e2e/test_agent_opencode_container.py",
        "tests/e2e/test_agent_provider_egress.py",
    ):
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "if containers or volumes or networks:" in content
        assert "if containers and volumes and networks:" not in content


def test_disposable_agent_fixtures_never_claim_the_stable_host_port() -> None:
    """A disposable stack must coexist with the named local deployment."""

    for relative_path in (
        "tests/e2e/test_agent_opencode_container.py",
        "tests/e2e/test_agent_provider_egress.py",
    ):
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert '"TERMFLOW_HOST_PORT": str(_free_port())' in content
