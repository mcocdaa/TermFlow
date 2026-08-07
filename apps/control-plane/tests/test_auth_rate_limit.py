from __future__ import annotations

from contextlib import AbstractAsyncContextManager

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from termflow_control_plane.auth.rate_limit import (
    AuthRateLimiter,
    direct_peer_source,
)
from termflow_control_plane.errors import TermFlowError


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _limiter(clock: ManualClock, **overrides: object) -> AuthRateLimiter:
    options: dict[str, object] = {
        "clock": clock,
        "capacity": 5,
        "refill_seconds": 60.0,
        "global_capacity": 100,
        "global_refill_seconds": 1.0,
        "max_backoff_seconds": 300,
        "max_entries": 32,
        "state_ttl_seconds": 900.0,
        "max_concurrent_verifications": 2,
    }
    options.update(overrides)
    return AuthRateLimiter(**options)  # type: ignore[arg-type]


def _rate_limited(limiter: AuthRateLimiter, purpose: str, source: str) -> TermFlowError:
    with pytest.raises(TermFlowError) as caught:
        limiter.check(purpose, source)
    assert caught.value.code == "rate_limited"
    assert caught.value.status_code == 429
    assert caught.value.message == "Authentication is temporarily unavailable."
    return caught.value


def test_source_bucket_allows_five_then_refills_once_per_minute() -> None:
    clock = ManualClock()
    limiter = _limiter(clock)

    for _ in range(5):
        limiter.check("web_session", "192.0.2.1")

    assert _rate_limited(limiter, "web_session", "192.0.2.1").retry_after == 60

    clock.advance(59.1)
    assert _rate_limited(limiter, "web_session", "192.0.2.1").retry_after == 1
    clock.advance(0.9)
    limiter.check("web_session", "192.0.2.1")
    assert _rate_limited(limiter, "web_session", "192.0.2.1").retry_after == 60


def test_purpose_budget_overrides_default_capacity_and_refill() -> None:
    clock = ManualClock()
    limiter = _limiter(clock, purpose_budgets={"oauth_device_token": (60, 1.0)})

    for _ in range(60):
        limiter.check("oauth_device_token", "192.0.2.20")

    assert _rate_limited(limiter, "oauth_device_token", "192.0.2.20").retry_after == 1

    clock.advance(1.0)
    limiter.check("oauth_device_token", "192.0.2.20")

    for _ in range(5):
        limiter.check("other_purpose", "192.0.2.20")
    assert _rate_limited(limiter, "other_purpose", "192.0.2.20").retry_after == 60


def test_failures_back_off_one_two_four_seconds_and_cap_at_five_minutes() -> None:
    clock = ManualClock()
    limiter = _limiter(clock, capacity=1000)
    expected_delays = [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300]

    for delay in expected_delays:
        limiter.check("totp", "198.51.100.7")
        assert limiter.record_failure("totp", "198.51.100.7") == delay
        assert _rate_limited(limiter, "totp", "198.51.100.7").retry_after == delay
        clock.advance(delay)


def test_retry_after_is_stable_and_rounded_up() -> None:
    clock = ManualClock()
    limiter = _limiter(clock, capacity=100)
    limiter.record_failure("oauth", "203.0.113.4")
    clock.advance(0.01)

    first = _rate_limited(limiter, "oauth", "203.0.113.4")
    second = _rate_limited(limiter, "oauth", "203.0.113.4")

    assert first.retry_after == 1
    assert second.retry_after == 1


def test_success_clears_only_matching_source_and_purpose_failure_state() -> None:
    clock = ManualClock()
    limiter = _limiter(clock, capacity=100)
    limiter.record_failure("web_session", "192.0.2.10")
    limiter.record_failure("web_session", "192.0.2.11")
    limiter.record_failure("cli_token", "192.0.2.10")

    limiter.record_success("web_session", "192.0.2.10")

    limiter.check("web_session", "192.0.2.10")
    assert _rate_limited(limiter, "web_session", "192.0.2.11").retry_after == 1
    assert _rate_limited(limiter, "cli_token", "192.0.2.10").retry_after == 1


