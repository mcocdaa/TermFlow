"""AgentPipelineService tests (M4.5 spec §1-§4: dispatcher + SSE consumer).

All tests run against a fake in-memory backend (no real OpenCode container;
the real container round trip is an M4 exit integration item).  The fake
models the pinned contract semantics: 204 → ``confirmed`` admission, a
queue-driven ``events()`` stream that ends cleanly on disconnect, and
call/order recording so the dispatcher's fail-closed sequencing is asserted
deterministically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackendCapabilities,
    CancelScope,
    ConcurrencyMode,
    ContextMode,
    ReplayMode,
    RuntimeIsolation,
    SubmitMode,
    ToolCallIdentity,
)
from termflow_control_plane.plugins.agent_broker.agent.pipeline import (
    AgentPipelineService,
    SubmitAdmission,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import AgentStreamHub
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendConversationRef,
    BackendConversationSnapshot,
    BackendEventScope,
    BackendNotification,
    BackendOperationResult,
    BackendOutcome,
    BackendSubmitResult,
    BackendTurnRequest,
    CreateBackendConversation,
    NotificationPayload,
    TurnPartTrust,
)
from termflow_control_plane.plugins.protocol import RuntimeRef
from termflow_protocol.agent import (
    AgentActorKind,
    AgentEventKind,
    AgentInputKind,
    AgentInputSource,
    WatchTriggeredInput,
    WatchTriggeredPayload,
)

RUNTIME_ID = "runtime-1"

# ---------------------------------------------------------------------------
# Fixtures and doubles.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    # Test seam: let tests open ad-hoc sessions against the same database.
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


@pytest.fixture
def hub() -> AgentStreamHub:
    return AgentStreamHub()


@dataclass
class SubmitCall:
    ref: BackendConversationRef
    request: BackendTurnRequest
    delivery_state_at_submit: str | None


class FakeBackend:
    """In-memory ``AgentBackend`` double with deterministic call recording.

    ``events(scope)`` drains a per-instance queue; a ``None`` sentinel ends
    the stream cleanly (SSE disconnect semantics), after which the consumer's
    reconnect loop re-subscribes through a fresh ``events`` call.
    """

    def __init__(
        self,
        *,
        repositories: RepositoryBundle | None = None,
        runtime_id: str = RUNTIME_ID,
        epoch: int = 1,
    ) -> None:
        self.runtime_id = runtime_id
        self.epoch = epoch
        self.directory = "/workspace"
        self._repositories = repositories
        self.created_sessions = 0
        self.create_calls: list[CreateBackendConversation] = []
        self.submit_calls: list[SubmitCall] = []
        self.submit_result: BackendSubmitResult = BackendSubmitResult(
            outcome=BackendOutcome.CONFIRMED
        )
        self.submit_exception: Exception | None = None
        self.events_calls = 0
        self._event_queue: asyncio.Queue[BackendNotification | None] = asyncio.Queue()
        self.closed = False

    async def capabilities(self) -> AgentBackendCapabilities:
        return backend_capabilities()

    async def create_conversation(
        self, request: CreateBackendConversation
    ) -> BackendConversationRef:
        self.created_sessions += 1
        self.create_calls.append(request)
        return BackendConversationRef(
            backend_kind="fake",
            backend_version="0.1.0",
            runtime_id=self.runtime_id,
            binding_capability_epoch=self.epoch,
            provider_ref=f"provider-{self.created_sessions}",
        )

    async def submit(
        self, ref: BackendConversationRef, request: BackendTurnRequest
    ) -> BackendSubmitResult:
        # Record the item's durable delivery state at submit time so tests can
        # assert mark_started (claimed→dispatched) happened before the write.
        delivery_state: str | None = None
        if self._repositories is not None:
            item = await self._repositories.agent_inbox.get_by_idempotency_key(
                request.idempotency_key
            )
            delivery_state = item.delivery_state if item is not None else None
        self.submit_calls.append(
            SubmitCall(
                ref=ref,
                request=request,
                delivery_state_at_submit=delivery_state,
            )
        )
        if self.submit_exception is not None:
            raise self.submit_exception
        return self.submit_result

    async def events(self, scope: BackendEventScope):
        self.events_calls += 1
        while True:
            item = await self._event_queue.get()
            if item is None:
                return  # clean disconnect: the consumer reconciles + reconnects
            yield item

    def push_event(self, notification: BackendNotification) -> None:
        self._event_queue.put_nowait(notification)

    def disconnect(self) -> None:
        self._event_queue.put_nowait(None)

    async def close(self) -> None:
        self.closed = True

    async def reconcile(
        self, ref: BackendConversationRef
    ) -> BackendConversationSnapshot:
        return BackendConversationSnapshot(
            conversation_ref=ref, outcome=BackendOutcome.CONFIRMED, resumable=False
        )

    async def cancel(self, request) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOutcome.CONFIRMED)

    async def interact(self, request) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOutcome.CONFIRMED)

    async def delete_conversation(self, ref) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOutcome.CONFIRMED)


class FakeSupervisor:
    """Supervisor double whose activation gate is scripted per test."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[tuple[RuntimeRef, int]] = []

    def accept_activation(self, runtime_ref: RuntimeRef, epoch: int) -> bool:
        self.calls.append((runtime_ref, epoch))
        return self.accept


