from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import PaneObservationCursor
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.watches import (
    WatchContract,
    encode_watch_contract,
)
from termflow_protocol.mcp import PaneCursor, WatchCondition, WatchConditionKind

pytestmark = [pytest.mark.e2e, pytest.mark.tmux]


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, object] | None = None,
) -> dict[str, Any]:
    response = httpx.request(
        method,
        url,
        headers=headers,
        json=body,
        timeout=10,
    )
    assert response.status_code < 300, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


@contextmanager
def _fresh_browser_client(termflow_system: Any) -> Iterator[httpx.Client]:
    """Create a cookie session whose strong-auth timestamp is process-local."""

    with httpx.Client(
        base_url=termflow_system.base_url,
        headers={"Origin": termflow_system.base_url},
        timeout=10,
    ) as client:
        response = client.post(
            "/api/v1/admin/sessions",
            json={"admin_token": termflow_system.admin_token},
        )
        assert response.status_code == 201, response.text
        yield client


async def _seed_restart_state(
    database_path: Path,
    *,
    binding_id: UUID,
    conversation_id: UUID,
    instance_id: UUID,
    pane_id: str,
) -> int:
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    await database.initialize()
    repositories = RepositoryBundle(database.session_factory)
    try:
        async with database.session_factory() as session:
            observed = await session.get(
                PaneObservationCursor,
                (instance_id, pane_id),
            )
            assert observed is not None, "Watch engine did not persist the pane cursor"
            start_cursor = PaneCursor(
                instance_id=instance_id,
                pane_id=pane_id,
                pane_incarnation=int(observed.pane_incarnation),
                stream_id=UUID(observed.stream_id),
                seq=observed.seq,
            )

        condition = WatchCondition(
            kind=WatchConditionKind.OUTPUT_CONTAINS,
            match="AGENT_WATCH_TRIGGER",
        )
        contract = WatchContract(
            condition=condition,
            start_cursor=start_cursor,
            intent_summary="continue after the cross-process marker",
        )
        await repositories.watches.create(
            binding_id=binding_id,
            conversation_id=conversation_id,
            pane_id=pane_id,
            condition_kind=condition.kind.value,
            start_cursor=encode_watch_contract(contract),
            intent_summary=contract.intent_summary,
            one_shot=True,
        )

        auth_epoch = (await repositories.auth_state.get()).epoch
        for index, kind in enumerate(("run_started", "run_completed"), start=1):
            await repositories.agent_events.append(
                conversation_id=conversation_id,
                event_kind=kind,
                dedup_key=f"cross-process-event-{index}",
                payload_digest=hashlib.sha256(kind.encode()).hexdigest(),
            )
        return auth_epoch
    finally:
        await database.dispose()


async def _seed_live_approval(
    database_path: Path,
    *,
    binding_id: UUID,
    conversation_id: UUID,
    pane_id: str,
    auth_epoch: int,
) -> UUID:
    """Create test input after restart recovery has fenced orphaned approvals."""
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    repositories = RepositoryBundle(database.session_factory)
    try:
        approval = await repositories.approvals.create(
            binding_id=binding_id,
            conversation_id=conversation_id,
            tool_call_id="cross-process-tool-call",
            canonical_hash=hashlib.sha256(b"cross-process-approval").hexdigest(),
            auth_epoch=auth_epoch,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            pane_id=pane_id,
            operation="key",
            intent_summary="approve a bounded terminal write",
        )
        return approval.id
    finally:
        await database.dispose()


def _wait_for_sqlite_row(
    database_path: Path,
    statement: str,
    parameters: tuple[object, ...],
    *,
    timeout: float = 10,
) -> tuple[object, ...]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
            row = connection.execute(statement, parameters).fetchone()
        if row is not None:
            return cast(tuple[object, ...], row)
        time.sleep(0.05)
    raise AssertionError(f"SQLite row did not appear: {statement}")


