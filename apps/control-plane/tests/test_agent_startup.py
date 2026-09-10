import asyncio
from uuid import uuid4

import pytest
from termflow_control_plane.plugins.agent_broker.startup import AgentStartupCoordinator


def test_runtime_health_loop_runs_recovery_aware_health_cycle():
    """The periodic task delegates retry and fencing policy to the controller."""

    from termflow_control_plane.plugins.agent_broker.plugin import runtime_health_loop

    async def scenario():
        stop = asyncio.Event()

        class Controller:
            reconcile_calls = 0
            health_calls = 0

            async def reconcile_all(self):
                self.reconcile_calls += 1
                stop.set()
                return []

            async def run_health_cycle(self):
                self.health_calls += 1
                stop.set()
                return []

        controller = Controller()
        await runtime_health_loop(controller, tick_seconds=0.01, stop=stop)
        assert controller.reconcile_calls == 0
        assert controller.health_calls == 1

    asyncio.run(scenario())


def test_critical_fencing_failure_blocks_activation_and_retry_is_idempotent():
    async def scenario():
        calls = []
        failed = True

        async def fencing():
            calls.append("fence")
            if failed:
                raise RuntimeError("private db failure")

        def hook(name):
            async def run():
                calls.append(name)

            return run

        coordinator = AgentStartupCoordinator(
            fencing,
            hook("controller"),
            hook("topology"),
            hook("backend"),
            hook("watches"),
            hook("dispatcher"),
            hook("cleanup"),
        )
        result = await coordinator.recover()
        assert result.state == "degraded"
        assert result.reason_code == "recovery_failed"
        await coordinator.activate()
        assert calls == ["fence", "cleanup"]
        failed = False
        assert (await coordinator.recover()).state == "starting"
        assert (await coordinator.activate()).state == "ready"
        await coordinator.activate()
        assert calls.count("backend") == calls.count("dispatcher") == 1

    asyncio.run(scenario())


def test_app_critical_recovery_failure_keeps_health_and_degrades_capabilities(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from termflow_control_plane.app import create_app
    from termflow_control_plane.config import Settings
    from termflow_control_plane.plugins.agent_broker.agent.inbox import InboxDeliveryStateMachine

    async def fail(self, **kwargs):
        raise RuntimeError("database fencing failed")

    monkeypatch.setattr(InboxDeliveryStateMachine, "recover_stale", fail)
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}",
        allow_insecure_loopback=True,
    )
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/healthz").status_code == 200
        body = client.get("/api/v1/agent/capabilities").json()
        assert body["state"] == "degraded"
        assert body["reason_code"] == "recovery_failed"
        assert client.app.state.agent_broker_plugin._watch_tick_task is None
        from starlette.requests import Request
        from termflow_control_plane.api.agent_admin import _reconcile_binding
        from termflow_control_plane.errors import TermFlowError

        with pytest.raises(TermFlowError) as error:
            client.portal.call(
                _reconcile_binding, Request({"type": "http", "app": client.app}), uuid4()
            )
        assert error.value.code == "recovery_failed"


def test_agent_migration_failure_keeps_core_lifespan_and_degrades_capabilities(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient
    from termflow_control_plane import app as app_module
    from termflow_control_plane.config import Settings
    from termflow_control_plane.persistence import database as database_module

    def fail_agent_migrations(_connection):
        raise RuntimeError("injected Agent migration failure")

    monkeypatch.setattr(database_module, "_upgrade_agent", fail_agent_migrations)
    settings = Settings(
        admin_token="admin-token-that-is-long-enough-for-tests",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'migration-failure.db'}",
        allow_insecure_loopback=True,
    )

    with TestClient(app_module.create_app(settings=settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get(
            "/api/v1/dashboard",
            headers={"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"},
        ).status_code == 200
        capabilities = client.get("/api/v1/agent/capabilities").json()
        assert capabilities["state"] == "degraded"
        assert capabilities["reason_code"] == "recovery_failed"
        blocked = client.get(
            "/api/v1/agent/admin/profiles",
            headers={"Authorization": "Bearer admin-token-that-is-long-enough-for-tests"},
        )
        assert blocked.status_code == 503
        assert blocked.json()["error"]["code"] == "recovery_failed"
        assert client.app.state.agent_broker_plugin._watch_tick_task is None


def test_activation_requires_successful_recovery():
    async def scenario():
        async def forbidden():
            raise AssertionError("activation before fencing")

        coordinator = AgentStartupCoordinator(forbidden, backend=forbidden)
        assert (await coordinator.activate()).state == "starting"

    asyncio.run(scenario())


def test_recovery_report_failure_blocks_all_activation_hooks():
    from termflow_control_plane.plugins.agent_broker.plugin import AgentRecoveryReport

    async def scenario():
        async def fencing():
            return AgentRecoveryReport(critical_failures=("run_fencing",))

        async def forbidden():
            raise AssertionError("work must remain fenced")

        coordinator = AgentStartupCoordinator(fencing, backend=forbidden)
        assert (await coordinator.recover()).state == "degraded"
        assert (await coordinator.activate()).state == "degraded"

    asyncio.run(scenario())


def test_cleanup_failure_does_not_change_successful_recovery():
    async def scenario():
        async def success():
            return None

        async def cleanup():
            raise RuntimeError("helper offline")

        coordinator = AgentStartupCoordinator(success, cleanup=cleanup)
        assert (await coordinator.recover()).state == "starting"
        assert (await coordinator.activate()).state == "ready"

    asyncio.run(scenario())


def test_disabled_agent_capabilities_are_explicit(client):
    client.app.state.settings.agent_broker_enabled = False
    assert client.get("/api/v1/agent/capabilities").json()["state"] == "disabled"