def backend_capabilities() -> AgentBackendCapabilities:
    return AgentBackendCapabilities(
        context_mode=ContextMode.LOST_ON_RESTART,
        submit_mode=SubmitMode.NON_IDEMPOTENT,
        cancel_scope=CancelScope.CONVERSATION,
        replay_mode=ReplayMode.NONE,
        concurrency_mode=ConcurrencyMode.SERIALIZED,
        tool_call_identity=ToolCallIdentity.BINDING,
        runtime_isolation=RuntimeIsolation.BINDING,
        explicit_run_boundaries=True,
    )


def make_pipeline(
    repositories: RepositoryBundle,
    hub: AgentStreamHub,
    backend: FakeBackend,
    *,
    binding_id: UUID | None = None,
    supervisor: FakeSupervisor | None = None,
    **overrides,
) -> AgentPipelineService:
    binding_id = binding_id or uuid4()
    scope = BackendEventScope(binding_id=str(binding_id), runtime_epoch=1)
    kwargs = {
        "payload_grace_attempts": 50,
        "payload_grace_delay": 0.002,
        "dispatch_poll_seconds": 0.001,
        "active_run_retry_seconds": 0.01,
        "reconnect_backoff_base": 0.001,
        "reconnect_backoff_cap": 0.005,
    }
    kwargs.update(overrides)
    return AgentPipelineService(
        binding_id=binding_id,
        adapter=backend,
        scope=scope,
        capabilities=backend_capabilities(),
        repositories=repositories,
        sessions=repositories.session_factory,
        hub=hub,
        supervisor=supervisor,
        runtime_ref=backend.runtime_id,
        runtime_epoch=backend.epoch,
        **kwargs,
    )


async def seed_binding(repositories: RepositoryBundle) -> UUID:
    profile = await repositories.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    installation = await repositories.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    display_name = f"term-{uuid4().hex[:8]}"
    term = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="pending",
    )
    return binding.id


