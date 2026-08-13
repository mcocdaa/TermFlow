"""Approval-gated CommandService tests (plan §12.1, task M5.2; spec test matrix).

Covers the synchronous wait-then-execute flow of
:class:`~termflow_control_plane.plugins.agent_broker.agent.command_service.CommandService`:

- wait timeout revokes the pending approval (``system:tool_timeout``) and
  returns ``approval_required`` + approval_id; a later decide is rejected;
  cancellation of a pending call revokes it too (guard-timeout path).
- revoke during the wait / after approval exit without sending.
- auth-epoch change after approval is refused with a ``system:auth_epoch_changed``
  revoke; expiry and binding-state prechecks refuse too.
- the pre-execution hash recheck against the CURRENT ledger rejects drifted
  targets (cursor or incarnation) with ``INCARNATION_CHANGED`` + a
  ``system:target_changed`` revoke, while a late-in-TTL approval still
  matches because the recheck uses the stored ``expires_at`` (frozen).
- settle rules: confirmed -> consume, known failure -> consume,
  uncertain outcome -> unknown (terminal); one-time use is enforced by the
  settle CAS and the gateway is never called twice for one tool call.
- in-flight revoke race: the send completes, the factual receipt is
  returned, and the approval stays revoked.
- unknown named keys fail closed with ``invalid_request`` before any
  approval is created; a replayed tool_call_id maps to ``approval_conflict``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.command_service import (
    CommandRouterGateway,
    CommandService,
    OutcomeUnknownError,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import (
    ApprovalAlreadyConsumed,
    ApprovalAlreadyDecided,
    ApprovalPolicy,
    ApprovalRevoked,
    ApprovalState,
)
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import TermFlowToolError
from termflow_control_plane.plugins.agent_broker.auth import (
    SCOPE_TERMINAL_OBSERVE,
    SCOPE_TERMINAL_WRITE,
    AgentTokenPrincipal,
)
from termflow_protocol import CommandResultPayload, PaneCursor
from termflow_protocol.agent import ApprovalDecision
from termflow_protocol.mcp import (
    PaneSendKeysParams,
    PaneSendTextParams,
    TermFlowErrorCode,
)

#: Fixed reference instant so the fake clock is deterministic.
T0 = datetime(2030, 6, 1, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeObservation:
    """Ledger fake: resolve_cursor returns the configured cursor (or None)."""

    def __init__(self, cursor: PaneCursor | None = None) -> None:
        self.cursor = cursor

    async def resolve_cursor(self, instance_id: UUID, pane_id: str) -> PaneCursor | None:
        return self.cursor


class FakeGateway:
    """CommandGateway fake recording sends; can fail or stall on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.raise_error: TermFlowToolError | None = None
        self.raise_unknown: bool = False
        self.stall: asyncio.Event | None = None
        self.auto_revoke_during_send: UUID | None = None
        self.policy: ApprovalPolicy | None = None

    async def send_text(
        self,
        instance_id: UUID,
        pane_id: str,
        text: str,
        submit: bool,
        idempotency_key: UUID,
        pane_incarnation: int | None,
    ) -> CommandResultPayload:
        self.calls.append(
            ("send_text", instance_id, pane_id, text, submit, idempotency_key, pane_incarnation)
        )
        return await self._finish()

    async def send_keys(
        self,
        instance_id: UUID,
        pane_id: str,
        keys: tuple[str, ...],
        idempotency_key: UUID,
        pane_incarnation: int | None,
    ) -> CommandResultPayload:
        self.calls.append(
            ("send_keys", instance_id, pane_id, keys, idempotency_key, pane_incarnation)
        )
        return await self._finish()

    async def _finish(self) -> CommandResultPayload:
        if self.stall is not None:
            await self.stall.wait()
        if self.auto_revoke_during_send is not None and self.policy is not None:
            await self.policy.revoke(self.auto_revoke_during_send, actor="admin")
        if self.raise_unknown:
            raise OutcomeUnknownError("simulated uncertain outcome")
        if self.raise_error is not None:
            raise self.raise_error
        return CommandResultPayload(command_id=uuid4(), idempotency_key=uuid4(), ok=True)


