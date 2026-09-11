"""HTTP-health production runtime client tests (2026-08-22 scope decision).

Covers the simplified production ``RuntimeClient``
(``plugins.agent_broker.agent.http_runtime_client``): admission is an
authenticated ``/global/health`` probe; the reported epoch resolves from the
binding provisioned for the runtime ref; quiesce drains trivially (the
one-active-run invariant serializes calls); restart re-probes without any
container orchestration; cleanup is best-effort confirmed.  Also covers the
settings-backed capability secret provider used by the supervisor's restart
ceremony.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from termflow_control_plane.plugins.agent_broker.agent.http_runtime_client import (
    PUBLIC_READINESS_REASON_CODES,
    HttpHealthRuntimeClient,
    RuntimeReadinessProbe,
    settings_capability_secret_provider,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_supervisor import (
    SecretUnavailableError,
)
from termflow_control_plane.plugins.protocol import (
    CapabilityRef,
    DrainStatus,
    RuntimeRef,
    RuntimeStatus,
)

RUNTIME_REF = RuntimeRef("runtime-1")
CAPABILITY_REF = CapabilityRef("capability-1")


def _client(
    handler,
    *,
    epoch_resolver=None,
    **kwargs,
) -> HttpHealthRuntimeClient:
    return HttpHealthRuntimeClient(
        base_url="http://runtime.test",
        username="termflow",
        password="secret-password",
        epoch_resolver=epoch_resolver,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _healthy_handler(request: httpx.Request) -> httpx.Response:
    auth = request.headers.get("Authorization", "")
    assert auth.startswith("Basic ")
    if request.url.path == "/global/health":
        return httpx.Response(200, json={"healthy": True, "version": "1.18.18"})
    assert request.url.path == "/mcp"
    return httpx.Response(200, json={"termflow": {"status": "connected"}})


async def test_readiness_requires_termflow_mcp_connected() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        return httpx.Response(200, json={"termflow": {"status": "connected"}})

    client = _client(handler)
    try:
        result = await client.readiness(RUNTIME_REF)
    finally:
        await client.aclose()
    assert result == RuntimeReadinessProbe(True, True, None)
    assert calls == ["/global/health", "/mcp"]


async def test_readiness_probes_mcp_with_the_configured_directory() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        assert request.url.path == "/mcp"
        seen.append(request.url.params.get("directory"))
        return httpx.Response(200, json={"termflow": {"status": "connected"}})

    client = _client(handler, directory="/tmp")
    try:
        result = await client.readiness(RUNTIME_REF)
    finally:
        await client.aclose()
    assert result == RuntimeReadinessProbe(True, True, None)
    assert seen == ["/tmp"]


async def test_readiness_rejects_missing_failed_or_malformed_mcp_state() -> None:
    payloads = (
        {},
        {"termflow": {"status": "failed"}},
        {"TermFlow": {"status": "connected"}},
        [],
        "not-json",
    )
    for payload in payloads:
        def handler(request: httpx.Request, payload=payload) -> httpx.Response:
            if request.url.path == "/global/health":
                return httpx.Response(200, json={"healthy": True})
            if payload == "not-json":
                return httpx.Response(200, text="not-json")
            return httpx.Response(200, json=payload)

        client = _client(handler)
        try:
            result = await client.readiness(RUNTIME_REF)
        finally:
            await client.aclose()
        assert result.healthy is True
        assert result.mcp_connected is False
        assert result.reason_code == "mcp_not_connected"
        assert result.reason_code in PUBLIC_READINESS_REASON_CODES


async def test_probe_does_not_echo_endpoint_or_credentials_in_public_reason() -> None:
    endpoint = "https://user:pass@example.invalid/private?secret=1"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("endpoint=https://user:pass@example.invalid token=secret")

    client = HttpHealthRuntimeClient(
        base_url=endpoint,
        username="basic-user",
        password="basic-password",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.readiness(RUNTIME_REF)
    finally:
        await client.aclose()
    assert result.reason_code == "runtime_unreachable"
    public = result.reason_code or ""
    for secret in (endpoint, "basic-user", "basic-password", "Authorization", "endpoint="):
        assert secret not in public


def test_client_requires_a_complete_basic_auth_pair_and_bounded_probe_config() -> None:
    common = {
        "base_url": "http://runtime.test",
        "transport": httpx.MockTransport(_healthy_handler),
    }
    with pytest.raises(ValueError, match="set together"):
        HttpHealthRuntimeClient(**common, username="only-user")
    with pytest.raises(ValueError, match="set together"):
        HttpHealthRuntimeClient(**common, password="only-password")
    with pytest.raises(ValueError, match="between"):
        HttpHealthRuntimeClient(**common, probe_timeout_seconds=0)
    with pytest.raises(ValueError, match="between"):
        HttpHealthRuntimeClient(**common, probe_timeout_seconds=30.1)
    with pytest.raises(ValueError, match="between"):
        HttpHealthRuntimeClient(**common, restart_probe_attempts=0)
    with pytest.raises(ValueError, match="between"):
        HttpHealthRuntimeClient(**common, restart_probe_attempts=101)
    with pytest.raises(ValueError, match="between"):
        HttpHealthRuntimeClient(**common, restart_probe_delay_seconds=-1)
    with pytest.raises(ValueError, match="between"):
        HttpHealthRuntimeClient(**common, restart_probe_delay_seconds=5.1)


async def test_health_reports_ready_with_resolved_binding_epoch() -> None:
    async def resolver(runtime_ref: RuntimeRef) -> int:
        assert runtime_ref == RUNTIME_REF
        return 7

    client = _client(_healthy_handler, epoch_resolver=resolver)
    try:
        health = await client.health(RUNTIME_REF)
    finally:
        await client.aclose()
    assert health.status is RuntimeStatus.READY
    assert health.epoch == 7


async def test_health_defaults_to_epoch_one_without_a_resolver() -> None:
    client = _client(_healthy_handler)
    try:
        health = await client.health(RUNTIME_REF)
    finally:
        await client.aclose()
    assert health.status is RuntimeStatus.READY
    assert health.epoch == 1


async def test_health_fails_closed_when_the_epoch_cannot_be_resolved() -> None:
    async def broken_resolver(runtime_ref: RuntimeRef) -> int:
        del runtime_ref
        raise LookupError("no binding carries this ref")

    client = _client(_healthy_handler, epoch_resolver=broken_resolver)
    try:
        health = await client.health(RUNTIME_REF)
    finally:
        await client.aclose()
    assert health.status is RuntimeStatus.UNKNOWN
    assert "epoch" in health.detail


async def test_health_reports_unknown_when_the_endpoint_is_unreachable() -> None:
    def refusing_handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("connection refused")

    client = _client(refusing_handler, epoch_resolver=_epoch_2)
    try:
        health = await client.health(RUNTIME_REF)
    finally:
        await client.aclose()
    assert health.status is RuntimeStatus.UNKNOWN
    assert health.epoch == 2


async def test_health_rejects_non_200_and_unhealthy_bodies() -> None:
    def unhealthy_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"healthy": False})

    client = _client(unhealthy_handler, epoch_resolver=_epoch_2)
    try:
        assert (await client.health(RUNTIME_REF)).status is RuntimeStatus.UNKNOWN
    finally:
        await client.aclose()

    server_error = _client(
        lambda request: httpx.Response(503), epoch_resolver=_epoch_2
    )
    try:
        health = await server_error.health(RUNTIME_REF)
    finally:
        await server_error.aclose()
    assert health.status is RuntimeStatus.UNKNOWN


async def test_health_rejects_malformed_success_bodies() -> None:
    responses = (
        httpx.Response(200, text="healthy"),
        httpx.Response(200, json={"healthy": "false"}),
        httpx.Response(200, json={"version": "1.18.18"}),
    )
    for response in responses:
        client = _client(lambda request, response=response: response)
        try:
            health = await client.health(RUNTIME_REF)
        finally:
            await client.aclose()
        assert health.status is RuntimeStatus.UNKNOWN


async def test_quiesce_drains_immediately() -> None:
    client = _client(_healthy_handler)
    try:
        status = await client.quiesce(RUNTIME_REF, datetime.now(UTC))
    finally:
        await client.aclose()
    assert status is DrainStatus.DRAINED


async def test_restart_reprobes_and_reports_the_requested_epoch() -> None:
    attempts = {"count": 0}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise httpx.ConnectError("not up yet")
            return httpx.Response(200, json={"healthy": True})
        return httpx.Response(200, json={"termflow": {"status": "connected"}})

    client = _client(
        flaky_handler,
        restart_probe_attempts=5,
        restart_probe_delay_seconds=0.0,
    )
    try:
        health = await client.restart(RUNTIME_REF, 9, "raw-secret")
    finally:
        await client.aclose()
    assert health.status is RuntimeStatus.READY
    assert health.epoch == 9
    assert attempts["count"] == 3


async def test_restart_fails_closed_after_exhausting_probes() -> None:
    client = _client(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
        restart_probe_attempts=2,
        restart_probe_delay_seconds=0.0,
    )
    try:
        health = await client.restart(RUNTIME_REF, 3, "raw-secret")
    finally:
        await client.aclose()
    assert health.status is RuntimeStatus.NOT_READY
    assert health.epoch == 3


async def test_restart_rejects_an_invalid_epoch() -> None:
    client = _client(_healthy_handler)
    try:
        with pytest.raises(ValueError):
            await client.restart(RUNTIME_REF, 0, "raw-secret")
    finally:
        await client.aclose()


async def test_cleanup_confirms_without_runtime_state() -> None:
    client = _client(_healthy_handler)
    try:
        result = await client.cleanup(RUNTIME_REF)
    finally:
        await client.aclose()
    assert result.outcome.value == "confirmed"


async def test_capability_secret_provider_fails_closed_without_a_token() -> None:
    provider = settings_capability_secret_provider(lambda: None)
    with pytest.raises(SecretUnavailableError):
        provider(CAPABILITY_REF, 4)

    empty = settings_capability_secret_provider(lambda: "   ")
    with pytest.raises(SecretUnavailableError):
        empty(CAPABILITY_REF, 4)

    configured = settings_capability_secret_provider(lambda: "raw-capability")
    assert configured(CAPABILITY_REF, 4) == "raw-capability"


async def _epoch_2(runtime_ref: RuntimeRef) -> int:
    del runtime_ref
    return 2