async def seed_conversation(
    repositories: RepositoryBundle,
    *,
    binding_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    """Create binding + conversation rows; returns (binding_id, conversation_id)."""
    binding_id = binding_id or await seed_binding(repositories)
    conversation = await repositories.agent_conversations.create(binding_id=binding_id)
    return binding_id, conversation.id


def make_notification(
    backend: FakeBackend,
    pipeline: AgentPipelineService,
    *,
    kind: AgentEventKind,
    provider_ref: str,
    text: str | None = None,
    error_code: str | None = None,
    dedup_key: str | None = None,
    run_id: UUID | None = None,
    message_id: UUID | None = None,
    binding_id: str | None = None,
    runtime_id: str | None = None,
) -> BackendNotification:
    return BackendNotification(
        conversation_ref=BackendConversationRef(
            backend_kind="fake",
            backend_version="0.1.0",
            runtime_id=runtime_id or backend.runtime_id,
            binding_capability_epoch=backend.epoch,
            provider_ref=provider_ref,
        ),
        scope=BackendEventScope(
            binding_id=binding_id or str(pipeline.binding_id),
            runtime_epoch=1,
        ),
        kind=kind,
        run_id=run_id or uuid4(),
        message_id=message_id or uuid4(),
        dedup_key=dedup_key or f"dedup-{uuid4()}",
        payload=NotificationPayload(text=text, error_code=error_code),
    )


async def wait_until(
    predicate: Callable[[], bool | Awaitable[bool]],
    *,
    deadline_seconds: float = 5.0,
    interval: float = 0.002,
    message: str = "condition not reached within timeout",
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
    pytest.fail(message)


async def _runs(repositories: RepositoryBundle, conversation_id: UUID):
    return await repositories.agent_runs.list_for_conversation(conversation_id)


async def _messages(repositories: RepositoryBundle, conversation_id: UUID):
    return await repositories.agent_messages.list_for_conversation(conversation_id)


async def _events(repositories: RepositoryBundle, conversation_id: UUID):
    return await repositories.agent_events.list_for_conversation(conversation_id)


# ---------------------------------------------------------------------------
# Dispatcher: user message admission → turn submission.
# ---------------------------------------------------------------------------


async def test_submit_user_message_admits_enqueues_and_dispatches(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        admission = await pipeline.submit_user_message(
            conversation_id, "please fix the build", actor="admin"
        )

        assert isinstance(admission, SubmitAdmission)
        assert admission.conversation_id == conversation_id
        assert admission.admission_seq == 1
        assert admission.delivery_state == "pending"
        assert admission.submission_state == "not_started"
        # The enqueue is durable with the text digest (no payload column).
        row = await repositories.agent_inbox.get_by_idempotency_key(
            admission.idempotency_key
        )
        assert row is not None
        assert row.kind == "user_message"
        assert row.source == "user"
        assert row.payload_digest == hashlib.sha256(
            b"please fix the build"
        ).hexdigest()

        await wait_until(lambda: len(backend.submit_calls) == 1)

        # First turn creates the backend conversation exactly once.
        assert backend.created_sessions == 1
        ref_row = await repositories.agent_backend_conversations.get_by_conversation(
            conversation_id
        )
        assert ref_row is not None
        assert ref_row.provider_ref == "provider-1"

        call = backend.submit_calls[0]
        # mark_started (claimed→dispatched) committed BEFORE the submit write.
        assert call.delivery_state_at_submit == "dispatched"
        part = call.request.parts[0]
        assert part.kind is AgentInputKind.USER_MESSAGE
        assert part.text == "please fix the build"
        assert part.trust is TurnPartTrust.TRUSTED
        assert call.request.idempotency_key == admission.idempotency_key
        assert call.request.conversation_ref.provider_ref == "provider-1"
        assert str(call.request.correlation_id) == str(row.correlation_id)

        # 204 → confirmed: the run stays queued until the SSE RUN_STARTED.
        runs = await _runs(repositories, conversation_id)
        assert len(runs) == 1
        assert runs[0].run_state == "queued"

        # The SSE boundary events advance the run to completed.
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_STARTED,
                provider_ref="provider-1",
            )
        )
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_COMPLETED,
                provider_ref="provider-1",
            )
        )

        async def run_completed() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(run.run_state == "completed" for run in runs)

        await wait_until(run_completed)
    finally:
        await pipeline.stop()


