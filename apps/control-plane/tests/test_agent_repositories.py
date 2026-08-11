"""Agent Broker persistence repository tests (plan §15, task M1.3).

These tests exercise the agent-domain repositories wired into
``RepositoryBundle`` against a fresh SQLite database created from
``Base.metadata``; the Alembic ``0006`` migration is a separate parallel task
and is intentionally not involved (the same approach the agent model tests
use).
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentConversation,
    AgentDiagnostic,
    AgentEvent,
    AgentProfile,
    AgentRun,
    Base,
)
from termflow_control_plane.persistence.repositories import (
    AgentBackendConversationRepository,
    AgentBindingRepository,
    AgentConversationRepository,
    AgentEventRepository,
    AgentInboxRepository,
    AgentMemoryScopeRepository,
    AgentMessageRepository,
    AgentProfileRepository,
    AgentRunRepository,
    AgentRuntimeBindingRepository,
    AgentTokenRepository,
    AgentToolRequestKeyConflict,
    AgentToolRequestRepository,
    ApprovalRepository,
    CleanupJobRepository,
    DiagnosticsRepository,
    PanePolicyRepository,
    RepositoryBundle,
    TranscriptDraftRepository,
    WatchDeliveryRepository,
    WatchRepository,
    digest_secret,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    bundle = RepositoryBundle(session_factory)
    # Test seam: let tests open ad-hoc sessions against the same database.
    bundle.session_factory = session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await engine.dispose()


async def _seed_profile(
    repos: RepositoryBundle, name: str | None = None
) -> AgentProfile:
    return await repos.agent_profiles.create(
        display_name=name or f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )


async def _seed_term(
    repos: RepositoryBundle, name: str | None = None
) -> UUID:
    display_name = name or f"term-{uuid4().hex[:8]}"
    installation = await repos.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    term = await repos.instances.register_or_rotate(
        uuid4(),
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    return term.id


async def _seed_binding(
    repos: RepositoryBundle,
    *,
    profile_id: UUID | None = None,
    term_id: UUID | None = None,
    status: str = "pending",
) -> AgentBinding:
    profile = await _seed_profile(repos) if profile_id is None else None
    term = await _seed_term(repos) if term_id is None else None
    return await repos.agent_bindings.create(
        profile_id=profile_id or (profile.id if profile else uuid4()),
        term_id=term_id or term,
        status=status,
    )


async def _seed_conversation(
    repos: RepositoryBundle,
    binding_id: UUID | None = None,
    *,
    title: str | None = None,
) -> AgentConversation:
    binding = await _seed_binding(repos) if binding_id is None else None
    return await repos.agent_conversations.create(
        binding_id=binding_id or (binding.id if binding else uuid4()),
        title=title,
    )


async def _seed_run(
    repos: RepositoryBundle,
    conversation_id: UUID,
    *,
    run_state: str = "queued",
) -> AgentRun:
    return await repos.agent_runs.create(
        conversation_id=conversation_id,
        run_state=run_state,
    )


# ---------------------------------------------------------------------------
# RepositoryBundle wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_bundle_exposes_all_agent_repositories(
    repositories: RepositoryBundle,
) -> None:
    exposed = {
        "agent_profiles": AgentProfileRepository,
        "agent_bindings": AgentBindingRepository,
        "agent_memory_scopes": AgentMemoryScopeRepository,
        "agent_conversations": AgentConversationRepository,
        "agent_backend_conversations": AgentBackendConversationRepository,
        "agent_runtime_bindings": AgentRuntimeBindingRepository,
        "agent_inbox": AgentInboxRepository,
        "agent_tool_requests": AgentToolRequestRepository,
        "agent_runs": AgentRunRepository,
        "agent_messages": AgentMessageRepository,
        "agent_events": AgentEventRepository,
        "agent_tokens": AgentTokenRepository,
        "pane_policies": PanePolicyRepository,
        "approvals": ApprovalRepository,
        "watches": WatchRepository,
        "agent_watch_deliveries": WatchDeliveryRepository,
        "transcript_drafts": TranscriptDraftRepository,
        "cleanup_jobs": CleanupJobRepository,
        "diagnostics": DiagnosticsRepository,
    }
    for attribute, repository_type in exposed.items():
        value = getattr(repositories, attribute)
        assert isinstance(value, repository_type), attribute


# ---------------------------------------------------------------------------
# Profiles and bindings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_lifecycle(repositories: RepositoryBundle) -> None:
    created = await _seed_profile(repositories, name="ops-assistant")
    assert created.display_name == "ops-assistant"
    assert created.backend_kind == "opencode"

    fetched = await repositories.agent_profiles.get_by_id(created.id)
    assert fetched is not None
    assert fetched.id == created.id

    renamed = await repositories.agent_profiles.rename(created.id, "dev-assistant")
    assert renamed is not None
    assert renamed.display_name == "dev-assistant"

    listed = await repositories.agent_profiles.list()
    assert [profile.id for profile in listed] == [created.id]

    assert await repositories.agent_profiles.delete(created.id) is True
    assert await repositories.agent_profiles.get_by_id(created.id) is None
    assert await repositories.agent_profiles.delete(created.id) is False


@pytest.mark.asyncio
async def test_binding_create_active_lookup_and_state_transition(
    repositories: RepositoryBundle,
) -> None:
    profile = await _seed_profile(repositories, name="binding-profile")
    term_id = await _seed_term(repositories, name="binding-term")

    binding = await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term_id,
        status="pending",
    )
    assert binding.status == "pending"
    assert binding.runtime_ref is None

    active = await repositories.agent_bindings.active_binding_for(profile.id, term_id)
    assert active is not None
    assert active.id == binding.id

    # The (profile, term, status) uniqueness means a second pending binding for
    # the same pair is rejected by the database.
    with pytest.raises(IntegrityError):
        await repositories.agent_bindings.create(
            profile_id=profile.id,
            term_id=term_id,
            status="pending",
        )

    # pending -> ready
    ready = await repositories.agent_bindings.set_status(
        binding.id, "ready", expected_status="pending"
    )
    assert ready is not None
    assert ready.status == "ready"
    # CAS rejects the same transition again.
    assert (
        await repositories.agent_bindings.set_status(
            binding.id, "ready", expected_status="pending"
        )
        is None
    )

    active = await repositories.agent_bindings.active_binding_for(profile.id, term_id)
    assert active is not None and active.id == binding.id

    # terminal state removes the binding from the active lookup.
    revoked = await repositories.agent_bindings.set_status(
        binding.id, "revoked", expected_status="ready"
    )
    assert revoked is not None
    assert await repositories.agent_bindings.active_binding_for(profile.id, term_id) is None


@pytest.mark.asyncio
async def test_binding_runtime_update_and_scoped_lists(repositories: RepositoryBundle) -> None:
    profile = await _seed_profile(repositories, name="runtime-profile")
    term_a = await _seed_term(repositories, name="runtime-term-a")
    term_b = await _seed_term(repositories, name="runtime-term-b")
    binding_a = await repositories.agent_bindings.create(
        profile_id=profile.id, term_id=term_a, status="pending"
    )
    binding_b = await repositories.agent_bindings.create(
        profile_id=profile.id, term_id=term_b, status="pending"
    )

    updated = await repositories.agent_bindings.update_runtime(
        binding_a.id,
        runtime_ref="runtime://opencode",
        runtime_epoch=3,
        capability_ref="mcp://termflow/3",
    )
    assert updated is not None
    assert updated.runtime_ref == "runtime://opencode"
    assert updated.runtime_epoch == 3
    assert updated.capability_ref == "mcp://termflow/3"

    assert [binding.id for binding in await repositories.agent_bindings.list_for_term(term_a)] == [
        binding_a.id
    ]
    assert sorted(
        binding.id for binding in await repositories.agent_bindings.list_for_profile(profile.id)
    ) == sorted([binding_a.id, binding_b.id])
    assert await repositories.agent_bindings.get_by_id(binding_b.id) is not None
    assert await repositories.agent_bindings.get_by_id(uuid4()) is None


@pytest.mark.asyncio
async def test_binding_delete_cascades_to_agent_rows(repositories: RepositoryBundle) -> None:
    profile = await _seed_profile(repositories, name="cascade-profile")
    term_id = await _seed_term(repositories, name="cascade-term")
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id, term_id=term_id, status="pending"
    )
    conversation = await repositories.agent_conversations.create(binding_id=binding.id)
    await repositories.agent_messages.create(
        conversation_id=conversation.id,
        role="user",
        kind="text",
        body_digest=_hash("hello"),
    )
    await repositories.agent_tokens.create(
        binding_id=binding.id,
        token_hash=_hash("raw-token"),
        scopes=("observe",),
        expiry_epoch=99,
        binding_epoch=1,
    )

    assert await repositories.agent_bindings.delete(binding.id) is True

    assert await repositories.agent_bindings.get_by_id(binding.id) is None
    assert await repositories.agent_conversations.get_by_id(conversation.id) is None
    assert await repositories.agent_messages.latest_revision(conversation.id) is None
    assert await repositories.agent_tokens.list_for_binding(binding.id) == []
    # The profile and term themselves survive.
    assert await repositories.agent_profiles.get_by_id(profile.id) is not None
    assert await repositories.instances.get(term_id) is not None
    assert await repositories.agent_bindings.delete(binding.id) is False


@pytest.mark.asyncio
async def test_profile_delete_cascades_through_bindings(repositories: RepositoryBundle) -> None:
    profile = await _seed_profile(repositories, name="profile-cascade")
    term_id = await _seed_term(repositories, name="profile-cascade-term")
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id, term_id=term_id, status="pending"
    )
    conversation = await repositories.agent_conversations.create(binding_id=binding.id)

    assert await repositories.agent_profiles.delete(profile.id) is True

    assert await repositories.agent_profiles.get_by_id(profile.id) is None
    assert await repositories.agent_bindings.get_by_id(binding.id) is None
    assert await repositories.agent_conversations.get_by_id(conversation.id) is None
    # The term survives profile deletion.
    assert await repositories.instances.get(term_id) is not None


# ---------------------------------------------------------------------------
# Memory scopes and conversations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_scope_quota_and_usage(repositories: RepositoryBundle) -> None:
    profile = await _seed_profile(repositories, name="memory-profile")
    term_id = await _seed_term(repositories, name="memory-term")
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id, term_id=term_id, status="pending"
    )

    scope = await repositories.agent_memory_scopes.create(
        binding_id=binding.id,
        term_id=term_id,
        profile_id=profile.id,
        byte_quota=1024,
        count_quota=50,
    )
    assert scope.current_bytes == 0
    assert scope.current_count == 0

    assert await repositories.agent_memory_scopes.get_for_binding(binding.id) is not None

    recorded = await repositories.agent_memory_scopes.record_usage(
        scope.id, delta_bytes=256, delta_count=1
    )
    assert recorded is not None
    assert recorded.current_bytes == 256
    assert recorded.current_count == 1

    re_quota = await repositories.agent_memory_scopes.update_quota(
        scope.id, byte_quota=2048, count_quota=100
    )
    assert re_quota is not None
    assert re_quota.byte_quota == 2048
    assert re_quota.count_quota == 100
    assert re_quota.current_bytes == 256


@pytest.mark.asyncio
async def test_conversation_pagination_rename_and_delete(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    other_binding = await _seed_binding(repositories)
    created = [
        await repositories.agent_conversations.create(
            binding_id=binding.id, title=f"topic-{index}"
        )
        for index in range(4)
    ]
    await repositories.agent_conversations.create(binding_id=other_binding.id, title="other")

    page_one = await repositories.agent_conversations.list_for_binding(
        binding.id, limit=2, offset=0
    )
    page_two = await repositories.agent_conversations.list_for_binding(
        binding.id, limit=2, offset=2
    )
    assert len(page_one) == 2
    assert len(page_two) == 2
    assert {item.id for item in page_one + page_two} == {item.id for item in created}

    renamed = await repositories.agent_conversations.rename(created[0].id, "renamed-topic")
    assert renamed is not None
    assert renamed.title == "renamed-topic"

    assert await repositories.agent_conversations.delete(created[0].id) is True
    assert await repositories.agent_conversations.get_by_id(created[0].id) is None
    assert await repositories.agent_conversations.delete(created[0].id) is False


@pytest.mark.asyncio
async def test_backend_conversation_ref_lifecycle(repositories: RepositoryBundle) -> None:
    conversation = await _seed_conversation(repositories)
    ref = await repositories.agent_backend_conversations.create(
        conversation_id=conversation.id,
        backend_kind="opencode",
        runtime_id="runtime://opencode",
        binding_capability_epoch=2,
        provider_ref="sess_abc123",
    )
    assert ref.provider_ref == "sess_abc123"

    assert await repositories.agent_backend_conversations.get_by_conversation(
        conversation.id
    ) is not None
    assert (
        await repositories.agent_backend_conversations.get_by_provider_ref("sess_abc123")
        is not None
    )
    assert await repositories.agent_backend_conversations.get_by_provider_ref("missing") is None

    assert await repositories.agent_backend_conversations.delete(conversation.id) is True
    assert (
        await repositories.agent_backend_conversations.get_by_conversation(conversation.id)
        is None
    )
    assert await repositories.agent_backend_conversations.delete(conversation.id) is False


# ---------------------------------------------------------------------------
# Runtime bindings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_binding_upsert_readiness_and_health(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    runtime = await repositories.agent_runtime_bindings.upsert(
        binding_id=binding.id,
        runtime_ref="runtime://opencode",
        runtime_epoch=1,
    )
    assert runtime.readiness == "not_ready"

    # Same (runtime_ref, runtime_epoch) upserts into the same row.
    again = await repositories.agent_runtime_bindings.upsert(
        binding_id=binding.id,
        runtime_ref="runtime://opencode",
        runtime_epoch=1,
        readiness="ready",
    )
    assert again.id == runtime.id
    assert again.readiness == "ready"

    assert await repositories.agent_runtime_bindings.get_by_binding(binding.id) is not None

    ready = await repositories.agent_runtime_bindings.set_readiness(runtime.id, "ready")
    assert ready is not None
    assert ready.readiness == "ready"

    observed = datetime.now(UTC)
    healthy = await repositories.agent_runtime_bindings.record_health(runtime.id, now=observed)
    assert healthy is not None
    assert healthy.last_health_at is not None


# ---------------------------------------------------------------------------
# Inbox: admission order, claiming, delivery state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_enqueue_assigns_monotonic_admission_seq_per_conversation(
    repositories: RepositoryBundle,
) -> None:
    conversation_a = await _seed_conversation(repositories)
    conversation_b = await _seed_conversation(repositories)

    first = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_a.id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="a-1",
        payload_digest=_hash("first"),
        source="user",
    )
    second = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_a.id,
        kind="user_message",
        actor_id="actor-2",
        actor_kind="user_session",
        idempotency_key="a-2",
        payload_digest=_hash("second"),
        source="user",
    )
    other = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_b.id,
        kind="user_message",
        actor_id="actor-3",
        actor_kind="user_session",
        idempotency_key="b-1",
        payload_digest=_hash("other"),
        source="user",
    )
    third = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_a.id,
        kind="user_message",
        actor_id="actor-4",
        actor_kind="user_session",
        idempotency_key="a-3",
        payload_digest=_hash("third"),
        source="user",
    )

    assert first.admission_seq == 1
    assert second.admission_seq == 2
    assert third.admission_seq == 3
    assert other.admission_seq == 1

    listed = await repositories.agent_inbox.list_for_conversation(conversation_a.id)
    assert [item.admission_seq for item in listed] == [1, 2, 3]


@pytest.mark.asyncio
async def test_inbox_concurrent_enqueue_assigns_unique_sequences(
    repositories: RepositoryBundle,
) -> None:
    """Concurrent enqueues to one conversation get unique DB-assigned sequences.

    This locks in the documented concurrency guarantee of the atomic
    ``INSERT ... SELECT`` assignment: even with many sessions racing on the
    same conversation, the sequence stays a strict ``1..N`` permutation.
    """
    conversation = await _seed_conversation(repositories)
    count = 8

    async def enqueue(index: int) -> int:
        item = await repositories.agent_inbox.enqueue(
            conversation_id=conversation.id,
            kind="user_message",
            actor_id=f"actor-{index}",
            actor_kind="user_session",
            idempotency_key=f"concurrent-{index}",
            payload_digest=_hash(f"payload-{index}"),
            source="user",
        )
        return item.admission_seq

    sequences = await asyncio.gather(*(enqueue(index) for index in range(count)))
    assert sorted(sequences) == list(range(1, count + 1))
    assert len(set(sequences)) == count


@pytest.mark.asyncio
async def test_inbox_idempotency_key_lookup_and_duplicate_rejection(
    repositories: RepositoryBundle,
) -> None:
    conversation = await _seed_conversation(repositories)
    item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation.id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="stable-key",
        payload_digest=_hash("payload"),
        source="user",
    )
    assert await repositories.agent_inbox.get_by_idempotency_key("stable-key") is not None
    assert (await repositories.agent_inbox.get_by_idempotency_key("stable-key")).id == item.id
    assert await repositories.agent_inbox.get_by_idempotency_key("missing") is None

    with pytest.raises(IntegrityError):
        await repositories.agent_inbox.enqueue(
            conversation_id=conversation.id,
            kind="user_message",
            actor_id="actor-2",
            actor_kind="user_session",
            idempotency_key="stable-key",
            payload_digest=_hash("payload"),
            source="user",
        )


@pytest.mark.asyncio
async def test_inbox_next_pending_filters_by_state_and_attempt_time(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    conversation = await _seed_conversation(repositories)
    now_item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation.id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="now-item",
        payload_digest=_hash("now"),
        source="user",
        now=observed,
    )
    later_item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation.id,
        kind="user_message",
        actor_id="actor-2",
        actor_kind="user_session",
        idempotency_key="later-item",
        payload_digest=_hash("later"),
        source="user",
        now=observed,
    )

    pending = await repositories.agent_inbox.next_pending(
        conversation_id=conversation.id, now=observed
    )
    assert [item.id for item in pending] == [now_item.id, later_item.id]

    # A claimed item is no longer eligible...
    await repositories.agent_inbox.claim(now_item.id, "owner-1", lease_seconds=60, now=observed)
    pending = await repositories.agent_inbox.next_pending(
        conversation_id=conversation.id, now=observed
    )
    assert [item.id for item in pending] == [later_item.id]

    # ...and a retry_wait item is eligible only after its next_attempt_at.
    await repositories.agent_inbox.mark_retry(
        later_item.id,
        next_attempt_at=observed + timedelta(minutes=5),
    )
    assert await repositories.agent_inbox.next_pending(
        conversation_id=conversation.id, now=observed
    ) == []
    # By +6 minutes the retry deadline has passed and the earlier claim lease
    # has expired, so both items are claimable again in admission order.
    retried = await repositories.agent_inbox.next_pending(
        conversation_id=conversation.id, now=observed + timedelta(minutes=6)
    )
    assert [item.id for item in retried] == [now_item.id, later_item.id]
    assert retried[0].attempt_count == 0
    assert retried[1].attempt_count == 1


@pytest.mark.asyncio
async def test_inbox_claim_is_cas_and_expired_leases_are_reclaimable(
    repositories: RepositoryBundle,
) -> None:
    observed = datetime.now(UTC)
    conversation = await _seed_conversation(repositories)
    item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation.id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="claim-item",
        payload_digest=_hash("claim"),
        source="user",
        now=observed,
    )

    claimed = await repositories.agent_inbox.claim(
        item.id, "worker-1", lease_seconds=60, now=observed
    )
    assert claimed is not None
    assert claimed.delivery_state == "claimed"
    assert claimed.claim_owner == "worker-1"

    # A second, concurrent claim on the same live lease is rejected.
    assert (
        await repositories.agent_inbox.claim(
            item.id, "worker-2", lease_seconds=60, now=observed
        )
        is None
    )

    # Once the lease expires the item is reclaimable by a new owner.
    reclaimed = await repositories.agent_inbox.claim(
        item.id,
        "worker-3",
        lease_seconds=60,
        now=observed + timedelta(seconds=61),
    )
    assert reclaimed is not None
    assert reclaimed.claim_owner == "worker-3"


@pytest.mark.asyncio
async def test_inbox_delivery_transitions(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    conversation = await _seed_conversation(repositories)
    item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation.id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="dispatch-item",
        payload_digest=_hash("dispatch"),
        source="user",
        now=observed,
    )
    await repositories.agent_inbox.claim(item.id, "worker-1", lease_seconds=60, now=observed)

    # Fencing: only the lease holder can mark the item dispatched.
    assert (
        await repositories.agent_inbox.mark_dispatched(item.id, "other-worker", now=observed)
        is None
    )
    dispatched = await repositories.agent_inbox.mark_dispatched(item.id, "worker-1", now=observed)
    assert dispatched is not None
    assert dispatched.delivery_state == "dispatched"


@pytest.mark.asyncio
async def test_inbox_mark_retry_and_dead_letter(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    conversation = await _seed_conversation(repositories)
    retry_item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation.id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="retry-item",
        payload_digest=_hash("retry"),
        source="user",
        now=observed,
    )
    dead_item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation.id,
        kind="user_message",
        actor_id="actor-2",
        actor_kind="user_session",
        idempotency_key="dead-item",
        payload_digest=_hash("dead"),
        source="user",
        now=observed,
    )
    await repositories.agent_inbox.claim(retry_item.id, "worker-1", lease_seconds=60, now=observed)
    await repositories.agent_inbox.claim(dead_item.id, "worker-2", lease_seconds=60, now=observed)

    retried = await repositories.agent_inbox.mark_retry(
        retry_item.id,
        next_attempt_at=observed + timedelta(minutes=2),
    )
    assert retried is not None
    assert retried.delivery_state == "retry_wait"
    assert retried.attempt_count == 1

    dead = await repositories.agent_inbox.dead_letter(dead_item.id)
    assert dead is not None
    assert dead.delivery_state == "dead_letter"


@pytest.mark.asyncio
async def test_inbox_mark_cancelled(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    conversation = await _seed_conversation(repositories)
    item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation.id,
        kind="user_message",
        actor_id="actor-1",
        actor_kind="user_session",
        idempotency_key="cancel-item",
        payload_digest=_hash("cancel"),
        source="user",
        now=observed,
    )
    cancelled = await repositories.agent_inbox.mark_cancelled(item.id)
    assert cancelled is not None
    assert cancelled.delivery_state == "cancelled"
    assert await repositories.agent_inbox.next_pending(
        conversation_id=conversation.id, now=observed
    ) == []


# ---------------------------------------------------------------------------
# Tool requests, runs, messages, events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_request_key_dedup_and_conflict(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    request = await repositories.agent_tool_requests.create(
        binding_id=binding.id,
        tool_call_id="call-1",
        request_key="side-effect-key",
        arguments_digest=_hash('{"command": "ls"}'),
    )
    assert request.request_key == "side-effect-key"

    # Same key with the same digest returns the recorded row.
    await repositories.agent_tool_requests.record_result(
        "side-effect-key", result_digest=_hash('{"exit": 0}')
    )
    deduped = await repositories.agent_tool_requests.reject_duplicate_key_different_args(
        "side-effect-key", _hash('{"command": "ls"}')
    )
    assert deduped is not None
    assert deduped.id == request.id
    assert deduped.recorded_result_digest is not None
    assert await repositories.agent_tool_requests.get_by_key("side-effect-key") is not None

    # Same key with a different digest is rejected.
    with pytest.raises(AgentToolRequestKeyConflict):
        await repositories.agent_tool_requests.reject_duplicate_key_different_args(
            "side-effect-key", _hash('{"command": "rm -rf /"}')
        )


@pytest.mark.asyncio
async def test_run_state_transitions_and_conversation_listing(
    repositories: RepositoryBundle,
) -> None:
    conversation = await _seed_conversation(repositories)
    other = await _seed_conversation(repositories)

    run = await _seed_run(repositories, conversation.id)
    assert run.run_state == "queued"
    await repositories.agent_runs.create(conversation_id=other.id)

    running = await repositories.agent_runs.set_state(
        run.id, "running", expected_state="queued"
    )
    assert running is not None
    assert running.run_state == "running"

    completed = await repositories.agent_runs.set_state(
        run.id, "completed", expected_state="running", error_code=None
    )
    assert completed is not None
    assert completed.run_state == "completed"
    assert completed.completed_at is not None

    # CAS rejects the same transition applied twice.
    assert (
        await repositories.agent_runs.set_state(
            run.id, "running", expected_state="queued"
        )
        is None
    )

    with_backend = await repositories.agent_runs.set_backend_run_id(run.id, "backend-run-1")
    assert with_backend is not None
    assert with_backend.backend_run_id == "backend-run-1"

    listed = await repositories.agent_runs.list_for_conversation(conversation.id)
    assert [item.id for item in listed] == [run.id]
    assert await repositories.agent_runs.get_by_id(run.id) is not None
    assert await repositories.agent_runs.get_by_id(uuid4()) is None


@pytest.mark.asyncio
async def test_message_revisions_and_pagination(repositories: RepositoryBundle) -> None:
    conversation = await _seed_conversation(repositories)
    first = await repositories.agent_messages.create(
        conversation_id=conversation.id,
        role="user",
        kind="text",
        body_digest=_hash("one"),
    )
    second = await repositories.agent_messages.create(
        conversation_id=conversation.id,
        role="assistant",
        kind="text",
        body_digest=_hash("two"),
        is_final=True,
    )
    assert first.assembly_revision == 1
    assert second.assembly_revision == 2

    assert await repositories.agent_messages.latest_revision(conversation.id) == 2

    listed = await repositories.agent_messages.list_for_conversation(conversation.id)
    assert [item.assembly_revision for item in listed] == [1, 2]

    page = await repositories.agent_messages.list_for_conversation(
        conversation.id, limit=1, offset=1
    )
    assert [item.assembly_revision for item in page] == [2]

    # An explicit revision is respected and stays unique per conversation.
    explicit = await repositories.agent_messages.create(
        conversation_id=conversation.id,
        role="user",
        kind="text",
        body_digest=_hash("explicit"),
        assembly_revision=4,
    )
    assert explicit.assembly_revision == 4
    assert await repositories.agent_messages.latest_revision(conversation.id) == 4


@pytest.mark.asyncio
async def test_message_explicit_revision_on_empty_conversation(
    repositories: RepositoryBundle,
) -> None:
    """A caller-supplied assembly_revision inserts on a fresh conversation.

    Regression: the explicit-revision branch previously kept the conversation
    ``WHERE`` clause, which made the subquery return zero rows for an empty
    conversation and crash with ``NoResultFound``.
    """
    conversation = await _seed_conversation(repositories)
    message = await repositories.agent_messages.create(
        conversation_id=conversation.id,
        role="user",
        kind="text",
        body_digest=_hash("explicit-first"),
        assembly_revision=7,
    )
    assert message.assembly_revision == 7
    assert await repositories.agent_messages.latest_revision(conversation.id) == 7
    listed = await repositories.agent_messages.list_for_conversation(conversation.id)
    assert [item.assembly_revision for item in listed] == [7]


@pytest.mark.asyncio
async def test_event_append_is_monotonic_and_dedupes(repositories: RepositoryBundle) -> None:
    conversation = await _seed_conversation(repositories)
    other = await _seed_conversation(repositories)

    first = await repositories.agent_events.append(
        conversation_id=conversation.id,
        event_kind="run_started",
        dedup_key="evt-1",
        payload_digest=_hash("start"),
    )
    second = await repositories.agent_events.append(
        conversation_id=conversation.id,
        event_kind="message_completed",
        dedup_key="evt-2",
        payload_digest=_hash("message"),
    )
    assert first.database_seq == 1
    assert second.database_seq == 2

    other_event = await repositories.agent_events.append(
        conversation_id=other.id,
        event_kind="run_started",
        dedup_key="other-1",
        payload_digest=_hash("other"),
    )
    assert other_event.database_seq == 1

    # Re-appending the same dedup_key returns the original event (dedup).
    duped = await repositories.agent_events.append(
        conversation_id=conversation.id,
        event_kind="run_started",
        dedup_key="evt-1",
        payload_digest=_hash("start"),
    )
    assert duped.id == first.id
    assert duped.database_seq == 1

    # The sequence cursor is monotonic and ordered per conversation.
    third = await repositories.agent_events.append(
        conversation_id=conversation.id,
        event_kind="run_completed",
        dedup_key="evt-3",
        payload_digest=_hash("done"),
    )
    assert third.database_seq == 3

    since = await repositories.agent_events.list_since_cursor(conversation.id, 1)
    assert [event.database_seq for event in since] == [2, 3]

    full = await repositories.agent_events.list_for_conversation(conversation.id)
    assert [event.database_seq for event in full] == [1, 2, 3]


@pytest.mark.asyncio
async def test_event_concurrent_append_same_dedup_key_inserts_once(
    repositories: RepositoryBundle,
) -> None:
    """Concurrent appends of the same dedup_key produce exactly one event.

    Regression: dedup used to be check-then-act with no DB backstop, so racing
    appends of one dedup_key could insert duplicate rows.  The atomic
    ``INSERT ... SELECT ... WHERE NOT EXISTS`` guard makes the dedup part of
    the same single statement that assigns the sequence.
    """
    conversation = await _seed_conversation(repositories)

    async def append() -> AgentEvent:
        return await repositories.agent_events.append(
            conversation_id=conversation.id,
            event_kind="run_started",
            dedup_key="concurrent-dup",
            payload_digest=_hash("start"),
        )

    events = await asyncio.gather(*(append() for _ in range(4)))
    assert len({event.id for event in events}) == 1
    assert len(await repositories.agent_events.list_for_conversation(conversation.id)) == 1

    # A distinct dedup_key still advances the sequence monotonically.
    later = await repositories.agent_events.append(
        conversation_id=conversation.id,
        event_kind="run_completed",
        dedup_key="after-race",
        payload_digest=_hash("done"),
    )
    assert later.database_seq == 2


# ---------------------------------------------------------------------------
# Tokens, pane policies, approvals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_stores_hash_only_and_revokes(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    raw_token = "raw-capability-token"
    token_hash = _hash(raw_token)
    token = await repositories.agent_tokens.create(
        binding_id=binding.id,
        token_hash=token_hash,
        scopes=("observe",),
        expiry_epoch=100,
        binding_epoch=1,
    )
    assert token.token_hash == token_hash
    # The raw token is never persisted.
    assert token.token_hash != raw_token
    persisted = (await repositories.agent_tokens.list_for_binding(binding.id))[0]
    assert raw_token not in persisted.token_hash

    assert await repositories.agent_tokens.get_by_hash(token_hash) is not None
    assert await repositories.agent_tokens.get_by_hash(_hash("other")) is None

    assert await repositories.agent_tokens.revoke(token_hash) is True
    # Fail closed: a revoked token is excluded from lookups...
    assert await repositories.agent_tokens.get_by_hash(token_hash) is None
    assert await repositories.agent_tokens.revoke(token_hash) is False
    # ...while the revocation stays visible in the binding's token history.
    revoked = (await repositories.agent_tokens.list_for_binding(binding.id))[0]
    assert revoked.revoked_at is not None


@pytest.mark.asyncio
async def test_expire_all_for_binding_revokes_binding_tokens(
    repositories: RepositoryBundle,
) -> None:
    binding = await _seed_binding(repositories)
    other = await _seed_binding(repositories)
    for index in range(3):
        await repositories.agent_tokens.create(
            binding_id=binding.id,
            token_hash=_hash(f"token-{index}"),
            scopes=("observe",),
            expiry_epoch=100 + index,
            binding_epoch=1,
        )
    await repositories.agent_tokens.create(
        binding_id=other.id,
        token_hash=_hash("other-token"),
        scopes=("observe",),
        expiry_epoch=100,
        binding_epoch=1,
    )

    expired = await repositories.agent_tokens.expire_all_for_binding(binding.id)
    assert expired == 3
    binding_tokens = await repositories.agent_tokens.list_for_binding(binding.id)
    assert all(token.revoked_at is not None for token in binding_tokens)
    other_tokens = await repositories.agent_tokens.list_for_binding(other.id)
    assert len(other_tokens) == 1 and other_tokens[0].revoked_at is None


@pytest.mark.asyncio
async def test_pane_policy_upsert_and_lookup(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    policy = await repositories.pane_policies.set_policy(
        binding_id=binding.id, pane_id="term-a:0", allowed=True
    )
    assert policy.allowed is True
    assert await repositories.pane_policies.pane_allowed(binding.id, "term-a:0") is True
    assert await repositories.pane_policies.pane_allowed(binding.id, "term-a:1") is None

    flipped = await repositories.pane_policies.set_policy(
        binding_id=binding.id, pane_id="term-a:0", allowed=False
    )
    assert flipped.id == policy.id
    assert flipped.allowed is False
    assert await repositories.pane_policies.pane_allowed(binding.id, "term-a:0") is False

    policies = await repositories.pane_policies.get_for_binding(binding.id)
    assert [item.pane_id for item in policies] == ["term-a:0"]


@pytest.mark.asyncio
async def test_approval_cas_transition_and_lookup(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories)
    observed = datetime.now(UTC)
    approval = await repositories.approvals.create(
        binding_id=binding.id,
        conversation_id=conversation.id,
        tool_call_id="tool-call-1",
        canonical_hash=_hash('{"text": "ls"}'),
        auth_epoch=1,
        expires_at=observed + timedelta(minutes=2),
    )
    assert approval.state == "pending"

    assert (
        await repositories.approvals.get_by_tool_call(conversation.id, "tool-call-1")
    ).id == approval.id
    assert await repositories.approvals.get_by_id(approval.id) is not None
    assert await repositories.approvals.get_by_tool_call(conversation.id, "missing") is None

    approved = await repositories.approvals.set_state(
        approval.id, "approved", expected_state="pending", decision="approved", now=observed
    )
    assert approved is not None
    assert approved.state == "approved"
    assert approved.decided_at is not None

    # CAS: a double decision on the already-decided approval is rejected.
    assert (
        await repositories.approvals.set_state(
            approval.id, "denied", expected_state="pending", decision="denied", now=observed
        )
        is None
    )


@pytest.mark.asyncio
async def test_approval_expire_pending(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    observed = datetime.now(UTC)
    for index in range(2):
        conversation = await _seed_conversation(repositories)
        await repositories.approvals.create(
            binding_id=binding.id,
            conversation_id=conversation.id,
            tool_call_id=f"call-{index}",
            canonical_hash=_hash(f"payload-{index}"),
            auth_epoch=1,
            expires_at=observed - timedelta(seconds=1),
        )
    expired = await repositories.approvals.expire_pending(now=observed)
    assert expired == 2


# ---------------------------------------------------------------------------
# Watches, transcript drafts, cleanup jobs, diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_lifecycle_and_list_active(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories)
    observed = datetime.now(UTC)
    watch = await repositories.watches.create(
        binding_id=binding.id,
        conversation_id=conversation.id,
        pane_id="term-a:0",
        condition_kind="output_contains",
        start_cursor="stream:1:42",
        intent_summary="wait for build to finish",
        expiry_at=observed + timedelta(hours=1),
    )
    assert watch.watch_generation == 0

    advanced = await repositories.watches.advance_generation(
        watch.id, rearm_cursor="stream:1:99"
    )
    assert advanced is not None
    assert advanced.watch_generation == 1
    assert advanced.rearm_cursor == "stream:1:99"

    active = await repositories.watches.list_active(now=observed)
    assert [item.id for item in active] == [watch.id]
    assert await repositories.watches.get_by_id(watch.id) is not None
    assert [item.id for item in await repositories.watches.list_for_binding(binding.id)] == [
        watch.id
    ]

    cancelled = await repositories.watches.cancel(watch.id)
    assert cancelled is not None
    assert cancelled.state == "cancelled"
    assert await repositories.watches.list_active(now=observed) == []


@pytest.mark.asyncio
async def test_watch_delivery_unique_key_and_attempts(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories)
    watch = await repositories.watches.create(
        binding_id=binding.id,
        conversation_id=conversation.id,
        pane_id="term-a:0",
        condition_kind="output_contains",
        start_cursor="stream:1:42",
        intent_summary="continue",
    )
    delivery = await repositories.agent_watch_deliveries.create_unique(
        watch_id=watch.id, delivery_key="watch-1:gen-1:stream-1-50"
    )
    assert delivery.attempt_count == 0

    duped = await repositories.agent_watch_deliveries.create_unique(
        watch_id=watch.id, delivery_key="watch-1:gen-1:stream-1-50"
    )
    assert duped.id == delivery.id

    assert await repositories.agent_watch_deliveries.get_by_key(
        "watch-1:gen-1:stream-1-50"
    ) is not None

    recorded = await repositories.agent_watch_deliveries.record_attempt(
        delivery.id,
        last_error="timeout",
        next_attempt_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert recorded is not None
    assert recorded.attempt_count == 1
    assert recorded.last_error == "timeout"


@pytest.mark.asyncio
async def test_transcript_draft_state_machine(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories)
    observed = datetime.now(UTC)
    draft = await repositories.transcript_drafts.create(
        binding_id=binding.id,
        target_conversation_id=conversation.id,
        owner_actor_id="actor-1",
        transcript_hash=_hash("transcript"),
        provider="whisper",
        region="us-east-1",
        expires_at=observed + timedelta(minutes=5),
    )
    assert draft.state == "pending"

    confirmed = await repositories.transcript_drafts.set_state(
        draft.id, "confirmed", expected_state="pending"
    )
    assert confirmed is not None
    assert confirmed.state == "confirmed"

    assert (
        await repositories.transcript_drafts.set_state(
            draft.id, "confirmed", expected_state="pending"
        )
        is None
    )
    assert await repositories.transcript_drafts.get_by_id(draft.id) is not None

    assert await repositories.transcript_drafts.delete(draft.id) is True
    assert await repositories.transcript_drafts.get_by_id(draft.id) is None
    assert await repositories.transcript_drafts.delete(draft.id) is False


@pytest.mark.asyncio
async def test_transcript_draft_expire_pending(repositories: RepositoryBundle) -> None:
    binding = await _seed_binding(repositories)
    observed = datetime.now(UTC)
    conversation = await _seed_conversation(repositories)
    await repositories.transcript_drafts.create(
        binding_id=binding.id,
        target_conversation_id=conversation.id,
        owner_actor_id="actor-1",
        transcript_hash=_hash("expired"),
        provider="whisper",
        region="us-east-1",
        expires_at=observed - timedelta(seconds=1),
    )
    assert await repositories.transcript_drafts.expire_pending(now=observed) == 1


@pytest.mark.asyncio
async def test_cleanup_job_lifecycle(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    term_id = await _seed_term(repositories, name="cleanup-term")
    job = await repositories.cleanup_jobs.create(
        target_kind="backend_conversation",
        target_ref="sess_abc123",
        term_id=term_id,
    )
    assert job.state == "pending"
    assert job.attempt_count == 0

    pending = await repositories.cleanup_jobs.list_pending(now=observed)
    assert [item.id for item in pending] == [job.id]

    recorded = await repositories.cleanup_jobs.record_attempt(
        job.id,
        next_attempt_at=observed + timedelta(minutes=5),
        last_error="backend unreachable",
    )
    assert recorded is not None
    assert recorded.attempt_count == 1
    assert recorded.last_error == "backend unreachable"

    # With a future retry time the job is no longer pending now.
    assert await repositories.cleanup_jobs.list_pending(now=observed) == []
    assert [
        item.id
        for item in await repositories.cleanup_jobs.list_pending(
            now=observed + timedelta(minutes=6)
        )
    ] == [job.id]

    completed = await repositories.cleanup_jobs.complete(job.id, now=observed)
    assert completed is not None
    assert completed.state == "completed"
    assert await repositories.cleanup_jobs.list_pending(now=observed) == []


@pytest.mark.asyncio
async def test_cleanup_job_dead_letter(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    job = await repositories.cleanup_jobs.create(
        target_kind="volume",
        target_ref="/data/term-a",
    )
    dead = await repositories.cleanup_jobs.dead_letter(
        job.id, last_error="permission denied", now=observed
    )
    assert dead is not None
    assert dead.state == "dead_letter"
    assert dead.last_error == "permission denied"


@pytest.mark.asyncio
async def test_diagnostics_append_and_prune(repositories: RepositoryBundle) -> None:
    observed = datetime.now(UTC)
    conversation = await _seed_conversation(repositories)
    kept = await repositories.diagnostics.append(
        conversation_id=conversation.id,
        event_kind="backend_state_changed",
        correlation_hash=_hash("corr-1"),
        ttl_expires_at=observed + timedelta(hours=1),
        error_code=None,
    )
    expired = await repositories.diagnostics.append(
        conversation_id=conversation.id,
        event_kind="backend_state_changed",
        correlation_hash=_hash("corr-2"),
        ttl_expires_at=observed - timedelta(seconds=1),
    )
    assert kept.ttl_expires_at > observed
    assert expired.ttl_expires_at < observed

    pruned = await repositories.diagnostics.prune_expired(now=observed)
    assert pruned == 1

    async with repositories.session_factory() as session:  # type: ignore[attr-defined]
        remaining = list(
            (
                await session.scalars(
                    select(AgentDiagnostic).where(
                        AgentDiagnostic.conversation_id == conversation.id
                    )
                )
            ).all()
        )
    assert [item.id for item in remaining] == [kept.id]