def _consume_sse(
    url: str,
    headers: dict[str, str],
    frames: queue.Queue[tuple[str, dict[str, object]]],
) -> None:
    try:
        timeout = httpx.Timeout(connect=5, read=10, write=5, pool=5)
        with httpx.stream("GET", url, headers=headers, timeout=timeout) as response:
            response.raise_for_status()
            event_name = ""
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event_name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    payload = json.loads(line.removeprefix("data: "))
                    frames.put((event_name, payload))
                    if event_name == "closed":
                        return
    except Exception as exc:  # pragma: no cover - reported in the parent thread
        frames.put(("error", {"message": repr(exc)}))


def test_agent_broker_durable_workflows_across_control_plane_restart(
    termflow_system: Any,
) -> None:
    termflow_system.login(termflow_system.create_enrollment())
    instance = termflow_system.new_and_detach("agent-broker-process")
    assert termflow_system.wait_until_online(instance.instance_id)
    pane_id = termflow_system.first_pane_id(instance.instance_id)

    profile = _request_json(
        "POST",
        f"{termflow_system.base_url}/api/v1/agent/admin/profiles",
        headers=termflow_system.admin_headers,
        body={
            "display_name": "cross-process-opencode",
            "backend_kind": "opencode",
            "config": '{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        },
    )
    binding = _request_json(
        "POST",
        f"{termflow_system.base_url}/api/v1/agent/admin/bindings",
        headers=termflow_system.admin_headers,
        body={
            "profile_id": profile["profile_id"],
            "term_id": str(instance.instance_id),
        },
    )
    binding_id = UUID(binding["binding_id"])
    with _fresh_browser_client(termflow_system) as browser:
        activated = browser.post(
            f"/api/v1/agent/admin/bindings/{binding_id}/activate",
            json={},
        )
        assert activated.status_code == 202, activated.text
        assert activated.json()["status"] == "enabled"
    conversation = _request_json(
        "POST",
        f"{termflow_system.base_url}/api/v1/agent/conversations",
        headers=termflow_system.admin_headers,
        body={"binding_id": str(binding_id), "title": "cross-process"},
    )
    conversation_id = UUID(conversation["conversation_id"])

    events = termflow_system.subscribe(instance.instance_id)
    try:
        termflow_system.send_text(
            instance.instance_id,
            pane_id,
            "printf WATCH_START_CURSOR",
            submit=True,
        )
        assert events.wait_for_bytes(b"WATCH_START_CURSOR", timeout=10)
    finally:
        events.connection.close()

    termflow_system.stop_control_plane()
    auth_epoch = asyncio.run(
        _seed_restart_state(
            termflow_system.database_path,
            binding_id=binding_id,
            conversation_id=conversation_id,
            instance_id=instance.instance_id,
            pane_id=pane_id,
        )
    )
    termflow_system.start_control_plane()
    assert termflow_system.wait_until_online(instance.instance_id, timeout=15)
    approval_id = asyncio.run(
        _seed_live_approval(
            termflow_system.database_path,
            binding_id=binding_id,
            conversation_id=conversation_id,
            pane_id=pane_id,
            auth_epoch=auth_epoch,
        )
    )

    resumed_events = termflow_system.subscribe(instance.instance_id)
    try:
        termflow_system.send_text(
            instance.instance_id,
            pane_id,
            "printf AGENT_WATCH_TRIGGER",
            submit=True,
        )
        assert resumed_events.wait_for_bytes(b"AGENT_WATCH_TRIGGER", timeout=10)
    finally:
        resumed_events.connection.close()

    inbox = _wait_for_sqlite_row(
        termflow_system.database_path,
        """
        SELECT kind, delivery_state, source
        FROM agent_inbox_items
        WHERE conversation_id = ? AND kind = 'watch_triggered'
        """,
        (conversation_id.hex,),
    )
    assert inbox == ("watch_triggered", "pending", "system")
    delivery = _wait_for_sqlite_row(
        termflow_system.database_path,
        """
        SELECT COUNT(*)
        FROM watch_deliveries AS delivery
        JOIN agent_inbox_items AS inbox ON inbox.id = delivery.inbox_item_id
        WHERE inbox.conversation_id = ?
        HAVING COUNT(*) = 1
        """,
        (conversation_id.hex,),
    )
    assert delivery == (1,)

    not_fresh = httpx.post(
        f"{termflow_system.base_url}/api/v1/agent/approvals/{approval_id}/decide",
        headers=termflow_system.admin_headers,
        json={"decision": "approve"},
        timeout=10,
    )
    assert not_fresh.status_code == 428, not_fresh.text
    assert not_fresh.json()["error"]["code"] == "approval_reauthentication_required"

    with _fresh_browser_client(termflow_system) as browser:
        approved_response = browser.post(
            f"/api/v1/agent/approvals/{approval_id}/decide",
            json={"decision": "approve"},
        )
        assert approved_response.status_code == 200, approved_response.text
        approved = approved_response.json()
    assert approved["state"] == "approved"
    decided_twice = httpx.post(
        f"{termflow_system.base_url}/api/v1/agent/approvals/{approval_id}/decide",
        headers=termflow_system.admin_headers,
        json={"decision": "deny"},
        timeout=10,
    )
    assert decided_twice.status_code == 409
    assert decided_twice.json()["error"]["code"] == "approval_already_decided"

    first_page = _request_json(
        "GET",
        (
            f"{termflow_system.base_url}/api/v1/agent/conversations/"
            f"{conversation_id}/events?since=0&limit=1"
        ),
        headers=termflow_system.admin_headers,
    )
    assert [event["database_seq"] for event in first_page["events"]] == [1]
    second_page = _request_json(
        "GET",
        (
            f"{termflow_system.base_url}/api/v1/agent/conversations/"
            f"{conversation_id}/events?since={first_page['next_cursor']}"
        ),
        headers=termflow_system.admin_headers,
    )
    assert [event["database_seq"] for event in second_page["events"]] == [2]

    frames: queue.Queue[tuple[str, dict[str, object]]] = queue.Queue()
    stream_url = (
        f"{termflow_system.base_url}/api/v1/agent/stream"
        f"?conversation_id={conversation_id}&cursor={auth_epoch}-1"
    )
    stream_thread = threading.Thread(
        target=_consume_sse,
        args=(stream_url, termflow_system.admin_headers, frames),
        daemon=True,
    )
    stream_thread.start()
    replay_name, replay_payload = frames.get(timeout=10)
    assert replay_name == "agent_event", replay_payload
    assert replay_payload["cursor"] == f"{auth_epoch}-2"

    revoked = _request_json(
        "PATCH",
        f"{termflow_system.base_url}/api/v1/agent/admin/bindings/{binding_id}",
        headers=termflow_system.admin_headers,
        body={"status": "revoked"},
    )
    assert revoked["status"] == "revoked"
    closed_name, closed_payload = frames.get(timeout=10)
    assert closed_name == "closed", closed_payload
    assert closed_payload == {
        "type": "closed",
        "code": 4412,
        "reason": "binding_revoked",
    }
    stream_thread.join(timeout=5)
    assert not stream_thread.is_alive()

    deleted = httpx.delete(
        f"{termflow_system.base_url}/api/v1/agent/conversations/{conversation_id}",
        headers=termflow_system.admin_headers,
        timeout=10,
    )
    assert deleted.status_code == 204, deleted.text
    tombstone = _wait_for_sqlite_row(
        termflow_system.database_path,
        """
        SELECT state, target_kind, target_ref
        FROM agent_cleanup_jobs
        WHERE target_kind = 'conversation' AND target_ref = ?
        """,
        (str(conversation_id),),
    )
    assert tombstone == ("completed", "conversation", str(conversation_id))