async def test_backend_conversation_ref_is_reused(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    # NOTE: the public path cannot reach a second submission for the same
    # conversation in this milestone (the M1.4 inbox schema keeps a
    # dispatched item in-flight forever), so the reuse branch is exercised
    # directly.
    first = await pipeline._resolve_backend_ref(conversation_id)
    second = await pipeline._resolve_backend_ref(conversation_id)
    # Neutral refs are frozen value models: equality compares their content.
    assert first == second
    assert first.provider_ref == "provider-1"
    assert backend.created_sessions == 1
    await pipeline.stop()


# ---------------------------------------------------------------------------
# Watch continuation.
# ---------------------------------------------------------------------------


async def test_watch_input_dispatches_untrusted_watch_triggered_part(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)

    watch_id = uuid4()
    delivery_key = str(uuid4())
    continuation = (
        '{"watch_id": "...", "intent_summary": "verify deploy", "wake_behavior": "inspect"}'
    )
    item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_id,
        kind=AgentInputKind.WATCH_TRIGGERED.value,
        actor_id=str(watch_id),
        actor_kind=AgentActorKind.WATCH_ENGINE.value,
        idempotency_key=delivery_key,
        payload_digest=hashlib.sha256(continuation.encode("utf-8")).hexdigest(),
        source=AgentInputSource.SYSTEM.value,
    )
    watch_input = WatchTriggeredInput(
        kind="watch_triggered",
        conversation_id=conversation_id,
        actor_id=str(watch_id),
        actor_kind=AgentActorKind.WATCH_ENGINE,
        admission_seq=item.admission_seq,
        idempotency_key=delivery_key,
        source=AgentInputSource.SYSTEM,
        causation_id=str(watch_id),
        correlation_id=str(uuid4()),
        payload=WatchTriggeredPayload(
            watch_id=watch_id,
            watch_generation=1,
            trigger_event_id=uuid4(),
            continuation=continuation,
        ),
    )

    await pipeline.start()
    try:
        # The typed payload arrives in-process AFTER the enqueue (the fire()
        # commit raced the registration); the grace window must cover it.
        await pipeline.submit_watch_input(watch_input)
        await wait_until(lambda: len(backend.submit_calls) == 1)

        call = backend.submit_calls[0]
        assert call.delivery_state_at_submit == "dispatched"
        part = call.request.parts[0]
        assert part.kind is AgentInputKind.WATCH_TRIGGERED
        assert part.trust is TurnPartTrust.UNTRUSTED  # terminal observation
        assert part.text == continuation
        assert call.request.idempotency_key == delivery_key
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# SSE consumption: run state machine + canonical event persistence.
# ---------------------------------------------------------------------------


async def test_sse_events_advance_run_and_persist_canonical_events(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "hello", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)
        runs = await _runs(repositories, conversation_id)
        assert runs[0].run_state == "queued"

        backend_run_id = uuid4()
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_STARTED,
                provider_ref="provider-1",
                run_id=backend_run_id,
                dedup_key="evt-run-started",
            )
        )

        async def run_started() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(
                run.run_state == "running"
                and run.backend_run_id == str(backend_run_id)
                for run in runs
            )

        await wait_until(run_started)

        delta_text = "streaming chunk"
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.MESSAGE_DELTA,
                provider_ref="provider-1",
                text=delta_text,
                dedup_key="evt-delta",
            )
        )
        completed_text = "deployment is healthy"
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.MESSAGE_COMPLETED,
                provider_ref="provider-1",
                text=completed_text,
                dedup_key="evt-completed",
            )
        )
        await wait_until(
            lambda: _messages_count(repositories, conversation_id)
        )

        # MESSAGE_DELTA is an ephemeral event row only: no message assembly.
        messages = await _messages(repositories, conversation_id)
        assert len(messages) == 1
        assert messages[0].role == "assistant"
        assert messages[0].kind == "text"
        assert messages[0].is_final is True
        assert messages[0].body_digest == hashlib.sha256(
            completed_text.encode("utf-8")
        ).hexdigest()
        assert messages[0].run_id == runs[0].id

        # Digest invariant (M6a): every persisted payload hashes to its digest.
        events = await _events(repositories, conversation_id)
        kinds = [event.event_kind for event in events]
        assert kinds == ["run_started", "message_delta", "message_completed"]
        delta_row = events[1]
        assert delta_row.ephemeral is True
        assert delta_row.payload is not None
        assert hashlib.sha256(
            delta_row.payload.encode("utf-8")
        ).hexdigest() == delta_row.payload_digest
        assert '"text":"streaming chunk"' in delta_row.payload
        completed_row = events[2]
        assert completed_row.ephemeral is False

        # RUN_COMPLETED moves the run to its terminal state.
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_COMPLETED,
                provider_ref="provider-1",
                dedup_key="evt-run-completed",
            )
        )

        async def run_completed() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(run.run_state == "completed" for run in runs)

        await wait_until(run_completed)
    finally:
        await pipeline.stop()


async def _messages_count(repositories: RepositoryBundle, conversation_id: UUID) -> bool:
    return len(await _messages(repositories, conversation_id)) == 1


