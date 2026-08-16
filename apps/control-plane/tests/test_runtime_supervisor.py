"""Runtime supervisor connector, epoch-bound capability, and Compose OpenCode
deployment tests (M4.3).

Covers plan §6.2.1 and §16 for the supervisor port:

- ``SupervisorConnector`` (``plugins.agent_broker.agent.runtime_supervisor``):
  register attestation with fail-closed not-ready / epoch-mismatch /
  binding-conflict handling, health overlay, quiesce drain semantics, an
  epoch-rotating restart that only reports ready after health proves the new
  epoch, cleanup, and the activation/tool-call gates B relies on.
- ``EpochBoundCapability``: fresh token per epoch, only the digest retained,
  raw token delivered exactly once then discarded, fail-closed verification.
- The OpenCode Compose service (plan §16): internal network only, no published
  host port, no docker socket / host workspace mounts, deny-by-default frozen
  config mounted read-only, hardened runtime and bounded logs.

The connector deliberately contains no Docker control: the tests inject a
``FakeRuntimeClient`` standing in for the deployment-owned runtime manager
(production wiring would be HTTP to that manager, which is out of M4.3 scope).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    CapabilityReuseAcrossEpochsError,
    EpochAttestationError,
    EpochBoundCapability,
    RuntimeBindingConflictError,
    RuntimeNotReadyError,
    SecretUnavailableError,
    SupervisorConnector,
    UnknownRuntimeError,
)
from termflow_control_plane.plugins.protocol import (
    AgentRuntimeSupervisor,
    BackendOperationOutcome,
    BackendOperationResult,
    CapabilityRef,
    DrainStatus,
    RuntimeHealth,
    RuntimeRef,
    RuntimeStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO_ROOT / "deploy" / "compose.yaml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
CONFIG_FIXTURE_PATH = (
    REPO_ROOT
    / "apps"
    / "control-plane"
    / "tests"
    / "fixtures"
    / "opencode"
    / "opencode-config.yaml"
)

#: The exact TermFlow MCP tool allowlist (plan §10) the frozen config pins.
TERMFLOW_TOOLS = {
    "termflow_list_panes",
    "termflow_pane_read",
    "termflow_pane_send_text",
    "termflow_pane_send_keys",
    "termflow_watch_create",
    "termflow_watch_list",
    "termflow_watch_get",
    "termflow_watch_cancel",
}

#: Every required variable the compose deployment interpolates must be
#: documented with a placeholder in the committed .env.example.
OPENCODE_REQUIRED_VARS = (
    "OPENCODE_SERVER_USERNAME",
    "OPENCODE_SERVER_PASSWORD",
    "OPENCODE_MODEL_API_KEY",
)

#: Values the compose parse check needs; never real secrets.
COMPOSE_ENV = {
    "TERMFLOW_ADMIN_TOKEN": "test-admin-token-for-compose-config",
    "OPENCODE_SERVER_USERNAME": "test-opencode-user",
    "OPENCODE_SERVER_PASSWORD": "test-opencode-password",
    "OPENCODE_MODEL_API_KEY": "test-opencode-model-key",
}

RUNTIME_REF = RuntimeRef("runtime-1")
BINDING_ID = "binding-1"
OTHER_BINDING_ID = "binding-2"
CAPABILITY_REF = CapabilityRef("capability-1")


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FakeRuntimeState:
    epoch: int
    ready: bool


class FakeRuntimeClient:
    """Test double for the deployment-owned runtime manager.

    The production connector never drives Docker: production wiring injects an
    HTTP client for a deployment-owned runtime manager (out of M4.3 scope).
    This fake simulates that manager's side of the lifecycle: a runtime
    provisioned with an initial readiness/epoch, restarts that take effect only
    after a configurable number of not-ready health polls, and cleanup that
    removes the runtime.
    """

    def __init__(
        self,
        *,
        ready: bool = True,
        epoch: int = 1,
        restart_unready_polls: int = 0,
    ) -> None:
        self._initial_ready = ready
        self._initial_epoch = epoch
        self._restart_unready_polls = restart_unready_polls
        self._states: dict[RuntimeRef, _FakeRuntimeState] = {}
        self._removed: set[RuntimeRef] = set()
        self._unready_remaining: dict[RuntimeRef, int] = {}
        self.restart_calls: list[tuple[RuntimeRef, int]] = []
        self.restart_secrets: list[str] = []
        self.cleaned_up: list[RuntimeRef] = []
        self.health_calls = 0

    def _state(self, runtime_ref: RuntimeRef) -> _FakeRuntimeState | None:
        if runtime_ref in self._removed:
            return None
        return self._states.get(runtime_ref) or _FakeRuntimeState(
            epoch=self._initial_epoch,
            ready=self._initial_ready,
        )

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth:
        self.health_calls += 1
        state = self._state(runtime_ref)
        if state is None:
            return RuntimeHealth(
                status=RuntimeStatus.UNKNOWN,
                epoch=self._initial_epoch,
                observed_at=datetime.now(UTC),
            )
        if state.ready:
            return RuntimeHealth(
                status=RuntimeStatus.READY,
                epoch=state.epoch,
                observed_at=datetime.now(UTC),
            )
        remaining = self._unready_remaining.get(runtime_ref, 0)
        if remaining > 0:
            self._unready_remaining[runtime_ref] = remaining - 1
            if remaining - 1 == 0:
                # The restart finally proves the new epoch: become ready on
                # the next health poll.
                self._states[runtime_ref] = _FakeRuntimeState(
                    epoch=state.epoch, ready=True
                )
        return RuntimeHealth(
            status=RuntimeStatus.NOT_READY,
            epoch=state.epoch,
            observed_at=datetime.now(UTC),
        )

    async def quiesce(self, runtime_ref: RuntimeRef, deadline: datetime) -> DrainStatus:
        if datetime.now(UTC) <= deadline:
            return DrainStatus.DRAINED
        return DrainStatus.DRAIN_TIMEOUT

    async def restart(
        self,
        runtime_ref: RuntimeRef,
        epoch: int,
        capability_secret: str,
    ) -> RuntimeHealth:
        self.restart_calls.append((runtime_ref, epoch))
        self.restart_secrets.append(capability_secret)
        ready = self._restart_unready_polls <= 0
        self._states[runtime_ref] = _FakeRuntimeState(epoch=epoch, ready=ready)
        if not ready:
            self._unready_remaining[runtime_ref] = self._restart_unready_polls
        return RuntimeHealth(
            status=RuntimeStatus.READY if ready else RuntimeStatus.NOT_READY,
            epoch=epoch,
            observed_at=datetime.now(UTC),
        )

    async def cleanup(self, runtime_ref: RuntimeRef) -> BackendOperationResult:
        self.cleaned_up.append(runtime_ref)
        self._states.pop(runtime_ref, None)
        self._removed.add(runtime_ref)
        return BackendOperationResult(
            outcome=BackendOperationOutcome.CONFIRMED,
            message=f"runtime {runtime_ref} cleaned up",
        )


class FakeSecretProvider:
    """Binding-scoped capability secrets: stable within an epoch, fresh across.

    Mirrors the deployment contract the connector relies on: the secret for a
    given (capability, epoch) never changes, but an epoch rotation must yield a
    new secret (otherwise the rotation would not invalidate the old token).
    """

    def __init__(self) -> None:
        self._tokens: dict[tuple[CapabilityRef, int], str] = {}
        self.calls: list[tuple[CapabilityRef, int]] = []

    def __call__(self, capability_ref: CapabilityRef, epoch: int) -> str:
        self.calls.append((capability_ref, epoch))
        key = (capability_ref, epoch)
        if key not in self._tokens:
            self._tokens[key] = f"binding-secret-epoch-{epoch}-{secrets.token_hex(12)}"
        return self._tokens[key]


@pytest.fixture
def client() -> FakeRuntimeClient:
    return FakeRuntimeClient()


@pytest.fixture
def provider() -> FakeSecretProvider:
    return FakeSecretProvider()


def make_connector(
    client: FakeRuntimeClient,
    provider: FakeSecretProvider,
    **kwargs: object,
) -> SupervisorConnector:
    """Build a connector with polling disabled for fast deterministic tests."""
    return SupervisorConnector(
        client,
        provider,
        health_poll_delay_seconds=0.0,
        **kwargs,
    )


def _connector_holds_secret(connector: SupervisorConnector, secret: str) -> bool:
    """Recursively search the connector's own state for a raw secret.

    Injected dependencies (the runtime client and the secret provider) are
    opaque: they are owned by the deployment, not by the connector.
    """

    def contains(value: object) -> bool:
        if isinstance(value, str):
            return value == secret
        if isinstance(value, Mapping):
            return any(contains(item) for item in value.values())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(contains(item) for item in value)
        if value is None or isinstance(value, (int, float, bool, bytes)):
            return False
        namespace = getattr(value, "__dict__", None)
        if namespace is None and hasattr(value, "__slots__"):
            namespace = {name: getattr(value, name) for name in value.__slots__}
        if namespace:
            return any(contains(item) for item in namespace.values())
        return False

    for name, value in vars(connector).items():
        if name in ("_client", "_secret_provider"):
            continue
        if contains(value):
            return True
    return False


# ---------------------------------------------------------------------------
# SupervisorConnector: register attestation.
# ---------------------------------------------------------------------------


class TestRegister:
    async def test_ready_runtime_with_expected_epoch_is_admitted(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)

        health = await connector.health(RUNTIME_REF)
        assert health.status is RuntimeStatus.READY
        assert health.epoch == 1
        assert connector.accept_activation(RUNTIME_REF, 1) is True
        assert connector.accept_tool_call(RUNTIME_REF, 1) is True

    async def test_not_ready_runtime_is_rejected_fail_closed(
        self, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(FakeRuntimeClient(ready=False), provider)
        with pytest.raises(RuntimeNotReadyError):
            await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        assert connector.accept_activation(RUNTIME_REF, 1) is False
        assert connector.accept_tool_call(RUNTIME_REF, 1) is False

    async def test_unknown_runtime_is_rejected_fail_closed(
        self, provider: FakeSecretProvider
    ) -> None:
        client = FakeRuntimeClient()
        await client.cleanup(RUNTIME_REF)
        connector = make_connector(client, provider)
        with pytest.raises(RuntimeNotReadyError):
            await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)

    async def test_epoch_attestation_mismatch_is_rejected_fail_closed(
        self, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(FakeRuntimeClient(epoch=2), provider)
        with pytest.raises(EpochAttestationError):
            await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        assert connector.accept_activation(RUNTIME_REF, 1) is False

    async def test_regression_to_an_older_epoch_is_rejected(
        self, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(FakeRuntimeClient(epoch=2), provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 2, CAPABILITY_REF)
        with pytest.raises(EpochAttestationError):
            await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)

    async def test_one_binding_per_runtime(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        with pytest.raises(RuntimeBindingConflictError):
            await connector.register(OTHER_BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        # The original binding keeps working.
        assert connector.accept_activation(RUNTIME_REF, 1) is True


# ---------------------------------------------------------------------------
# SupervisorConnector: health overlay.
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_unregistered_runtime_reports_unknown(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        # The deployment never provisioned this runtime.
        await client.cleanup(RUNTIME_REF)
        health = await connector.health(RUNTIME_REF)
        assert health.status is RuntimeStatus.UNKNOWN

    async def test_attested_runtime_drift_reports_not_ready(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        # External drift: the deployment restarts the runtime behind B's back.
        client._states[RUNTIME_REF] = _FakeRuntimeState(epoch=3, ready=True)
        health = await connector.health(RUNTIME_REF)
        assert health.status is RuntimeStatus.NOT_READY
        assert health.epoch == 1
        assert connector.accept_tool_call(RUNTIME_REF, 1) is False


# ---------------------------------------------------------------------------
# SupervisorConnector: quiesce.
# ---------------------------------------------------------------------------


class TestQuiesce:
    async def test_drained_within_deadline(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        deadline = datetime.now(UTC) + timedelta(hours=1)
        result = await connector.quiesce(RUNTIME_REF, deadline)
        assert result is DrainStatus.DRAINED

    async def test_drain_timeout_blocks_activation(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        deadline = datetime.now(UTC) - timedelta(hours=1)
        result = await connector.quiesce(RUNTIME_REF, deadline)
        assert result is DrainStatus.DRAIN_TIMEOUT
        assert connector.accept_activation(RUNTIME_REF, 1) is False
        assert connector.accept_tool_call(RUNTIME_REF, 1) is False


# ---------------------------------------------------------------------------
# SupervisorConnector: epoch-rotating restart.
# ---------------------------------------------------------------------------


class TestRestart:
    async def test_restart_rotates_epoch_and_is_ready_once_health_proves_it(
        self, provider: FakeSecretProvider
    ) -> None:
        client = FakeRuntimeClient(restart_unready_polls=2)
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)

        health = await connector.restart(RUNTIME_REF, 2, CAPABILITY_REF)

        assert health.status is RuntimeStatus.READY
        assert health.epoch == 2
        assert provider.calls == [(CAPABILITY_REF, 2)]
        assert client.restart_calls == [(RUNTIME_REF, 2)]
        raw = client.restart_secrets
        assert len(raw) == 1, "the raw capability secret must be delivered exactly once"

    async def test_restart_rotates_capability_and_does_not_retain_the_raw_token(
        self, provider: FakeSecretProvider
    ) -> None:
        client = FakeRuntimeClient()
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        await connector.restart(RUNTIME_REF, 2, CAPABILITY_REF)

        raw = client.restart_secrets[0]
        # Only the digest is stored on the connector; the raw token is discarded.
        stored = connector._capabilities._digests[(CAPABILITY_REF, 2)]
        assert stored == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert raw not in connector._capabilities._digests.values()
        assert not _connector_holds_secret(connector, raw)
        # Verification succeeds only with the epoch-bound token itself.
        assert connector._capabilities.verify(CAPABILITY_REF, 2, raw) is True
        assert connector._capabilities.verify(CAPABILITY_REF, 2, "wrong-token") is False

    async def test_old_epoch_calls_rejected_after_rotation(
        self, provider: FakeSecretProvider
    ) -> None:
        client = FakeRuntimeClient()
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        await connector.restart(RUNTIME_REF, 2, CAPABILITY_REF)

        assert connector.accept_activation(RUNTIME_REF, 2) is True
        assert connector.accept_activation(RUNTIME_REF, 1) is False
        assert connector.accept_tool_call(RUNTIME_REF, 2) is True
        assert connector.accept_tool_call(RUNTIME_REF, 1) is False

    async def test_restart_stays_not_ready_until_health_proves_expected_epoch(
        self, provider: FakeSecretProvider
    ) -> None:
        client = FakeRuntimeClient(restart_unready_polls=10**6)
        connector = make_connector(client, provider, health_poll_attempts=3)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)

        health = await connector.restart(RUNTIME_REF, 2, CAPABILITY_REF)

        assert health.status is RuntimeStatus.NOT_READY
        assert health.epoch == 2
        # Fail-closed: neither the new nor the old epoch is admitted.
        assert connector.accept_activation(RUNTIME_REF, 2) is False
        assert connector.accept_tool_call(RUNTIME_REF, 2) is False
        assert connector.accept_tool_call(RUNTIME_REF, 1) is False

    async def test_restart_rejects_epoch_regression(
        self, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(FakeRuntimeClient(epoch=2), provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 2, CAPABILITY_REF)
        with pytest.raises(EpochAttestationError):
            await connector.restart(RUNTIME_REF, 1, CAPABILITY_REF)
        assert provider.calls == []
        assert connector.accept_activation(RUNTIME_REF, 2) is True

    async def test_restart_of_unregistered_runtime_fails_closed(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        with pytest.raises(UnknownRuntimeError):
            await connector.restart(RUNTIME_REF, 2, CAPABILITY_REF)
        assert client.restart_calls == []

    async def test_missing_secret_fails_closed_without_restarting(
        self, client: FakeRuntimeClient
    ) -> None:
        def empty_provider(capability_ref: CapabilityRef, epoch: int) -> str:
            return ""

        connector = make_connector(client, empty_provider)  # type: ignore[arg-type]
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)
        with pytest.raises(SecretUnavailableError):
            await connector.restart(RUNTIME_REF, 2, CAPABILITY_REF)
        assert client.restart_calls == []
        assert connector.accept_tool_call(RUNTIME_REF, 1) is True


# ---------------------------------------------------------------------------
# SupervisorConnector: cleanup.
# ---------------------------------------------------------------------------


class TestCleanup:
    async def test_cleanup_confirms_and_forgets_the_runtime(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        await connector.register(BINDING_ID, RUNTIME_REF, 1, CAPABILITY_REF)

        result = await connector.cleanup(RUNTIME_REF)

        assert result.outcome is BackendOperationOutcome.CONFIRMED
        assert client.cleaned_up == [RUNTIME_REF]
        assert connector.accept_activation(RUNTIME_REF, 1) is False
        health = await connector.health(RUNTIME_REF)
        assert health.status is RuntimeStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Structural protocol conformance.
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_connector_satisfies_agent_runtime_supervisor_protocol(
        self, client: FakeRuntimeClient, provider: FakeSecretProvider
    ) -> None:
        connector = make_connector(client, provider)
        assert isinstance(connector, AgentRuntimeSupervisor)


# ---------------------------------------------------------------------------
# EpochBoundCapability.
# ---------------------------------------------------------------------------


class TestEpochBoundCapability:
    def test_provision_stores_only_the_digest_and_verifies(self) -> None:
        capability = EpochBoundCapability()
        raw = "raw-binding-secret-1"
        capability.provision(CAPABILITY_REF, 1, raw)
        assert capability._digests[(CAPABILITY_REF, 1)] == hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()
        assert raw not in capability._digests.values()
        assert capability.verify(CAPABILITY_REF, 1, raw) is True
        assert capability.verify(CAPABILITY_REF, 1, "wrong") is False

    def test_fresh_token_per_epoch(self) -> None:
        capability = EpochBoundCapability()
        capability.provision(CAPABILITY_REF, 1, "token-epoch-1")
        capability.provision(CAPABILITY_REF, 2, "token-epoch-2")
        assert (
            capability._digests[(CAPABILITY_REF, 1)]
            != capability._digests[(CAPABILITY_REF, 2)]
        )
        assert capability.verify(CAPABILITY_REF, 1, "token-epoch-1") is True
        assert capability.verify(CAPABILITY_REF, 1, "token-epoch-2") is False
        assert capability.verify(CAPABILITY_REF, 2, "token-epoch-2") is True
        assert capability.verify(CAPABILITY_REF, 2, "token-epoch-1") is False

    def test_reusing_one_token_across_epochs_fails_closed(self) -> None:
        capability = EpochBoundCapability()
        capability.provision(CAPABILITY_REF, 1, "same-token")
        with pytest.raises(CapabilityReuseAcrossEpochsError):
            capability.provision(CAPABILITY_REF, 2, "same-token")

    def test_verify_of_unprovisioned_capability_fails_closed(self) -> None:
        capability = EpochBoundCapability()
        assert capability.verify(CAPABILITY_REF, 3, "anything") is False

    def test_revocation_forgets_the_digest(self) -> None:
        capability = EpochBoundCapability()
        capability.provision(CAPABILITY_REF, 1, "token")
        capability.revoke(CAPABILITY_REF, 1)
        assert capability.verify(CAPABILITY_REF, 1, "token") is False

    def test_empty_token_is_rejected(self) -> None:
        capability = EpochBoundCapability()
        with pytest.raises(ValueError):
            capability.provision(CAPABILITY_REF, 1, "   ")


# ---------------------------------------------------------------------------
# Compose deployment (plan §16).
# ---------------------------------------------------------------------------


class TestComposeDeployment:
    def test_frozen_opencode_config_still_denies_by_default(self) -> None:
        body: list[str] = []
        for line in CONFIG_FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            body.append(line)
        config = json.loads("\n".join(body))
        assert config["permission"]["*"] == "deny"
        configured_tools = set(config["permission"]) - {"*"}
        assert configured_tools == TERMFLOW_TOOLS
        assert config.get("mcp") in (None, {})

    def test_compose_static_hardening_assertions(self) -> None:
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        # No host-published port for the OpenCode server and exactly one
        # "ports:" block (control-plane only).
        assert ":4096:" not in text
        assert "4096:4096" not in text
        assert text.count("ports:") == 1
        # No docker socket, no A directories, no TermFlow credentials mounts.
        assert "/var/run/docker.sock" not in text
        # Isolated internal network and hardened runtime.
        assert "agent_internal" in text
        assert "internal: true" in text
        assert "cap_drop" in text and "ALL" in text
        assert "read_only" in text
        assert "no-new-privileges" in text
        assert "tmpfs" in text
        assert "mem_limit" in text
        assert "cpus" in text
        assert "pids_limit" in text
        assert "user:" in text and "405:100" in text
        assert "healthcheck" in text
        # Pinned image with digest placeholder (M8 pins the exact digest) and
        # the frozen deny-by-default config mounted read-only.
        assert "@sha256:" in text
        assert "opencode-config.yaml:ro" in text

    def test_env_example_documents_opencode_vars_without_real_secrets(self) -> None:
        text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        for var in OPENCODE_REQUIRED_VARS:
            assert var in text, f".env.example must document {var}"
        # Placeholders only: no real-looking credentials may be committed.
        assert "replace-with" in text
        for forbidden in (
            "test-opencode-password",
            "test-opencode-model-key",
            "sk-ant-",
            "sk-proj-",
        ):
            assert forbidden not in text

    @pytest.mark.skipif(
        shutil.which("docker") is None,
        reason="docker is not available; static assertions only",
    )
    def test_docker_compose_config_renders_isolated_opencode_service(self) -> None:
        env = {**os.environ, **COMPOSE_ENV}
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_PATH),
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            check=False,
        )
        if proc.returncode != 0:
            pytest.fail(f"docker compose config failed: {proc.stderr}")
        rendered = json.loads(proc.stdout)

        service = rendered["services"]["opencode-agent"]
        # Pinned image with a digest (M8 replaces the placeholder digest).
        assert "@sha256:" in service["image"]
        # No host-published ports at all.
        assert not service.get("ports")
        # Internal network only: never attached to the public/default network.
        assert set(service["networks"]) == {"agent_internal"}
        assert "default" not in service["networks"]
        assert rendered["networks"]["agent_internal"].get("internal") is True
        # control-plane bridges the public network and the internal one.
        control_plane_networks = set(rendered["services"]["control-plane"]["networks"])
        assert {"default", "agent_internal"} <= control_plane_networks
        # Hardening (plan §16).
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["read_only"] is True
        assert service["user"] not in ("root", "0", "0:0")
        assert service["tmpfs"]
        assert any("size=" in str(tmpfs) for tmpfs in service["tmpfs"])
        assert service.get("mem_limit")
        assert service.get("cpus")
        assert service.get("pids_limit")
        assert service["logging"]["options"].get("max-size")
        assert service["logging"]["options"].get("max-file")
        assert service["healthcheck"]["test"]
        # Mounts: the frozen config read-only and a dedicated data volume; no
        # docker socket, no A directories, no TermFlow credentials, no host
        # workspaces, and every bind mount must be read-only.
        mounts = service["volumes"]
        assert mounts, "opencode-agent must carry the config/data mounts"
        for mount in mounts:
            source = str(mount.get("source", ""))
            assert "/var/run/docker.sock" not in source
            if mount.get("type") == "bind":
                assert mount.get("read_only") is True, (
                    f"bind mount {source!r} must be read-only"
                )
        assert any(mount.get("type") == "volume" for mount in mounts)
