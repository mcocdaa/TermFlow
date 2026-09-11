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
    BackendInteraction,
    BackendNotification,
    BackendOperationResult,
    BackendOutcome,
    BackendSubmitResult,
    BackendTurnRequest,
    CreateBackendConversation,
    NotificationPayload,
    TurnPartTrust,
)
from termflow_protocol.agent import (
    AgentEventKind,
    AgentInputKind,
    BackendRuntimeState,
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
        self.reconcile_calls: list[BackendConversationRef] = []
        self.reconcile_results: list[BackendConversationSnapshot] = []
        self.reconcile_hook: Callable[[], Awaitable[None]] | None = None
        self.cancel_calls = []
        self.cancel_result = BackendOperationResult(outcome=BackendOutcome.CONFIRMED)
        self.interact_calls: list[BackendInteraction] = []
        self.interact_result = BackendOperationResult(outcome=BackendOutcome.CONFIRMED)
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

    async def reconcile(self, ref: BackendConversationRef) -> BackendConversationSnapshot:
        self.reconcile_calls.append(ref)
        if self.reconcile_hook is not None:
            await self.reconcile_hook()
        if self.reconcile_results:
            return self.reconcile_results.pop(0)
        return BackendConversationSnapshot(
            conversation_ref=ref, outcome=BackendOutcome.CONFIRMED, resumable=False
        )

    async def cancel(self, request) -> BackendOperationResult:
        self.cancel_calls.append(request)
        return self.cancel_result

    async def interact(self, request) -> BackendOperationResult:
        self.interact_calls.append(request)
        return self.interact_result

    async def delete_conversation(self, ref) -> BackendOperationResult:
        return BackendOperationResult(outcome=BackendOutcome.CONFIRMED)


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
        "cancel_wait_timeout": 0,
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
        supervisor=None,
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
    installation = await repositories.installations.create(digest_secret(f"computer-{uuid4().hex}"))
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
    summary: str | None = None,
    error_code: str | None = None,
    dedup_key: str | None = None,
    run_id: UUID | None = None,
    message_id: UUID | None = None,
    tool_call_id: str | None = None,
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
        tool_call_id=tool_call_id,
        dedup_key=dedup_key or f"dedup-{uuid4()}",
        payload=NotificationPayload(
            text=text,
            summary=summary,
            error_code=error_code,
        ),
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


async def _messages_count(repositories: RepositoryBundle, conversation_id: UUID) -> bool:
    """True once exactly one assistant message row is assembled."""
    messages = await _messages(repositories, conversation_id)
    return sum(1 for message in messages if message.role == "assistant") == 1


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
        row = await repositories.agent_inbox.get_by_idempotency_key(admission.idempotency_key)
        assert row is not None
        assert row.kind == "user_message"
        assert row.source == "user"
        assert row.payload_digest == hashlib.sha256(b"please fix the build").hexdigest()

        # Review fix (payload durability): the typed payload is persisted as
        # the user message row BEFORE the enqueue, digest-verified.
        messages = await _messages(repositories, conversation_id)
        assert [message.role for message in messages] == ["user"]
        assert messages[0].body == "please fix the build"
        assert (
            messages[0].body_digest
            == hashlib.sha256(b"please fix the build").hexdigest()
        )

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

        # Review fix (multi-turn): the run's terminal outcome persists
        # `delivered` for the dispatched inbox item, freeing the gate.
        async def delivered() -> bool:
            rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
            return bool(rows) and all(
                row.delivery_state == "delivered" for row in rows
            )

        await wait_until(delivered)
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
                run.run_state == "running" and run.backend_run_id == str(backend_run_id)
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
        await wait_until(lambda: _messages_count(repositories, conversation_id))

        # MESSAGE_DELTA is an ephemeral event row only: no message assembly.
        messages = await _messages(repositories, conversation_id)
        assistant_messages = [message for message in messages if message.role == "assistant"]
        assert len(assistant_messages) == 1
        assert assistant_messages[0].kind == "text"
        assert assistant_messages[0].is_final is True
        assert assistant_messages[0].body == completed_text
        assert (
            assistant_messages[0].body_digest
            == hashlib.sha256(completed_text.encode("utf-8")).hexdigest()
        )
        assert assistant_messages[0].run_id == runs[0].id

        # Review fix (payload durability): the admitted user message text was
        # persisted as the user message row at admission time.
        user_messages = [message for message in messages if message.role == "user"]
        assert len(user_messages) == 1
        assert user_messages[0].body == "hello"
        assert (
            user_messages[0].body_digest
            == hashlib.sha256(b"hello").hexdigest()
        )

        # Digest invariant (M6a): every persisted payload hashes to its digest.
        events = await _events(repositories, conversation_id)
        kinds = [event.event_kind for event in events]
        assert kinds == ["run_started", "message_delta", "message_completed"]
        delta_row = events[1]
        assert delta_row.ephemeral is True
        assert delta_row.payload is not None
        assert (
            hashlib.sha256(delta_row.payload.encode("utf-8")).hexdigest()
            == delta_row.payload_digest
        )
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


async def test_canonical_payloads_carry_agui_projection_keys(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    """Tool/permission/state events persist the keys the AG-UI projector reads.

    Previously TOOL_STARTED/TOOL_COMPLETED/PERMISSION_REQUESTED/
    BACKEND_STATE_CHANGED were dropped 100% as malformed under ?wire=agui
    because the canonical payload never wrote tool_name/status/state/epoch.
    """
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "inspect panes", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.RUN_STARTED,
                provider_ref="provider-1",
                dedup_key="evt-run-started",
            )
        )
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.TOOL_STARTED,
                provider_ref="provider-1",
                tool_call_id="call-1",
                summary="termflow_pane_read",
                dedup_key="evt-tool-started",
            )
        )
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.TOOL_COMPLETED,
                provider_ref="provider-1",
                tool_call_id="call-1",
                dedup_key="evt-tool-completed",
            )
        )
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.BACKEND_STATE_CHANGED,
                provider_ref="provider-1",
                summary="idle",
                dedup_key="evt-state",
            )
        )
        backend.push_event(
            make_notification(
                backend,
                pipeline,
                kind=AgentEventKind.PERMISSION_REQUESTED,
                provider_ref="provider-1",
                tool_call_id="call-2",
                summary="write pane",
                dedup_key="evt-permission",
            )
        )

        async def five_events() -> bool:
            events = await _events(repositories, conversation_id)
            return len(events) == 5

        await wait_until(five_events)
        events = await _events(repositories, conversation_id)
        by_kind = {event.event_kind: event for event in events}

        tool_started = by_kind["tool_started"]
        assert tool_started.payload is not None
        assert '"tool_name":"termflow_pane_read"' in tool_started.payload
        tool_completed = by_kind["tool_completed"]
        assert tool_completed.payload is not None
        assert '"status":"success"' in tool_completed.payload
        state = by_kind["backend_state_changed"]
        assert state.payload is not None
        assert '"state":"idle"' in state.payload
        assert '"epoch":1' in state.payload
        permission = by_kind["permission_requested"]
        assert permission.payload is not None
        assert '"tool_call_id":"call-2"' in permission.payload

        # The digest invariant still holds for the kind-aware payloads.
        for event in events:
            if event.payload is not None:
                assert (
                    hashlib.sha256(event.payload.encode("utf-8")).hexdigest()
                    == event.payload_digest
                )
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# Serialization (plan gate 6 / spec §7).
# ---------------------------------------------------------------------------


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
        context_a = backend.submit_calls[0].request.parts[-1]
        assert context_a.kind is AgentInputKind.SYSTEM_NOTIFICATION
        assert context_a.trust is TurnPartTrust.TRUSTED
        assert str(conversation_a) in (context_a.text or "")

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
        context_b = backend.submit_calls[1].request.parts[-1]
        assert context_b.kind is AgentInputKind.SYSTEM_NOTIFICATION
        assert str(conversation_b) in (context_b.text or "")
        assert backend.submit_calls[1].request.conversation_ref.provider_ref == ("provider-2")
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# Multi-turn conversations (review fix: the claim gate frees on run terminal).
# ---------------------------------------------------------------------------