async def test_run_failed_event_fails_run_with_error_code(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "boom", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_STARTED,
                provider_ref="provider-1",
                dedup_key="evt-start",
            )
        )

        async def run_running() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(run.run_state == "running" for run in runs)

        await wait_until(run_running)
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_FAILED,
                provider_ref="provider-1",
                error_code="backend_error_42",
                dedup_key="evt-failed",
            )
        )

        async def run_failed() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(
                run.run_state == "failed" and run.error_code == "backend_error_42"
                for run in runs
            )

        await wait_until(run_failed)
    finally:
        await pipeline.stop()


async def test_duplicate_dedup_key_persists_single_row_and_start_is_idempotent(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "hi", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)

        notification = make_notification(
            backend,
            pipeline,
            kind=AgentEventKind.RUN_STARTED,
            provider_ref="provider-1",
            run_id=uuid4(),
            dedup_key="same-dedup",
        )
        backend.push_event(notification)
        backend.push_event(notification)

        async def event_persisted() -> bool:
            return len(await _events(repositories, conversation_id)) == 1

        await wait_until(event_persisted)
        # Give the second delivery a beat to prove it stays a single row.
        await asyncio.sleep(0.02)
        events = await _events(repositories, conversation_id)
        assert len(events) == 1
        runs = await _runs(repositories, conversation_id)
        assert runs[0].run_state == "running"
        assert pipeline.diagnostics.run_start_idempotent == 1
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# Ownership and run mapping: drop foreign/unmapped events.
# ---------------------------------------------------------------------------


async def _seed_backend_ref(
    repositories: RepositoryBundle, backend: FakeBackend, conversation_id: UUID
) -> None:
    await repositories.agent_backend_conversations.create(
        conversation_id=conversation_id,
        backend_kind="fake",
        runtime_id=backend.runtime_id,
        binding_capability_epoch=1,
        provider_ref="provider-1",
    )


async def test_foreign_binding_events_are_dropped(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await _seed_backend_ref(repositories, backend, conversation_id)
    await pipeline.start()
    try:
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.MESSAGE_COMPLETED,
                provider_ref="provider-1",
                text="foreign",
                binding_id=str(uuid4()),
            )
        )
        await wait_until(lambda: pipeline.diagnostics.ownership_dropped == 1)
        assert await _events(repositories, conversation_id) == []
        assert await _messages(repositories, conversation_id) == []
    finally:
        await pipeline.stop()


async def test_foreign_runtime_events_are_dropped(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await _seed_backend_ref(repositories, backend, conversation_id)
    await pipeline.start()
    try:
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.MESSAGE_COMPLETED,
                provider_ref="provider-1",
                text="foreign",
                runtime_id="runtime-other",
            )
        )
        await wait_until(lambda: pipeline.diagnostics.ownership_dropped == 1)
        assert await _events(repositories, conversation_id) == []
    finally:
        await pipeline.stop()


async def test_event_without_active_run_is_dropped(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await _seed_backend_ref(repositories, backend, conversation_id)
    await pipeline.start()
    try:
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.MESSAGE_COMPLETED,
                provider_ref="provider-1",
                text="orphan",
            )
        )
        await wait_until(lambda: pipeline.diagnostics.no_active_run_dropped == 1)
        assert await _events(repositories, conversation_id) == []
        assert await _messages(repositories, conversation_id) == []
    finally:
        await pipeline.stop()


async def test_terminal_run_treats_late_events_as_processed(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    """Risk 6: an event arriving after the run is terminal is a no-op."""
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await _seed_backend_ref(repositories, backend, conversation_id)
    run = await repositories.agent_runs.create(
        conversation_id=conversation_id, run_state="completed"
    )
    await pipeline.start()
    try:
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_COMPLETED,
                provider_ref="provider-1",
                run_id=run.id,
            )
        )
        await asyncio.sleep(0.05)
        assert pipeline.diagnostics.no_active_run_dropped == 0
        assert await _events(repositories, conversation_id) == []
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# Fail-closed submission.
# ---------------------------------------------------------------------------


