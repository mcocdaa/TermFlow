"""Purge-sweep retention tests for canonical Agent events and messages.

Plan §15 (RepositoryBundle startup purge) and §16.1 (data retention
defaults): the sweep must bound ``agent_events`` - especially the
high-frequency ephemeral ``MESSAGE_DELTA`` deltas - and ``agent_messages``
with ceilings wired from the executable ``RETENTION_MATRIX`` contract, not
hard-coded duplicates of the matrix values.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from termflow_control_plane.agent_contracts import RETENTION_MATRIX, DataClass
from termflow_control_plane.persistence.models import AgentEvent, AgentMessage
from termflow_control_plane.persistence.repositories import (
    AGENT_ASSEMBLY_CHECKPOINT_RETENTION,
    AGENT_FINAL_TIMELINE_RETENTION,
    RepositoryBundle,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _seed_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> UUID:
    """Create a real profile + binding chain (FK enforcement is on)."""
    profile = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": "opencode",
            "backend_kind": "opencode",
            "config": '{"model": "default"}',
        },
    )
    assert profile.status_code == 201, profile.text
    term = provision_term(name="purge-term")
    binding = client.post(
        "/api/v1/agent/admin/bindings",
        headers=admin_headers,
        json={"profile_id": profile.json()["profile_id"], "term_id": str(term.instance_id)},
    )
    assert binding.status_code == 201, binding.text
    return UUID(str(binding.json()["binding_id"]))


async def _seed_conversation(
    client: TestClient,
    binding_id: UUID,
) -> UUID:
    repositories: RepositoryBundle = client.app.state.repositories
    conversation = await repositories.agent_conversations.create(
        binding_id=binding_id,
        title="purge-test",
    )
    return conversation.id


async def _seed_event(
    client: TestClient,
    *,
    conversation_id: UUID,
    dedup_key: str,
    database_seq: int,
    ephemeral: bool,
    created_at: datetime,
) -> UUID:
    session_factory = client.app.state.session_factory
    async with session_factory() as session:
        event = AgentEvent(
            conversation_id=conversation_id,
            event_kind="message_delta" if ephemeral else "message_completed",
            dedup_key=dedup_key,
            database_seq=database_seq,
            payload_digest=_digest(dedup_key),
            ephemeral=ephemeral,
            created_at=created_at,
        )
        session.add(event)
        await session.commit()
        return event.id


async def _seed_message(
    client: TestClient,
    *,
    conversation_id: UUID,
    role: str,
    assembly_revision: int,
    is_final: bool,
    created_at: datetime,
) -> UUID:
    session_factory = client.app.state.session_factory
    async with session_factory() as session:
        message = AgentMessage(
            conversation_id=conversation_id,
            role=role,
            kind="text",
            assembly_revision=assembly_revision,
            is_final=is_final,
            body_digest=_digest(f"{role}-{assembly_revision}"),
            created_at=created_at,
        )
        session.add(message)
        await session.commit()
        return message.id


async def _purge(client: TestClient, *, now: datetime) -> dict[str, int]:
    repositories: RepositoryBundle = client.app.state.repositories
    return await repositories.purge_expired(now=now)


async def _event_ids(client: TestClient, conversation_id: UUID) -> set[UUID]:
    session_factory = client.app.state.session_factory
    async with session_factory() as session:
        rows = await session.scalars(
            select(AgentEvent).where(AgentEvent.conversation_id == conversation_id)
        )
        return {row.id for row in rows}


async def _message_ids(client: TestClient, conversation_id: UUID) -> set[UUID]:
    session_factory = client.app.state.session_factory
    async with session_factory() as session:
        rows = await session.scalars(
            select(AgentMessage).where(AgentMessage.conversation_id == conversation_id)
        )
        return {row.id for row in rows}


def test_retention_constants_are_wired_from_the_contract_matrix() -> None:
    """The purge ceilings come from RETENTION_MATRIX, not local magic numbers."""
    assert AGENT_ASSEMBLY_CHECKPOINT_RETENTION == timedelta(hours=24)
    assert AGENT_FINAL_TIMELINE_RETENTION == timedelta(days=30)
    # Direct wiring proof: changing the matrix changes the sweep ceilings.
    assert (
        AGENT_ASSEMBLY_CHECKPOINT_RETENTION
        == RETENTION_MATRIX[DataClass.ASSEMBLY_CHECKPOINTS].retention
    )
    assert (
        AGENT_FINAL_TIMELINE_RETENTION
        == RETENTION_MATRIX[DataClass.FINAL_MESSAGES].retention
    )


def test_purge_sweeps_agent_events_by_retention_class(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    now = datetime.now(UTC)
    binding_id = _seed_binding(client, admin_headers, provision_term)

    async def scenario() -> None:
        conversation_id = await _seed_conversation(client, binding_id)

        # Ephemeral MESSAGE_DELTA deltas: 24h ceiling.
        old_ephemeral = await _seed_event(
            client,
            conversation_id=conversation_id,
            dedup_key="delta-old",
            database_seq=1,
            ephemeral=True,
            created_at=now - AGENT_ASSEMBLY_CHECKPOINT_RETENTION - timedelta(minutes=1),
        )
        fresh_ephemeral = await _seed_event(
            client,
            conversation_id=conversation_id,
            dedup_key="delta-fresh",
            database_seq=2,
            ephemeral=True,
            created_at=now - AGENT_ASSEMBLY_CHECKPOINT_RETENTION + timedelta(minutes=1),
        )
        # Durable timeline events: 30-day ceiling.
        old_durable = await _seed_event(
            client,
            conversation_id=conversation_id,
            dedup_key="completed-old",
            database_seq=3,
            ephemeral=False,
            created_at=now - AGENT_FINAL_TIMELINE_RETENTION - timedelta(minutes=1),
        )
        fresh_durable = await _seed_event(
            client,
            conversation_id=conversation_id,
            dedup_key="completed-fresh",
            database_seq=4,
            ephemeral=False,
            created_at=now - AGENT_FINAL_TIMELINE_RETENTION + timedelta(minutes=1),
        )

        counts = await _purge(client, now=now)
        assert counts["agent_events"] == 2

        remaining = await _event_ids(client, conversation_id)
        assert remaining == {fresh_ephemeral, fresh_durable}
        assert old_ephemeral not in remaining
        assert old_durable not in remaining

    client.portal.call(scenario)


def test_purge_sweeps_agent_messages_by_retention_class(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    now = datetime.now(UTC)
    binding_id = _seed_binding(client, admin_headers, provision_term)

    async def scenario() -> None:
        conversation_id = await _seed_conversation(client, binding_id)

        # Non-final assembly checkpoints: 24h ceiling.
        old_checkpoint = await _seed_message(
            client,
            conversation_id=conversation_id,
            role="assistant",
            assembly_revision=1,
            is_final=False,
            created_at=now - AGENT_ASSEMBLY_CHECKPOINT_RETENTION - timedelta(minutes=1),
        )
        fresh_checkpoint = await _seed_message(
            client,
            conversation_id=conversation_id,
            role="assistant",
            assembly_revision=2,
            is_final=False,
            created_at=now - AGENT_ASSEMBLY_CHECKPOINT_RETENTION + timedelta(minutes=1),
        )
        # Final product messages: 30-day ceiling.
        old_final = await _seed_message(
            client,
            conversation_id=conversation_id,
            role="assistant",
            assembly_revision=3,
            is_final=True,
            created_at=now - AGENT_FINAL_TIMELINE_RETENTION - timedelta(minutes=1),
        )
        fresh_final = await _seed_message(
            client,
            conversation_id=conversation_id,
            role="assistant",
            assembly_revision=4,
            is_final=True,
            created_at=now - AGENT_FINAL_TIMELINE_RETENTION + timedelta(minutes=1),
        )

        counts = await _purge(client, now=now)
        assert counts["agent_messages"] == 2

        remaining = await _message_ids(client, conversation_id)
        assert remaining == {fresh_checkpoint, fresh_final}
        assert old_checkpoint not in remaining
        assert old_final not in remaining

    client.portal.call(scenario)


def test_purge_leaves_fresh_rows_and_other_classes_untouched(
    client: TestClient, admin_headers: dict[str, str], provision_term
) -> None:
    """A sweep at ``now`` never touches rows inside their ceiling and reports
    zero for classes with nothing to sweep (no unbounded-side-effect scan)."""
    binding_id = _seed_binding(client, admin_headers, provision_term)

    async def scenario() -> None:
        conversation_id = await _seed_conversation(client, binding_id)
        fresh_event = await _seed_event(
            client,
            conversation_id=conversation_id,
            dedup_key="fresh",
            database_seq=1,
            ephemeral=True,
            created_at=datetime.now(UTC),
        )
        fresh_message = await _seed_message(
            client,
            conversation_id=conversation_id,
            role="assistant",
            assembly_revision=1,
            is_final=True,
            created_at=datetime.now(UTC),
        )

        counts = await _purge(client, now=datetime.now(UTC))
        assert counts["agent_events"] == 0
        assert counts["agent_messages"] == 0
        assert (await _event_ids(client, conversation_id)) == {fresh_event}
        assert (await _message_ids(client, conversation_id)) == {fresh_message}

    client.portal.call(scenario)
