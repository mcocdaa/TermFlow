"""Deterministic product-level Agent setup and lifecycle acceptance.

These tests run the real Control Plane and tmux-backed Node processes while a
small local HTTP fixture stands in for OpenCode/provider variability.  They do
not make an external model request and do not establish provider policy facts.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.tmux]


@contextmanager
def _fresh_browser(system: Any) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=system.base_url,
        headers={"Origin": system.base_url},
        timeout=10,
    ) as client:
        login = client.post(
            "/api/v1/admin/sessions",
            json={"admin_token": system.admin_token},
        )
        assert login.status_code == 201, login.text
        yield client


def _wait_for_assistant_message(
    system: Any,
    conversation_id: UUID,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
            headers=system.admin_headers,
            timeout=2,
        )
        assert response.status_code == 200, response.text
        for message in response.json()["messages"]:
            if message["role"] == "assistant" and message["is_final"]:
                return message
        time.sleep(0.05)
    raise AssertionError("assistant message was not assembled")


def _wait_for_runtime_readiness(
    system: Any,
    term_id: UUID,
    expected: str,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{system.base_url}/api/v1/agent/admin/setup",
            headers=system.admin_headers,
            params={"term_id": str(term_id)},
            timeout=2,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        runtime = body.get("runtime")
        if runtime is not None and runtime["readiness"] == expected:
            return body
        time.sleep(0.05)
    raise AssertionError(f"runtime did not reach {expected}")


def _wait_until(
    predicate: Any,
    *,
    description: str,
    timeout: float = 10,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(description)


def _database_run_delivery_states(
    system: Any,
    conversation_id: UUID,
) -> tuple[list[str], list[str]]:
    with sqlite3.connect(
        f"file:{system.database_path}?mode=ro",
        uri=True,
    ) as connection:
        delivery_states = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT delivery_state
                FROM agent_inbox_items
                WHERE conversation_id = ?
                ORDER BY admission_seq
                """,
                (conversation_id.hex,),
            )
        ]
        run_states = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT run_state
                FROM agent_runs
                WHERE conversation_id = ?
                ORDER BY started_at
                """,
                (conversation_id.hex,),
            )
        ]
    return delivery_states, run_states


def _provision_ready_agent(
    agent_product_system: Any,
    *,
    name: str,
) -> tuple[Any, str, dict[str, Any]]:
    system = agent_product_system.system
    runtime = agent_product_system.runtime
    system.login(system.create_enrollment())
    instance = system.new_and_detach(name)
    assert system.wait_until_online(instance.instance_id)
    pane_id = system.first_pane_id(instance.instance_id)
    runtime.start()

    with _fresh_browser(system) as browser:
        preview = browser.get(
            "/api/v1/agent/admin/setup",
            params={"term_id": str(instance.instance_id)},
        )
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        disclosure = preview_body["disclosure"]
        assert preview_body["state"] == "unconfigured"
        assert disclosure is not None and disclosure["accepted"] is False

        setup = browser.post(
            "/api/v1/agent/admin/setup",
            json={
                "term_id": str(instance.instance_id),
                "profile_display_name": f"{name}-profile",
                "pane_ids": [pane_id],
                "topology_revision": preview_body["topology_revision"],
                "disclosure_fingerprint": disclosure["disclosure_fingerprint"],
                "accepted": True,
                "idempotency_key": str(uuid4()),
            },
        )
        assert setup.status_code == 200, setup.text
        setup_body = setup.json()
        assert setup_body["state"] == "ready"
        assert setup_body["runtime"]["readiness"] == "ready"
        assert setup_body["runtime"]["provider_readiness"] == "configured_unverified"
    return instance, pane_id, setup_body


def _mcp_initialize(system: Any, token: str, *, host: str | None = None) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }
    if host is not None:
        headers["Host"] = host
    return httpx.post(
        f"{system.base_url}/api/v1/agent/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "agent-e2e", "version": "1"},
            },
        },
        timeout=10,
    )


def test_product_setup_activates_pipeline_without_b_restart(
    agent_product_system: Any,
) -> None:
    system = agent_product_system.system
    runtime = agent_product_system.runtime
    original_pid = system.control_process_id
    assert system.control_start_count == 1
    instance, _pane_id, setup_body = _provision_ready_agent(
        agent_product_system,
        name="agent-product-setup",
    )
    binding_id = UUID(setup_body["binding_id"])

    assert system.control_start_count == 1
    assert system.control_process_id == original_pid

    conversation = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations",
        headers=system.admin_headers,
        json={"binding_id": str(binding_id), "title": "deterministic turn"},
        timeout=10,
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = UUID(conversation.json()["conversation_id"])
    admitted = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "return the deterministic fixture result"},
        timeout=10,
    )
    assert admitted.status_code == 202, admitted.text

    assistant = _wait_for_assistant_message(system, conversation_id)
    assert assistant["body"] == "fixture-result-1"
    assert runtime.session_create_calls == 1
    assert runtime.prompt_calls == 1

    setup_after_turn = httpx.get(
        f"{system.base_url}/api/v1/agent/admin/setup",
        headers=system.admin_headers,
        params={"term_id": str(instance.instance_id)},
        timeout=10,
    )
    assert setup_after_turn.status_code == 200, setup_after_turn.text
    assert setup_after_turn.json()["runtime"]["provider_readiness"] == "verified"


def test_runtime_health_drift_unmaps_then_recovers(agent_product_system: Any) -> None:
    system = agent_product_system.system
    runtime = agent_product_system.runtime
    original_pid = system.control_process_id
    instance, pane_id, setup_body = _provision_ready_agent(
        agent_product_system,
        name="agent-health-drift",
    )
    binding_id = UUID(setup_body["binding_id"])

    conversation = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations",
        headers=system.admin_headers,
        json={"binding_id": str(binding_id), "title": "health drift"},
        timeout=10,
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = UUID(conversation.json()["conversation_id"])

    runtime.set_health(healthy=False)
    unavailable = _wait_for_runtime_readiness(
        system,
        instance.instance_id,
        "not_ready",
    )
    assert unavailable["runtime"]["reason_code"] == "runtime_unreachable"
    prompt_calls_before = runtime.prompt_calls
    fenced = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "must not reach the unavailable runtime"},
        timeout=10,
    )
    assert fenced.status_code == 503, fenced.text
    assert fenced.json()["error"]["code"] == "binding_runtime_unavailable"
    assert runtime.prompt_calls == prompt_calls_before

    runtime.set_health(healthy=True)
    with _fresh_browser(system) as browser:
        recovered = browser.post(
            "/api/v1/agent/admin/setup",
            json={
                "term_id": str(instance.instance_id),
                "profile_id": setup_body["profile"]["profile_id"],
                "pane_ids": [pane_id],
                "topology_revision": unavailable["topology_revision"],
                "disclosure_fingerprint": unavailable["disclosure"]["disclosure_fingerprint"],
                "accepted": True,
                "idempotency_key": str(uuid4()),
            },
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["runtime"]["readiness"] == "ready"

    admitted = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "the recovered runtime may answer"},
        timeout=10,
    )
    assert admitted.status_code == 202, admitted.text
    assert _wait_for_assistant_message(system, conversation_id)["body"] == "fixture-result-1"
    assert runtime.prompt_calls == prompt_calls_before + 1
    assert system.control_start_count == 1
    assert system.control_process_id == original_pid


def test_sse_disconnect_reconciles_replays_and_deduplicates(
    agent_product_system: Any,
) -> None:
    system = agent_product_system.system
    runtime = agent_product_system.runtime
    _instance, _pane_id, setup_body = _provision_ready_agent(
        agent_product_system,
        name="agent-disconnect-replay",
    )
    binding_id = UUID(setup_body["binding_id"])
    conversation = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations",
        headers=system.admin_headers,
        json={"binding_id": str(binding_id), "title": "disconnect replay"},
        timeout=10,
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = UUID(conversation.json()["conversation_id"])

    _wait_until(
        lambda: runtime.sse_connections >= 1,
        description="initial fake OpenCode SSE connection was not established",
    )
    runtime.disconnect_next_sse_after(event_count=1)
    admitted = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "complete once across an SSE reconnect"},
        timeout=10,
    )
    assert admitted.status_code == 202, admitted.text
    assert _wait_for_assistant_message(system, conversation_id)["body"] == ("fixture-result-1")
    _wait_until(
        lambda: runtime.sse_connections >= 2 and runtime.reconcile_calls >= 1,
        description="pipeline did not reconcile and reconnect",
    )

    messages = httpx.get(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        timeout=10,
    )
    assert messages.status_code == 200, messages.text
    assistant_messages = [
        message for message in messages.json()["messages"] if message["role"] == "assistant"
    ]
    assert [message["body"] for message in assistant_messages] == ["fixture-result-1"]
    events = httpx.get(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/events",
        headers=system.admin_headers,
        params={"limit": 200},
        timeout=10,
    )
    assert events.status_code == 200, events.text
    event_ids = [event["event_id"] for event in events.json()["events"]]
    assert len(event_ids) == len(set(event_ids))
    assert runtime.prompt_calls == 1
    assert runtime.reconcile_calls <= 2


def test_action_disconnect_parks_delivery_unknown_without_resubmit(
    agent_product_system: Any,
) -> None:
    system = agent_product_system.system
    runtime = agent_product_system.runtime
    _instance, _pane_id, setup_body = _provision_ready_agent(
        agent_product_system,
        name="agent-ambiguous-delivery",
    )
    binding_id = UUID(setup_body["binding_id"])
    conversation = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations",
        headers=system.admin_headers,
        json={"binding_id": str(binding_id), "title": "ambiguous delivery"},
        timeout=10,
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = UUID(conversation.json()["conversation_id"])

    _wait_until(
        lambda: runtime.sse_connections >= 1,
        description="initial fake OpenCode SSE connection was not established",
    )
    runtime.emit_tool_start_only_on_next_prompt()
    runtime.set_reconcile_available(False)
    runtime.disconnect_next_sse_after(event_count=2)
    admitted = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "begin one fixture tool action"},
        timeout=10,
    )
    assert admitted.status_code == 202, admitted.text

    _wait_until(
        lambda: (
            _database_run_delivery_states(system, conversation_id)
            == (["delivery_unknown"], ["unknown"])
        ),
        description="ambiguous action did not reach durable delivery_unknown",
        timeout=15,
    )
    _wait_until(
        lambda: runtime.sse_connections >= 2,
        description="pipeline did not reconnect after the ambiguous action",
        timeout=15,
    )
    events = httpx.get(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/events",
        headers=system.admin_headers,
        params={"limit": 200},
        timeout=10,
    )
    assert events.status_code == 200, events.text
    assert "tool_started" in [event["event_kind"] for event in events.json()["events"]]
    assert runtime.reconcile_calls == 2
    prompt_calls = runtime.prompt_calls
    time.sleep(0.5)
    assert runtime.prompt_calls == prompt_calls == 1
    with sqlite3.connect(f"file:{system.database_path}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_tool_requests").fetchone() == (0,)


def test_recovery_failure_keeps_core_up_and_inbox_unclaimed(
    agent_migration_failure_system: Any,
) -> None:
    system = agent_migration_failure_system.system
    assert httpx.get(f"{system.base_url}/healthz", timeout=2).status_code == 200
    dashboard = httpx.get(
        f"{system.base_url}/api/v1/dashboard",
        headers=system.admin_headers,
        timeout=2,
    )
    assert dashboard.status_code == 200, dashboard.text

    capabilities = httpx.get(
        f"{system.base_url}/api/v1/agent/capabilities",
        timeout=2,
    )
    assert capabilities.status_code == 200, capabilities.text
    assert capabilities.json()["state"] == "degraded"
    assert capabilities.json()["reason_code"] == "recovery_failed"
    blocked = httpx.get(
        f"{system.base_url}/api/v1/agent/admin/profiles",
        headers=system.admin_headers,
        timeout=2,
    )
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["error"]["code"] == "recovery_failed"

    with sqlite3.connect(
        f"file:{system.database_path}?mode=ro",
        uri=True,
    ) as connection:
        inbox = connection.execute(
            """
            SELECT delivery_state, claim_owner, attempt_count
            FROM agent_inbox_items
            WHERE id = ?
            """,
            (agent_migration_failure_system.inbox_id.hex,),
        ).fetchone()
    assert inbox == ("pending", None, 0)


def test_stale_epoch_wrong_host_and_revoked_token_fail_closed(
    agent_product_system: Any,
) -> None:
    system = agent_product_system.system
    _instance, _pane_id, setup_body = _provision_ready_agent(
        agent_product_system,
        name="agent-capability-fences",
    )
    binding_id = UUID(setup_body["binding_id"])
    current_epoch = int(setup_body["runtime"]["runtime_epoch"])

    assert _mcp_initialize(system, agent_product_system.mcp_token).status_code == 200
    wrong_host = _mcp_initialize(
        system,
        agent_product_system.mcp_token,
        host="attacker.invalid",
    )
    assert wrong_host.status_code == 421

    bootstrap_digest = hashlib.sha256(agent_product_system.mcp_token.encode()).hexdigest()
    with sqlite3.connect(system.database_path) as connection:
        changed = connection.execute(
            "UPDATE agent_tokens SET binding_epoch = ? WHERE token_hash = ?",
            (current_epoch + 1, bootstrap_digest),
        )
        assert changed.rowcount == 1
        connection.commit()
    assert _mcp_initialize(system, agent_product_system.mcp_token).status_code == 401
    with sqlite3.connect(system.database_path) as connection:
        connection.execute(
            "UPDATE agent_tokens SET binding_epoch = ? WHERE token_hash = ?",
            (current_epoch, bootstrap_digest),
        )
        connection.commit()

    issued = httpx.post(
        f"{system.base_url}/api/v1/agent/admin/tokens",
        headers=system.admin_headers,
        json={
            "binding_id": str(binding_id),
            "scopes": ["terminal.observe"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
        timeout=10,
    )
    assert issued.status_code == 201
    issued_body = issued.json()
    issued_token = issued_body["raw_token"]
    assert _mcp_initialize(system, issued_token).status_code == 200
    revoked = httpx.post(
        f"{system.base_url}/api/v1/agent/admin/tokens/{issued_body['token_id']}/revoke",
        headers=system.admin_headers,
        timeout=10,
    )
    assert revoked.status_code == 200
    assert _mcp_initialize(system, issued_token).status_code == 401

    binding_token = httpx.post(
        f"{system.base_url}/api/v1/agent/admin/tokens",
        headers=system.admin_headers,
        json={
            "binding_id": str(binding_id),
            "scopes": ["terminal.observe"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
        timeout=10,
    )
    assert binding_token.status_code == 201
    binding_raw_token = binding_token.json()["raw_token"]
    closed = httpx.patch(
        f"{system.base_url}/api/v1/agent/admin/bindings/{binding_id}",
        headers=system.admin_headers,
        json={"status": "revoked"},
        timeout=10,
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "revoked"
    assert _mcp_initialize(system, binding_raw_token).status_code == 401


def test_revoke_during_turn_fences_active_run_without_side_effect(
    agent_product_system: Any,
) -> None:
    """A binding revoke must fence an already-running turn before any A write."""

    system = agent_product_system.system
    runtime = agent_product_system.runtime
    _instance, _pane_id, setup_body = _provision_ready_agent(
        agent_product_system,
        name="agent-revoke-during-turn",
    )
    binding_id = UUID(setup_body["binding_id"])
    conversation = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations",
        headers=system.admin_headers,
        json={"binding_id": str(binding_id), "title": "revoke during turn"},
        timeout=10,
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = UUID(conversation.json()["conversation_id"])

    # The fixture emits only a busy boundary and a tool-start notification, so
    # the run remains active while the administrator revokes the Binding.
    runtime.emit_tool_start_only_on_next_prompt()
    admitted = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "begin one turn that must be fenced"},
        timeout=10,
    )
    assert admitted.status_code == 202, admitted.text
    _wait_until(
        lambda: (
            _database_run_delivery_states(system, conversation_id) == (["dispatched"], ["running"])
        ),
        description="fixture turn did not reach the active running boundary",
        timeout=15,
    )
    with sqlite3.connect(f"file:{system.database_path}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_tool_requests WHERE binding_id = ?",
            (binding_id.hex,),
        ).fetchone() == (0,)

    revoked = httpx.patch(
        f"{system.base_url}/api/v1/agent/admin/bindings/{binding_id}",
        headers=system.admin_headers,
        json={"status": "revoked"},
        timeout=10,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"

    # The same conversation and capability are unusable immediately after
    # the fence; a cancelled adapter must not cause a second submission.
    blocked = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "must be rejected after revoke"},
        timeout=10,
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"]["code"] == "binding_revoked"
    assert _mcp_initialize(system, agent_product_system.mcp_token).status_code == 401

    prompt_calls = runtime.prompt_calls
    time.sleep(0.5)
    assert runtime.prompt_calls == prompt_calls == 1
    with sqlite3.connect(f"file:{system.database_path}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_tool_requests WHERE binding_id = ?",
            (binding_id.hex,),
        ).fetchone() == (0,)


def test_cleanup_outage_returns_202_then_receipts_complete(
    agent_product_system: Any,
) -> None:
    system = agent_product_system.system
    runtime = agent_product_system.runtime
    _instance, _pane_id, setup_body = _provision_ready_agent(
        agent_product_system,
        name="agent-cleanup-outage",
    )
    binding_id = UUID(setup_body["binding_id"])

    conversation = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations",
        headers=system.admin_headers,
        json={"binding_id": str(binding_id), "title": "cleanup outage"},
        timeout=10,
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = UUID(conversation.json()["conversation_id"])
    admitted = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "create a backend session before deletion"},
        timeout=10,
    )
    assert admitted.status_code == 202, admitted.text
    assert _wait_for_assistant_message(system, conversation_id)["body"] == ("fixture-result-1")

    runtime.delete_available = False
    first_delete = httpx.delete(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert first_delete.status_code == 202, first_delete.text
    first_body = first_delete.json()
    assert first_body["state"] == "deletion_pending"
    cleanup_job_id = UUID(first_body["cleanup_job_id"])
    assert runtime.delete_calls == 1

    still_present = httpx.get(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert still_present.status_code == 200, still_present.text
    pending = httpx.get(
        f"{system.base_url}/api/v1/agent/admin/cleanup-jobs/{cleanup_job_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["state"] == "pending"
    assert any(
        receipt["reason_code"] == "cleanup_retry_scheduled"
        for receipt in pending.json()["receipts"]
        if receipt["state"] == "pending"
    )

    runtime.delete_available = True
    second_delete = httpx.delete(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert second_delete.status_code == 202, second_delete.text
    assert UUID(second_delete.json()["cleanup_job_id"]) == cleanup_job_id
    assert runtime.delete_calls == 2

    after_runtime_cleanup = httpx.get(
        f"{system.base_url}/api/v1/agent/admin/cleanup-jobs/{cleanup_job_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert after_runtime_cleanup.status_code == 200, after_runtime_cleanup.text
    receipts = after_runtime_cleanup.json()["receipts"]
    assert after_runtime_cleanup.json()["state"] == "pending"
    assert any(
        receipt["artifact_kind"] == "backend_session" and receipt["state"] == "confirmed"
        for receipt in receipts
    )
    provider_receipt = next(
        receipt for receipt in receipts if receipt["artifact_kind"] == "provider_retention"
    )
    assert provider_receipt["state"] == "pending"

    with sqlite3.connect(
        f"file:{system.database_path}?mode=ro",
        uri=True,
    ) as connection:
        provider_row = connection.execute(
            """
            SELECT artifact_ref
            FROM agent_cleanup_receipts
            WHERE id = ? AND cleanup_job_id = ?
            """,
            (UUID(provider_receipt["receipt_id"]).hex, cleanup_job_id.hex),
        ).fetchone()
    assert provider_row is not None
    provider_artifact_ref = str(provider_row[0])

    confirmed = httpx.post(
        (
            f"{system.base_url}/api/v1/agent/admin/cleanup-jobs/{cleanup_job_id}"
            f"/receipts/{provider_receipt['receipt_id']}/confirm"
        ),
        headers={"Authorization": f"Bearer {agent_product_system.cleanup_helper_token}"},
        json={
            "artifact_ref": provider_artifact_ref,
            "result": "confirmed",
            "evidence_digest": "a" * 64,
            "reason_code": None,
            "idempotency_key": str(uuid4()),
        },
        timeout=10,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["state"] == "confirmed"

    completed = httpx.get(
        f"{system.base_url}/api/v1/agent/admin/cleanup-jobs/{cleanup_job_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "completed"
    final_delete = httpx.delete(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert final_delete.status_code == 204, final_delete.text


def test_cleanup_dead_letter_receipt_stays_visible(
    agent_product_system: Any,
) -> None:
    system = agent_product_system.system
    runtime = agent_product_system.runtime
    _instance, _pane_id, setup_body = _provision_ready_agent(
        agent_product_system,
        name="agent-cleanup-dead-letter",
    )
    binding_id = UUID(setup_body["binding_id"])
    conversation = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations",
        headers=system.admin_headers,
        json={"binding_id": str(binding_id), "title": "cleanup dead letter"},
        timeout=10,
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = UUID(conversation.json()["conversation_id"])
    admitted = httpx.post(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}/messages",
        headers=system.admin_headers,
        json={"text": "create a session for dead-letter evidence"},
        timeout=10,
    )
    assert admitted.status_code == 202, admitted.text
    assert _wait_for_assistant_message(system, conversation_id)["body"] == ("fixture-result-1")

    deleted = httpx.delete(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert deleted.status_code == 202, deleted.text
    job_id = UUID(deleted.json()["cleanup_job_id"])
    assert runtime.delete_calls == 1
    job = httpx.get(
        f"{system.base_url}/api/v1/agent/admin/cleanup-jobs/{job_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert job.status_code == 200, job.text
    assert job.json()["state"] == "pending"
    provider_receipt = next(
        receipt
        for receipt in job.json()["receipts"]
        if receipt["artifact_kind"] == "provider_retention"
    )
    with sqlite3.connect(f"file:{system.database_path}?mode=ro", uri=True) as connection:
        provider_ref_row = connection.execute(
            """
            SELECT artifact_ref
            FROM agent_cleanup_receipts
            WHERE id = ? AND cleanup_job_id = ?
            """,
            (UUID(provider_receipt["receipt_id"]).hex, job_id.hex),
        ).fetchone()
    assert provider_ref_row is not None

    dead_letter = httpx.post(
        (
            f"{system.base_url}/api/v1/agent/admin/cleanup-jobs/{job_id}"
            f"/receipts/{provider_receipt['receipt_id']}/confirm"
        ),
        headers={"Authorization": f"Bearer {agent_product_system.cleanup_helper_token}"},
        json={
            "artifact_ref": str(provider_ref_row[0]),
            "result": "dead_letter",
            "reason_code": "provider_cleanup_unavailable",
            "idempotency_key": str(uuid4()),
        },
        timeout=10,
    )
    assert dead_letter.status_code == 200, dead_letter.text
    assert dead_letter.json()["state"] == "dead_letter"
    assert dead_letter.json()["reason_code"] == "cleanup_dead_letter"

    for _ in range(2):
        visible = httpx.get(
            f"{system.base_url}/api/v1/agent/admin/cleanup-jobs/{job_id}",
            headers=system.admin_headers,
            timeout=10,
        )
        assert visible.status_code == 200, visible.text
        assert visible.json()["state"] == "dead_letter"
        assert any(
            receipt["state"] == "dead_letter" and receipt["reason_code"] == "cleanup_dead_letter"
            for receipt in visible.json()["receipts"]
        )

    repeat_delete = httpx.delete(
        f"{system.base_url}/api/v1/agent/conversations/{conversation_id}",
        headers=system.admin_headers,
        timeout=10,
    )
    assert repeat_delete.status_code == 202, repeat_delete.text
    assert UUID(repeat_delete.json()["cleanup_job_id"]) == job_id