async def test_retryable_submit_fails_run_and_parks_delivery_without_retry(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    backend.submit_result = BackendSubmitResult(
        outcome=BackendOutcome.RETRYABLE, retry_safe=False
    )
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        admission = await pipeline.submit_user_message(
            conversation_id, "risky", actor="admin"
        )

        async def run_failed() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(
                run.run_state == "failed" and run.error_code == "submit_retryable"
                for run in runs
            )

        await wait_until(run_failed)
        # Never auto-retried: exactly one submit call, parked delivery_unknown.
        await asyncio.sleep(0.05)
        assert len(backend.submit_calls) == 1
        assert (
            pipeline.inbox_machine._parked.get(admission.message_id)
            == "delivery_unknown"
        )
        assert pipeline.diagnostics.submits_failed == 1
    finally:
        await pipeline.stop()


async def test_submit_exception_fails_closed_without_retry(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    backend.submit_exception = RuntimeError("connection reset")
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        admission = await pipeline.submit_user_message(
            conversation_id, "unreachable", actor="admin"
        )

        async def run_failed() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(
                run.run_state == "failed" and run.error_code == "submit_retryable"
                for run in runs
            )

        await wait_until(run_failed)
        await asyncio.sleep(0.05)
        assert len(backend.submit_calls) == 1
        assert (
            pipeline.inbox_machine._parked.get(admission.message_id)
            == "delivery_unknown"
        )
    finally:
        await pipeline.stop()


async def test_supervisor_recheck_rejection_fails_closed(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    supervisor = FakeSupervisor(accept=False)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(
        repositories, hub, backend, supervisor=supervisor, binding_id=binding_id
    )
    await pipeline.start()
    try:
        admission = await pipeline.submit_user_message(
            conversation_id, "stale epoch", actor="admin"
        )

        async def run_failed() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(
                run.run_state == "failed" and run.error_code == "runtime_not_ready"
                for run in runs
            )

        await wait_until(run_failed)
        assert len(backend.submit_calls) == 0
        assert supervisor.calls == [(RuntimeRef(backend.runtime_id), backend.epoch)]
        assert (
            pipeline.inbox_machine._parked.get(admission.message_id)
            == "delivery_unknown"
        )
    finally:
        await pipeline.stop()


async def test_missing_payload_grace_exhaustion_fails_closed(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    """B restart: the inbox row survives but the in-process payload is gone."""
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(
        repositories,
        hub,
        backend,
        payload_grace_attempts=2,
        payload_grace_delay=0.001,
        binding_id=binding_id,
    )
    item = await repositories.agent_inbox.enqueue(
        conversation_id=conversation_id,
        kind=AgentInputKind.USER_MESSAGE.value,
        actor_id="admin",
        actor_kind=AgentActorKind.USER_SESSION.value,
        idempotency_key=str(uuid4()),
        payload_digest=hashlib.sha256(b"lost").hexdigest(),
        source=AgentInputSource.USER.value,
    )
    await pipeline.start()
    try:
        await wait_until(
            lambda: pipeline.inbox_machine._parked.get(item.id) == "delivery_unknown"
        )
        assert len(backend.submit_calls) == 0
        assert pipeline.diagnostics.missing_payload_delivery_unknown == 1
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# Serialization (plan gate 6 / spec §7).
# ---------------------------------------------------------------------------


async def test_same_conversation_second_item_waits_at_claim_gate(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        first = await pipeline.submit_user_message(
            conversation_id, "first", actor="admin"
        )
        second = await pipeline.submit_user_message(
            conversation_id, "second", actor="admin"
        )
        await wait_until(lambda: len(backend.submit_calls) == 1)
        assert backend.submit_calls[0].request.idempotency_key == first.idempotency_key

        # The second item stays pending while the first turn is in flight...
        rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
        by_id = {row.id: row for row in rows}
        assert by_id[second.message_id].delivery_state == "pending"

        # ...and even after the run completes (the M1.4 inbox schema keeps a
        # dispatched item in-flight until a later milestone persists terminal
        # delivery states; the claim gate never admits a second turn).
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_STARTED,
                provider_ref="provider-1",
            )
        )
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_COMPLETED,
                provider_ref="provider-1",
            )
        )

        async def run_completed() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(run.run_state == "completed" for run in runs)

        await wait_until(run_completed)
        await asyncio.sleep(0.05)
        assert len(backend.submit_calls) == 1
        rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
        by_id = {row.id: row for row in rows}
        assert by_id[second.message_id].delivery_state == "pending"
    finally:
        await pipeline.stop()


async def test_two_conversations_serialize_on_one_binding(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    binding_id, conversation_a = await seed_conversation(repositories)
    backend = FakeBackend(repositories=repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    # Second conversation on the same binding: the pipeline only claims items
    # of its own binding's conversations.
    _, conversation_b = await seed_conversation(repositories, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_a, "a", actor="admin")
        await pipeline.submit_user_message(conversation_b, "b", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)
        assert backend.submit_calls[0].request.parts[0].text == "a"

        # B waits in the inbox while A's run is active (per-binding serial).
        rows_b = await repositories.agent_inbox.list_for_conversation(conversation_b)
        assert rows_b[0].delivery_state == "pending"
        await asyncio.sleep(0.05)
        assert len(backend.submit_calls) == 1

        # A's run terminal releases the dispatcher; B is submitted next.
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_STARTED,
                provider_ref="provider-1",
            )
        )
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_COMPLETED,
                provider_ref="provider-1",
            )
        )
        await wait_until(lambda: len(backend.submit_calls) == 2)
        assert backend.submit_calls[1].request.parts[0].text == "b"
        assert backend.submit_calls[1].request.conversation_ref.provider_ref == (
            "provider-2"
        )
    finally:
        await pipeline.stop()