@pytest.mark.asyncio
async def test_global_concurrency_rejects_without_consuming_source_attempt() -> None:
    clock = ManualClock()
    limiter = _limiter(
        clock,
        global_capacity=5,
        max_concurrent_verifications=1,
    )

    first: AbstractAsyncContextManager[None] = limiter.verification_slot()
    async with first:
        with pytest.raises(TermFlowError) as caught:
            async with limiter.verification_slot():
                raise AssertionError("the overloaded verification must not start")
        assert caught.value.code == "rate_limited"
        assert caught.value.retry_after == 1

    for _ in range(5):
        limiter.check("web_session", "overloaded-peer")
    assert _rate_limited(limiter, "web_session", "overloaded-peer").retry_after == 1


def test_global_request_budget_is_independent_of_source_churn() -> None:
    clock = ManualClock()
    limiter = _limiter(
        clock,
        global_capacity=2,
        global_refill_seconds=60.0,
    )
    limiter.check("web_session", "192.0.2.1")
    limiter.check("web_session", "192.0.2.2")

    limited = _rate_limited(limiter, "web_session", "192.0.2.3")

    assert limited.retry_after == 60


def test_source_state_is_lru_bounded_and_expired_state_is_pruned() -> None:
    clock = ManualClock()
    limiter = _limiter(clock, max_entries=3, state_ttl_seconds=301.0)
    for index in range(10):
        limiter.record_failure("web_session", f"192.0.2.{index}")

    assert limiter.tracked_source_count == 3

    clock.advance(302)
    limiter.prune()
    assert limiter.tracked_source_count == 0


def test_direct_peer_source_ignores_forwarding_headers() -> None:
    app = FastAPI()

    @app.get("/source")
    async def source(request: Request) -> dict[str, str]:
        return {"source": direct_peer_source(request)}

    with TestClient(app) as client:
        response = client.get(
            "/source",
            headers={
                "Forwarded": "for=203.0.113.99",
                "X-Forwarded-For": "198.51.100.88",
                "X-Real-IP": "192.0.2.77",
            },
        )

    assert response.json() == {"source": "testclient"}


def test_control_plane_returns_structured_429_with_retry_after(client: TestClient) -> None:
    limiter = AuthRateLimiter()
    app = FastAPI()
    app.add_exception_handler(TermFlowError, client.app.exception_handlers[TermFlowError])

    @app.get("/limited")
    async def protected(request: Request) -> dict[str, bool]:
        limiter.check("test_endpoint", direct_peer_source(request))
        return {"allowed": True}

    with TestClient(app) as limited_client:
        for _ in range(5):
            assert limited_client.get("/limited").status_code == 200

        response = limited_client.get("/limited")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.json()["error"]["message"] == (
        "Authentication is temporarily unavailable."
    )


def test_real_browser_login_enforces_backoff_and_emits_safe_audit(client: TestClient) -> None:
    clock = ManualClock()
    client.app.state.auth_rate_limiter = _limiter(clock, capacity=100)
    events: list[object] = []

    class AuditCapture:
        async def record(self, *args: object, **kwargs: object) -> None:
            events.append((args, kwargs))

    client.app.state.auth_audit = AuditCapture()
    headers = {"Origin": "http://127.0.0.1:8000"}

    rejected = client.post(
        "/api/v1/admin/sessions",
        headers=headers,
        json={"admin_token": "submitted-secret"},
    )
    limited = client.post(
        "/api/v1/admin/sessions",
        headers=headers,
        json={"admin_token": "a-different-secret"},
    )

    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "authentication_failed"
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "1"
    serialized = repr(events)
    assert "submitted-secret" not in serialized
    assert "a-different-secret" not in serialized
    assert "REJECTED" in serialized
    assert "RATE_LIMITED" in serialized