async def test_same_conversation_second_turn_is_admitted_after_run_terminal(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    """The claim gate admits a second turn once the first run terminates.

    Previously the M1.4 inbox kept a dispatched item in-flight forever, so
    the second message stayed ``pending`` silently.  The run's terminal
    outcome must persist ``delivered`` and free the gate (plan §2.1).
    """
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        first = await pipeline.submit_user_message(conversation_id, "first", actor="admin")
        second = await pipeline.submit_user_message(conversation_id, "second", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)
        assert backend.submit_calls[0].request.idempotency_key == first.idempotency_key

        # The second item stays pending while the first turn is in flight...
        rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
        by_id = {row.id: row for row in rows}
        assert by_id[second.message_id].delivery_state == "pending"

        # ...and is admitted after the run terminates.
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
        assert backend.submit_calls[1].request.idempotency_key == second.idempotency_key
        assert backend.submit_calls[1].request.parts[0].text == "second"

        rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
        by_id = {row.id: row for row in rows}
        assert by_id[first.message_id].delivery_state == "delivered"
        assert by_id[second.message_id].delivery_state == "dispatched"
    finally:
        await pipeline.stop()


async def test_public_path_reuses_backend_ref_across_turns(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    """The public submit path can run a second turn on the same backend session.

    Previously the public path could never reach a second submission, so the
    backend-ref reuse branch was only exercisable directly.  Two admitted
    messages must reuse the one created backend conversation.
    """
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "one", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)
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
        await wait_until(lambda: backend.created_sessions == 1)

        await pipeline.submit_user_message(conversation_id, "two", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 2)

        assert backend.created_sessions == 1
        assert all(
            call.request.conversation_ref.provider_ref == "provider-1"
            for call in backend.submit_calls
        )
        assert [call.request.parts[0].text for call in backend.submit_calls] == [
            "one",
            "two",
        ]
    finally:
        await pipeline.stop()


async def test_restart_recovers_pending_user_payload_from_persisted_row(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    """A B restart must not fail-close a 202-acked message into delivery_unknown.

    The typed payload is persisted with the admission (the user message row,
    digest-verified), so a fresh pipeline can still render and deliver the
    turn (plan §7 "persist an Agent Inbox item before submitting it").
    """
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    first = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    # The first pipeline admits the message but is never started: its
    # in-process payload table dies with the simulated restart.
    admission = await first.submit_user_message(
        conversation_id, "survive restart", actor="admin"
    )
    assert admission.delivery_state == "pending"
    await first.stop()

    restarted = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await restarted.start()
    try:
        await wait_until(lambda: len(backend.submit_calls) == 1)
        call = backend.submit_calls[0]
        assert call.request.idempotency_key == admission.idempotency_key
        assert call.request.parts[0].text == "survive restart"
        # The durable admission row advanced to dispatched (not delivery_unknown).
        rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
        assert [row.delivery_state for row in rows] == ["dispatched"]
    finally:
        await restarted.stop()


# ---------------------------------------------------------------------------
# Cancellation terminal delivery (plan §7: dispatched -> cancel_requested ->
# cancelled|unknown).
# ---------------------------------------------------------------------------


async def test_cancel_proven_terminal_marks_delivery_cancelled(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "stop me", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)

        backend.reconcile_results = [
            BackendConversationSnapshot(
                conversation_ref=backend.submit_calls[0].ref,
                outcome=BackendOutcome.CONFIRMED,
                state=BackendRuntimeState.CLOSED,
            )
        ]
        result = await pipeline.cancel_conversation(conversation_id, reason="user request")
        assert result.outcome is BackendOutcome.CONFIRMED
        assert result.run_state == "cancelled"

        rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
        assert [row.delivery_state for row in rows] == ["cancelled"]
        # The gate is freed: the conversation can run another turn.
        await pipeline.submit_user_message(conversation_id, "resume", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 2)
    finally:
        await pipeline.stop()


async def test_cancel_unproven_outcome_marks_delivery_unknown(
    repositories: RepositoryBundle, hub: AgentStreamHub
) -> None:
    backend = FakeBackend(repositories=repositories)
    binding_id, conversation_id = await seed_conversation(repositories)
    pipeline = make_pipeline(repositories, hub, backend, binding_id=binding_id)
    await pipeline.start()
    try:
        await pipeline.submit_user_message(conversation_id, "busy turn", actor="admin")
        await wait_until(lambda: len(backend.submit_calls) == 1)

        # The default reconcile snapshot is CONFIRMED with state RECONCILING:
        # the cancellation cannot be proven terminal, so the delivery becomes
        # the visible recoverable `delivery_unknown` (never auto-resubmitted).
        result = await pipeline.cancel_conversation(conversation_id, reason="user request")
        assert result.outcome is BackendOutcome.CONFIRMED
        assert result.run_state == "unknown"

        rows = await repositories.agent_inbox.list_for_conversation(conversation_id)
        assert [row.delivery_state for row in rows] == ["delivery_unknown"]
        await asyncio.sleep(0.05)
        # Never an automatic duplicate action.
        assert len(backend.submit_calls) == 1
    finally:
        await pipeline.stop()


# ---------------------------------------------------------------------------
# Disconnect/reconcile/reconnect (spec §5).
# ---------------------------------------------------------------------------


async def test_disconnect_reconciles_active_conversation_before_reconnect(
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
            )
        )
        await wait_until(
            lambda: pipeline._active_run_id is not None,
            message="dispatcher did not begin waiting for the active run",
        )

        backend.disconnect()

        await wait_until(lambda: len(backend.reconcile_calls) == 1)
        assert backend.reconcile_calls[0].provider_ref == "provider-1"
        runs = await _runs(repositories, conversation_id)
        assert [run.run_state for run in runs] == ["running"]
        await wait_until(lambda: backend.events_calls >= 2)
    finally:
        await pipeline.stop()