async def test_one_active_run_refuses_submission_and_falls_back_to_waiting(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    # Recovery leftover: a run already in running.
    run = await repositories.agent_runs.create(conversation_id=conversation_id)
    await repositories.agent_runs.set_state(run.id, "running", expected_state="queued")
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "late", actor="admin")

        async def item_fell_back() -> bool:
            rows = await repositories.agent_inbox.list_for_conversation(
                conversation_id
            )
            return any(
                row.kind == "user_message"
                and row.delivery_state in ("retry_wait", "dead_letter")
                for row in rows
            )

        await wait_until(item_fell_back)
        await asyncio.sleep(0.02)
        # The submission was refused while the run is active: never a submit.
        assert len(backend.submit_calls) == 0
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# Disconnect/reconnect stub (spec §5; reconcile lands next task).
# ---------------------------------------------------------------------------


async def test_disconnect_triggers_bounded_backoff_reconnect(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "hi", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_STARTED,
                provider_ref="provider-1",
                dedup_key="before-disconnect",
            )
        )
        await wait_until(lambda: backend.events_calls == 1)

        async def event_persisted() -> bool:
            return len(await _events(repositories, conversation_id)) == 1

        await wait_until(event_persisted)

        # Stream ends cleanly: the consumer reconnects with backoff.
        backend.disconnect()
        await wait_until(lambda: pipeline.diagnostics.reconnects == 1)
        await wait_until(lambda: backend.events_calls >= 2)

        # The resubscribed stream keeps consuming.
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_COMPLETED,
                provider_ref="provider-1",
                dedup_key="after-reconnect",
            )
        )

        async def run_completed() -> bool:
            runs = await _runs(repositories, conversation_id)
            return bool(runs) and all(run.run_state == "completed" for run in runs)

        await wait_until(run_completed)
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# Canonical payload invariant (unit level).
# ---------------------------------------------------------------------------


def test_canonical_payload_json_digests_deterministically() -> None:
    from termflow_control_plane.plugins.agent_broker.agent.pipeline import (
        _canonical_payload_json,
    )

    notification = BackendNotification(
        conversation_ref=BackendConversationRef(
            backend_kind="fake",
            backend_version="0.1.0",
            runtime_id=RUNTIME_ID,
            binding_capability_epoch=1,
            provider_ref="provider-1",
        ),
        scope=BackendEventScope(binding_id="binding-1", runtime_epoch=1),
        kind=AgentEventKind.MESSAGE_DELTA,
        run_id=uuid4(),
        message_id=uuid4(),
        dedup_key="dedup-1",
        payload=NotificationPayload(text="hello"),
    )
    first = _canonical_payload_json(notification)
    second = _canonical_payload_json(notification)
    assert first == second
    parsed = json.loads(first)
    assert parsed["text"] == "hello"
    assert hashlib.sha256(first.encode("utf-8")).hexdigest() == hashlib.sha256(
        second.encode("utf-8")
    ).hexdigest()
