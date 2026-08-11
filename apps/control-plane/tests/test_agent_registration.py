"""Agent Broker plugin registration and capability-discovery tests (M1.2).

Covers plan M1: registering the ``agent_broker`` feature plugin from the B
composition root, explicit enable/disable configuration, deterministic
lifespan startup/shutdown, and a core capability-discovery response that keeps
working when the plugin is disabled.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.plugins.agent_broker.plugin import AgentBrokerPlugin
from termflow_control_plane.plugins.registry import FeatureRegistry

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"


def _make_client(tmp_path, *, agent_broker_enabled: bool = True) -> TestClient:
    settings = Settings(
        admin_token=ADMIN_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control-plane.db'}",
        allow_insecure_loopback=True,
        agent_broker_enabled=agent_broker_enabled,
    )
    database = Database(settings.database_url)
    app = create_app(settings=settings, database=database)
    return TestClient(app)


class TestCapabilityDiscovery:
    def test_reports_enabled_by_default(self, client: TestClient) -> None:
        response = client.get("/api/v1/agent/capabilities")

        assert response.status_code == 200
        assert response.json() == {"agent_broker_enabled": True}

    def test_reports_disabled_when_plugin_disabled(self, tmp_path) -> None:
        with _make_client(tmp_path, agent_broker_enabled=False) as client:
            response = client.get("/api/v1/agent/capabilities")

            assert response.status_code == 200
            assert response.json() == {"agent_broker_enabled": False}

    def test_reports_disabled_via_environment_override(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TERMFLOW_AGENT_BROKER_ENABLED", "false")
        settings = Settings(
            admin_token=ADMIN_TOKEN,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'env-disabled.db'}",
            allow_insecure_loopback=True,
        )
        database = Database(settings.database_url)
        app = create_app(settings=settings, database=database)

        with TestClient(app) as client:
            response = client.get("/api/v1/agent/capabilities")

            assert response.status_code == 200
            assert response.json() == {"agent_broker_enabled": False}

    def test_setting_defaults_to_enabled(self) -> None:
        settings = Settings(admin_token=ADMIN_TOKEN)

        assert settings.agent_broker_enabled is True


class TestExistingTerminalBehavior:
    def test_term_routes_unaffected_when_enabled(
        self,
        client: TestClient,
        admin_headers: dict[str, str],
    ) -> None:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/instances", headers=admin_headers).status_code == 200

    def test_term_routes_unaffected_when_disabled(
        self,
        tmp_path,
        admin_headers: dict[str, str],
    ) -> None:
        with _make_client(tmp_path, agent_broker_enabled=False) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/api/v1/instances", headers=admin_headers).status_code == 200


class TestFeatureRegistryWiring:
    def test_registry_exposed_on_app_state_and_declares_agent_broker(
        self,
        client: TestClient,
    ) -> None:
        registry = client.app.state.feature_registry

        assert isinstance(registry, FeatureRegistry)
        assert [(route.path, route.owner) for route in registry.routes] == [
            ("/api/v1/agent/capabilities", "agent_broker")
        ]
        assert [(revision.id, revision.owner) for revision in registry.migrations] == [
            ("0006", "agent_broker")
        ]

    def test_disabled_plugin_contributes_nothing(self, tmp_path) -> None:
        with _make_client(tmp_path, agent_broker_enabled=False) as client:
            registry = client.app.state.feature_registry

            assert registry.routes == ()
            assert registry.subscriptions == ()
            assert registry.migrations == ()

    def test_registry_starts_agent_broker_plugin(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        started: list[str] = []
        original_startup = AgentBrokerPlugin.startup

        async def recording_startup(self: AgentBrokerPlugin, context: object) -> None:
            started.append(self.id)
            await original_startup(self, context)

        monkeypatch.setattr(AgentBrokerPlugin, "startup", recording_startup)

        with _make_client(tmp_path) as client:
            assert started == ["agent_broker"]
            assert isinstance(client.app.state.feature_registry, FeatureRegistry)

    def test_creating_app_twice_does_not_double_register(self, tmp_path) -> None:
        settings = Settings(
            admin_token=ADMIN_TOKEN,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'duplicate.db'}",
            allow_insecure_loopback=True,
        )

        first = create_app(settings=settings)
        second = create_app(settings=settings)

        assert first is not second
        assert [
            (revision.id, revision.owner)
            for revision in first.state.feature_registry.migrations
        ] == [("0006", "agent_broker")]
        assert [
            (revision.id, revision.owner)
            for revision in second.state.feature_registry.migrations
        ] == [("0006", "agent_broker")]