def _cursor(*, incarnation: int = 2, seq: int = 7) -> PaneCursor:
    return PaneCursor(
        instance_id=uuid4(),
        pane_id="%1",
        pane_incarnation=incarnation,
        stream_id=uuid4(),
        seq=seq,
    )


def _principal(binding_id: UUID, instance_id: UUID) -> AgentTokenPrincipal:
    return AgentTokenPrincipal(
        binding_id=binding_id,
        instance_id=instance_id,
        scopes=frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE}),
        runtime_epoch=1,
    )


def _text_params(conversation_id: UUID, **overrides: object) -> PaneSendTextParams:
    values: dict[str, object] = {
        "pane_id": "%1",
        "request_key": "req-text",
        "conversation_id": conversation_id,
        "intent": "run the tests",
        "text": "make test",
        "submit": True,
    }
    values.update(overrides)
    return PaneSendTextParams(**values)


def _keys_params(conversation_id: UUID, **overrides: object) -> PaneSendKeysParams:
    values: dict[str, object] = {
        "pane_id": "%1",
        "request_key": "req-keys",
        "conversation_id": conversation_id,
        "intent": "interrupt",
        "keys": ("ctrl-c",),
    }
    values.update(overrides)
    return PaneSendKeysParams(**values)


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'commands.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def seeded(repositories: RepositoryBundle) -> tuple[UUID, UUID, UUID]:
    """Create profile/term/binding/conversation; return (binding, instance, conversation)."""
    profile = await repositories.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    installation = await repositories.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    term = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        f"term-{uuid4().hex[:8]}",
        digest_secret(f"instance-{uuid4().hex}"),
    )
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="ready",
        runtime_ref="runtime-1",
        runtime_epoch=1,
        capability_ref="cap-1",
    )
    conversation = await repositories.agent_conversations.create(
        binding_id=binding.id,
        title="command-service",
    )
    return binding.id, term.id, conversation.id


def _service(
    repositories: RepositoryBundle,
    gateway: FakeGateway,
    observation: FakeObservation,
    clock: FakeClock,
    *,
    wait_timeout: float = 3600.0,
    ttl: float = 300.0,
) -> CommandService:
    return CommandService(
        repositories=repositories,
        sessions=repositories.session_factory,  # type: ignore[attr-defined]
        gateway=gateway,
        observation=observation,
        clock=clock,
        approval_wait_timeout_seconds=wait_timeout,
        approval_ttl_seconds=ttl,
        poll_interval_seconds=0.01,
    )


async def _approve(
    service: CommandService, approval_id: UUID, *, epoch: int = 1, actor: str = "admin"
) -> None:
    await service.policy.decide(
        approval_id,
        decision=ApprovalDecision.APPROVED,
        actor=actor,
        auth_epoch=epoch,
    )


async def _wait_for_approval(
    repositories: RepositoryBundle, conversation_id: UUID, tool_call_id: str
):
    """Poll until the tool call produced a persisted approval row."""
    for _ in range(400):
        approval = await repositories.approvals.get_by_tool_call(
            conversation_id, tool_call_id
        )
        if approval is not None:
            return approval
        await asyncio.sleep(0.005)
    raise AssertionError(f"approval for tool call {tool_call_id!r} was never created")


class TestWaitAndDecision:
    async def test_approval_created_pending_and_confirmed_flow(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(
            repositories, gateway, FakeObservation(_cursor()), clock, ttl=60.0
        )
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        # The approval exists and is pending with the hash bound to the write.
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        assert pending.state == ApprovalState.PENDING
        assert pending.auth_epoch == 1
        assert pending.expires_at is not None

        await _approve(service, pending.id)
        result = await asyncio.wait_for(task, timeout=5)
        assert result.ok is True
        assert result.outcome == "confirmed"
        assert result.approval_id == pending.id
        assert result.request_key == "req-text"
        # The gateway received the ledger incarnation and the submit flag.
        call = gateway.calls[0]
        assert call[0] == "send_text"
        assert call[3] == "make test"
        assert call[4] is True
        assert call[6] == 2
        consumed = await repositories.approvals.get_by_id(pending.id)
        assert consumed is not None
        assert consumed.state == ApprovalState.CONSUMED

    async def test_wait_timeout_revokes_and_returns_approval_required(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        # TTL longer than the wait window: only the deadline decides the
        # timeout, so the post-hoc decision hits the revoked state (not the
        # expiry check) and reports approval_revoked.
        service = _service(
            repositories, gateway, FakeObservation(_cursor()), clock, ttl=7200.0
        )
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        clock.advance(3601)
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.APPROVAL_REQUIRED
        assert caught.value.data == {"approval_id": str(pending.id)}
        # The pending approval was revoked by the timeout, never left to
        # become an approved-but-unexecuted zombie.
        revoked = await repositories.approvals.get_by_id(pending.id)
        assert revoked is not None
        assert revoked.state == ApprovalState.REVOKED
        assert revoked.decision == ApprovalState.REVOKED.value
        assert gateway.calls == []
        # A later decision can never win.
        with pytest.raises(ApprovalRevoked):
            await _approve(service, pending.id)

    async def test_cancelled_wait_revokes_pending_approval(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_keys(
                principal, _keys_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The finally path revoked the still-pending approval (guard-timeout
        # cancellation never leaves a zombie behind).
        revoked = await repositories.approvals.get_by_id(pending.id)
        assert revoked is not None
        assert revoked.state == ApprovalState.REVOKED
        assert gateway.calls == []

    async def test_denied_decision_exits_with_approval_denied(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await service.policy.decide(
            pending.id,
            decision=ApprovalDecision.DENIED,
            actor="admin",
            auth_epoch=1,
        )
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.APPROVAL_DENIED
        assert gateway.calls == []

    async def test_revoked_during_wait_exits_without_sending(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await service.policy.revoke(pending.id, actor="admin")
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.APPROVAL_REVOKED
        assert gateway.calls == []

    async def test_revoked_after_approval_before_send_exits_without_sending(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        # Human revoke lands between the decision and the final state read.
        await service.policy.revoke(pending.id, actor="admin")
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.APPROVAL_REVOKED
        assert gateway.calls == []


class TestPreflightRechecks:
    async def test_auth_epoch_change_after_approval_is_refused(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        # Reset the persisted epoch BEFORE the approval is decided: the
        # decide CAS still binds the row's own epoch (1), and the preflight
        # then deterministically observes the stale persisted epoch.
        await repositories.auth_state.reset_and_increment_epoch()
        await _approve(service, pending.id, epoch=1)
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
        # The approved-but-stale approval was revoked, not left approved.
        revoked = await repositories.approvals.get_by_id(pending.id)
        assert revoked is not None
        assert revoked.state == ApprovalState.REVOKED
        assert revoked.decision == ApprovalState.REVOKED.value
        assert gateway.calls == []

    async def test_expired_approval_is_refused_before_send(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock, ttl=60.0)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        clock.advance(10)
        await _approve(service, pending.id)
        clock.advance(55)  # now past the T0+60 expiry
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.APPROVAL_EXPIRED
        assert gateway.calls == []

    async def test_inactive_binding_is_refused_before_send(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        await repositories.agent_bindings.set_status(binding_id, "revoked")
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
        assert gateway.calls == []
        revoked = await repositories.approvals.get_by_id(pending.id)
        assert revoked is not None
        assert revoked.state == ApprovalState.REVOKED

    async def test_cursor_drift_during_wait_is_refused_as_incarnation_changed(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        observation = FakeObservation(_cursor(incarnation=2, seq=7))
        service = _service(repositories, gateway, observation, clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        # Human terminal input advanced the ledger cursor during the wait.
        observation.cursor = _cursor(incarnation=2, seq=8)
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.INCARNATION_CHANGED
        revoked = await repositories.approvals.get_by_id(pending.id)
        assert revoked is not None
        assert revoked.state == ApprovalState.REVOKED
        assert gateway.calls == []

    async def test_pane_replacement_during_wait_is_refused_as_incarnation_changed(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        observation = FakeObservation(_cursor(incarnation=2, seq=7))
        service = _service(repositories, gateway, observation, clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        observation.cursor = _cursor(incarnation=3, seq=1)  # pane replaced
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.INCARNATION_CHANGED
        revoked = await repositories.approvals.get_by_id(pending.id)
        assert revoked is not None
        assert revoked.state == ApprovalState.REVOKED
        assert gateway.calls == []

    async def test_recovery_loop_after_target_drift_succeeds_with_new_tool_call(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        observation = FakeObservation(_cursor(incarnation=2, seq=7))
        service = _service(repositories, gateway, observation, clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        observation.cursor = _cursor(incarnation=2, seq=9)
        with pytest.raises(TermFlowToolError):
            await asyncio.wait_for(task, timeout=5)

        # The documented recovery loop: re-read the pane, then retry with a
        # fresh tool_call_id (a fresh human decision approves again).
        second = asyncio.create_task(
            service.send_text(
                principal,
                _text_params(conversation_id, request_key="req-2"),
                tool_call_id="tool-2",
            )
        )
        pending_two = await _wait_for_approval(repositories, conversation_id, "tool-2")
        await _approve(service, pending_two.id)
        result = await asyncio.wait_for(second, timeout=5)
        assert result.ok is True
        assert result.outcome == "confirmed"
        assert len(gateway.calls) == 1

    async def test_late_ttl_approval_still_matches_frozen_expiry(
        self, repositories, seeded
    ) -> None:
        """The hash recheck uses the stored expires_at, not a fresh now+ttl.

        A human approving in the second half of the TTL window must still
        match: the recheck recomputes with the expiry frozen at creation.
        """
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock, ttl=60.0)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        clock.advance(55)  # approve in the second half of the TTL window
        await _approve(service, pending.id)
        result = await asyncio.wait_for(task, timeout=5)
        assert result.ok is True
        assert result.outcome == "confirmed"
        assert len(gateway.calls) == 1


class TestSettleAndOneTimeUse:
    async def test_known_failure_consumes_approval(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        gateway.raise_error = TermFlowToolError(
            TermFlowErrorCode.PANE_NOT_FOUND, "pane disappeared"
        )
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.PANE_NOT_FOUND
        consumed = await repositories.approvals.get_by_id(pending.id)
        assert consumed is not None
        assert consumed.state == ApprovalState.CONSUMED

    async def test_uncertain_outcome_marks_unknown_and_is_terminal(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        gateway.raise_unknown = True
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        with pytest.raises(TermFlowToolError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value.error_code is TermFlowErrorCode.OUTCOME_UNKNOWN
        unknown = await repositories.approvals.get_by_id(pending.id)
        assert unknown is not None
        assert unknown.state == ApprovalState.UNKNOWN
        # Terminal: neither a decision nor a consume can replay it.
        with pytest.raises(ApprovalAlreadyDecided):
            await _approve(service, pending.id)
        with pytest.raises(ApprovalAlreadyDecided):
            await service.policy.consume(pending.id)

    async def test_in_flight_revoke_race_returns_factual_receipt(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        gateway.stall = asyncio.Event()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        # Let the send start, then revoke while the write is in flight.
        for _ in range(200):
            if gateway.calls:
                break
            await asyncio.sleep(0.005)
        assert gateway.calls, "the send should have started"
        await service.policy.revoke(pending.id, actor="admin")
        gateway.stall.set()
        # The in-flight write completes and the factual receipt is returned;
        # the approval stays revoked (the settle CAS lost).
        result = await asyncio.wait_for(task, timeout=5)
        assert result.ok is True
        assert result.outcome == "confirmed"
        revoked = await repositories.approvals.get_by_id(pending.id)
        assert revoked is not None
        assert revoked.state == ApprovalState.REVOKED

    async def test_one_time_use_no_second_execution(self, repositories, seeded) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        await asyncio.wait_for(task, timeout=5)
        assert len(gateway.calls) == 1
        # A second settle attempt on the same approval can never re-execute.
        with pytest.raises(ApprovalAlreadyConsumed):
            await service.policy.consume(pending.id)
        assert len(gateway.calls) == 1

    async def test_two_approved_writes_to_same_pane_both_execute_once(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        first = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        second = asyncio.create_task(
            service.send_keys(
                principal, _keys_params(conversation_id), tool_call_id="tool-2"
            )
        )
        one = await _wait_for_approval(repositories, conversation_id, "tool-1")
        two = await _wait_for_approval(repositories, conversation_id, "tool-2")
        await _approve(service, one.id)
        await _approve(service, two.id)
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
        assert [result.ok for result in results] == [True, True]
        assert {call[0] for call in gateway.calls} == {"send_text", "send_keys"}
        # Each approval settled exactly once.
        for approval_id in (one.id, two.id):
            settled = await repositories.approvals.get_by_id(approval_id)
            assert settled is not None
            assert settled.state == ApprovalState.CONSUMED


class TestEarlyRejectionAndReplay:
    async def test_unknown_named_key_is_invalid_request_before_approval(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        with pytest.raises(TermFlowToolError) as caught:
            await service.send_keys(
                principal,
                _keys_params(conversation_id, keys=("ctrl-c", "not-a-key")),
                tool_call_id="tool-1",
            )
        assert caught.value.error_code is TermFlowErrorCode.INVALID_REQUEST
        assert await repositories.approvals.get_by_tool_call(conversation_id, "tool-1") is None
        assert gateway.calls == []

    async def test_control_bytes_cannot_be_smuggled_through_keys(
        self, repositories, seeded
    ) -> None:
        # Control bytes are rejected at the protocol model boundary (the
        # shared plain-text validator); they can never reach the service.
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            _keys_params(conversation_id=uuid4(), keys=("\x1b[A",))

    async def test_replayed_tool_call_id_is_approval_conflict(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        first = asyncio.create_task(
            service.send_text(
                principal, _text_params(conversation_id), tool_call_id="tool-1"
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        await asyncio.wait_for(first, timeout=5)
        # Replay with a different payload but the same tool_call_id: the
        # unique (conversation, tool_call_id) gate rejects it.
        with pytest.raises(TermFlowToolError) as caught:
            await service.send_text(
                principal,
                _text_params(conversation_id, text="different text"),
                tool_call_id="tool-1",
            )
        assert caught.value.error_code is TermFlowErrorCode.APPROVAL_CONFLICT
        assert caught.value.data is not None
        assert caught.value.data["approval_id"] == str(pending.id)
        assert caught.value.data["state"] == "consumed"
        assert len(gateway.calls) == 1

    async def test_send_keys_passes_canonical_sequence_to_gateway(
        self, repositories, seeded
    ) -> None:
        binding_id, instance_id, conversation_id = seeded
        clock = FakeClock()
        gateway = FakeGateway()
        service = _service(repositories, gateway, FakeObservation(_cursor()), clock)
        principal = _principal(binding_id, instance_id)
        task = asyncio.create_task(
            service.send_keys(
                principal,
                _keys_params(conversation_id, keys=("ctrl-a", "enter")),
                tool_call_id="tool-1",
            )
        )
        pending = await _wait_for_approval(repositories, conversation_id, "tool-1")
        assert pending is not None
        await _approve(service, pending.id)
        result = await asyncio.wait_for(task, timeout=5)
        assert result.ok is True
        call = gateway.calls[0]
        assert call[0] == "send_keys"
        assert call[3] == ("ctrl-a", "enter")
        assert call[5] == 2  # ledger incarnation


class TestCommandRouterGatewayMapping:
    def test_receipt_error_codes_map_to_stable_tool_codes(self) -> None:
        from termflow_control_plane.errors import TermFlowError

        cases = {
            "pane_not_found": TermFlowErrorCode.PANE_NOT_FOUND,
            "invalid_key": TermFlowErrorCode.INVALID_REQUEST,
            "connection_lost": TermFlowErrorCode.INTERNAL_ERROR,
            "incarnation_changed": TermFlowErrorCode.INTERNAL_ERROR,
        }
        for error_code, expected in cases.items():
            mapped = CommandRouterGateway._map_error(
                TermFlowError(error_code, 409, "the bridge rejected the command")
            )
            assert mapped.error_code is expected, error_code

    def test_unprovable_router_outcomes_become_outcome_unknown(self) -> None:
        from termflow_control_plane.errors import TermFlowError

        for error_code in ("command_timeout", "outcome_unknown"):
            mapped = CommandRouterGateway._map_error(
                TermFlowError(error_code, 504, "no confirmation")
            )
            assert mapped.error_code is TermFlowErrorCode.OUTCOME_UNKNOWN
            assert isinstance(mapped, OutcomeUnknownError)
