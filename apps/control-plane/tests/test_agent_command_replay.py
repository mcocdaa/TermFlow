"""CommandService request-key replay contract (spec §6).

The approval replay gate is pinned on the model-supplied ``request_key``
(carried as ``tool_call_id``), never the MCP JSON-RPC request id: clients
recycle request ids across connections, and a recycled id used to reject a
legitimate new write as "replay".  These tests lock the idempotent retry
semantics:

- identical arguments observe the original state (receipt, pending wait, or
  decision failure) and never create a second approval;
- changed arguments under the same key are a hard ``approval_conflict``;
- a settled write is never re-executed by a retry.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from termflow_control_plane.auth.epoch import persisted_authentication_epoch
from termflow_control_plane.persistence.models import (
    ApprovalRequest,
    AuthenticationState,
    Base,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.approval_audit import (
    ApprovalAuditWriter,
)
from termflow_control_plane.plugins.agent_broker.agent.command_service import (
    CommandService,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import (
    ApprovalArgsHashInput,
    canonical_hash,
)
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    TermFlowToolError,
)
from termflow_control_plane.plugins.agent_broker.auth import (
    SCOPE_TERMINAL_OBSERVE,
    SCOPE_TERMINAL_WRITE,
    AgentTokenPrincipal,
)
from termflow_protocol.keys import canonical_key_bytes
from termflow_protocol.mcp import (
    PaneSendKeysParams,
    PaneSendTextParams,
    TermFlowErrorCode,
)


@pytest_asyncio.fixture
async def stack(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        # Migration 0002 seeds this singleton; create_all does not.
        session.add(AuthenticationState(id=1, epoch=1))
        await session.commit()
    try:
        yield RepositoryBundle(factory), factory
    finally:
        await engine.dispose()


async def _seed_binding(repositories: RepositoryBundle):
    profile = await repositories.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    display_name = f"term-{uuid4().hex[:8]}"
    installation = await repositories.installations.create(
        digest_secret(f"computer-{uuid4().hex}")
    )
    term = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        display_name,
        digest_secret(display_name),
    )
    return await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="ready",
        runtime_ref="runtime-1",
        runtime_epoch=2,
        capability_ref="cap-1",
    )


def _principal(binding) -> AgentTokenPrincipal:
    return AgentTokenPrincipal(
        binding_id=binding.id,
        instance_id=binding.term_id,
        scopes=frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE}),
        runtime_epoch=binding.runtime_epoch or 1,
    )


class _NullObservation:
    """No live pane cursor: the reviewed incarnation falls back to '1'."""

    async def resolve_cursor(self, instance_id: UUID, pane_id: str):
        return None


class _FailingGateway:
    """The replay paths must never reach the A-side gateway."""

    async def send_text(self, *args, **kwargs):
        raise AssertionError("gateway must not be reached")

    async def send_keys(self, *args, **kwargs):
        raise AssertionError("gateway must not be reached")


def _commands(repositories: RepositoryBundle, factory, *, wait: float = 0.05) -> CommandService:
    return CommandService(
        repositories=repositories,
        sessions=factory,
        gateway=_FailingGateway(),
        observation=_NullObservation(),
        approval_wait_timeout_seconds=wait,
        approval_ttl_seconds=60.0,
        poll_interval_seconds=0.01,
        audit=ApprovalAuditWriter(repositories),
    )


def _reviewed_hash(
    principal: AgentTokenPrincipal,
    params: PaneSendTextParams | PaneSendKeysParams,
    operation: str,
    *,
    expiry: datetime,
    policy_epoch: int,
) -> str:
    if operation == "send_text":
        assert isinstance(params, PaneSendTextParams)
        encoded, submit = params.text.encode("utf-8"), params.submit
    else:
        assert isinstance(params, PaneSendKeysParams)
        encoded, submit = canonical_key_bytes(params.keys), False
    return canonical_hash(
        ApprovalArgsHashInput(
            schema_version=1,
            operation=operation,
            instance_id=principal.instance_id,
            pane_id=params.pane_id,
            pane_incarnation="1",
            encoded_bytes=encoded,
            submit=submit,
            cursor_precondition=None,
            run_id=None,
            grant_id=None,
            expiry=expiry,
            policy_epoch=policy_epoch,
        )
    )


async def _seed_approval(
    repositories: RepositoryBundle,
    binding,
    principal: AgentTokenPrincipal,
    params: PaneSendTextParams | PaneSendKeysParams,
    operation: str,
    *,
    state: str = "pending",
):
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    epoch = await persisted_authentication_epoch(repositories)
    return await repositories.approvals.create(
        binding_id=binding.id,
        conversation_id=params.conversation_id,
        tool_call_id=params.request_key,
        canonical_hash=_reviewed_hash(
            principal, params, operation, expiry=expiry, policy_epoch=epoch
        ),
        auth_epoch=epoch,
        expires_at=expiry,
        state=state,
        pane_id=params.pane_id,
        operation=operation,
    )


async def _seed_outcome(
    repositories: RepositoryBundle,
    binding,
    approval,
    *,
    event_type: str,
    outcome: str,
    error_code: str | None = None,
) -> None:
    await repositories.approval_audit.create(
        event_type=event_type,
        approval_id=approval.id,
        binding_id=binding.id,
        instance_id=binding.term_id,
        runtime_epoch=binding.runtime_epoch,
        conversation_id=approval.conversation_id,
        run_id=None,
        tool_call_id=approval.tool_call_id,
        pane_id=approval.pane_id,
        operation=approval.operation,
        input_bytes=None,
        canonical_hash=approval.canonical_hash,
        auth_epoch=approval.auth_epoch,
        actor="test:replay",
        outcome=outcome,
        error_code=error_code,
    )


async def _approval_count(factory, conversation_id: UUID) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(ApprovalRequest.conversation_id == conversation_id)
            )
            or 0
        )


def _text_params(conversation_id: UUID, *, request_key: str, text: str = "make test"):
    return PaneSendTextParams(
        pane_id="%0",
        request_key=request_key,
        conversation_id=conversation_id,
        intent="run the tests",
        text=text,
        submit=True,
    )


async def _cancel_late_completions(commands: CommandService) -> None:
    tasks = list(commands._late_completions)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_pending_retry_observes_the_same_approval(stack) -> None:
    repositories, factory = stack
    binding = await _seed_binding(repositories)
    conversation = await repositories.agent_conversations.create(
        binding_id=binding.id, title="replay"
    )
    principal = _principal(binding)
    params = _text_params(conversation.id, request_key="req-1")
    approval = await _seed_approval(repositories, binding, principal, params, "send_text")
    commands = _commands(repositories, factory)

    with pytest.raises(TermFlowToolError) as excinfo:
        await commands.send_text(principal, params, tool_call_id="req-1")

    assert excinfo.value.error_code is TermFlowErrorCode.APPROVAL_REQUIRED
    assert excinfo.value.data["approval_id"] == str(approval.id)
    assert await _approval_count(factory, conversation.id) == 1


async def test_changed_arguments_under_the_same_key_conflict(stack) -> None:
    repositories, factory = stack
    binding = await _seed_binding(repositories)
    conversation = await repositories.agent_conversations.create(
        binding_id=binding.id, title="replay"
    )
    principal = _principal(binding)
    seeded = _text_params(conversation.id, request_key="req-1", text="make test")
    approval = await _seed_approval(
        repositories, binding, principal, seeded, "send_text"
    )
    commands = _commands(repositories, factory)
    changed = _text_params(conversation.id, request_key="req-1", text="make lint")

    with pytest.raises(TermFlowToolError) as excinfo:
        await commands.send_text(principal, changed, tool_call_id="req-1")

    assert excinfo.value.error_code is TermFlowErrorCode.APPROVAL_CONFLICT
    assert excinfo.value.data["approval_id"] == str(approval.id)
    stored = await repositories.approvals.get_by_tool_call(conversation.id, "req-1")
    assert stored is not None and stored.canonical_hash == approval.canonical_hash
    assert await _approval_count(factory, conversation.id) == 1


async def test_consumed_retry_returns_the_original_receipt(stack) -> None:
    repositories, factory = stack
    binding = await _seed_binding(repositories)
    conversation = await repositories.agent_conversations.create(
        binding_id=binding.id, title="replay"
    )
    principal = _principal(binding)
    params = _text_params(conversation.id, request_key="req-1")
    approval = await _seed_approval(
        repositories, binding, principal, params, "send_text", state="consumed"
    )
    await _seed_outcome(
        repositories, binding, approval, event_type="consumed", outcome="confirmed"
    )
    commands = _commands(repositories, factory)

    result = await commands.send_text(principal, params, tool_call_id="req-1")

    assert result.ok is True
    assert result.outcome == "confirmed"
    assert result.request_key == "req-1"
    assert result.approval_id == approval.id
    assert await _approval_count(factory, conversation.id) == 1


async def test_consumed_failed_retry_replays_the_recorded_error(stack) -> None:
    repositories, factory = stack
    binding = await _seed_binding(repositories)
    conversation = await repositories.agent_conversations.create(
        binding_id=binding.id, title="replay"
    )
    principal = _principal(binding)
    params = _text_params(conversation.id, request_key="req-1")
    approval = await _seed_approval(
        repositories, binding, principal, params, "send_text", state="consumed"
    )
    await _seed_outcome(
        repositories,
        binding,
        approval,
        event_type="consumed",
        outcome="failed",
        error_code="pane_not_found",
    )
    commands = _commands(repositories, factory)

    with pytest.raises(TermFlowToolError) as excinfo:
        await commands.send_text(principal, params, tool_call_id="req-1")

    assert excinfo.value.error_code is TermFlowErrorCode.PANE_NOT_FOUND
    assert excinfo.value.data["approval_id"] == str(approval.id)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("denied", TermFlowErrorCode.APPROVAL_DENIED),
        ("revoked", TermFlowErrorCode.APPROVAL_REVOKED),
        ("expired", TermFlowErrorCode.APPROVAL_EXPIRED),
    ],
)
async def test_decided_retry_replays_the_decision_failure(stack, state, expected) -> None:
    repositories, factory = stack
    binding = await _seed_binding(repositories)
    conversation = await repositories.agent_conversations.create(
        binding_id=binding.id, title="replay"
    )
    principal = _principal(binding)
    params = _text_params(conversation.id, request_key="req-1")
    approval = await _seed_approval(
        repositories, binding, principal, params, "send_text", state=state
    )
    commands = _commands(repositories, factory)

    with pytest.raises(TermFlowToolError) as excinfo:
        await commands.send_text(principal, params, tool_call_id="req-1")

    assert excinfo.value.error_code is expected
    assert excinfo.value.data["approval_id"] == str(approval.id)
    assert await _approval_count(factory, conversation.id) == 1


async def test_new_request_key_is_not_blocked_by_a_settled_neighbour(stack) -> None:
    """The original bug: a recycled id must not reject a legitimate new write."""
    repositories, factory = stack
    binding = await _seed_binding(repositories)
    conversation = await repositories.agent_conversations.create(
        binding_id=binding.id, title="replay"
    )
    principal = _principal(binding)
    settled = _text_params(conversation.id, request_key="req-old")
    old = await _seed_approval(
        repositories, binding, principal, settled, "send_text", state="consumed"
    )
    await _seed_outcome(
        repositories, binding, old, event_type="consumed", outcome="confirmed"
    )
    commands = _commands(repositories, factory)
    fresh = _text_params(conversation.id, request_key="req-new", text="make lint")

    try:
        with pytest.raises(TermFlowToolError) as excinfo:
            await commands.send_text(principal, fresh, tool_call_id="req-new")

        assert excinfo.value.error_code is TermFlowErrorCode.APPROVAL_REQUIRED
        created = await repositories.approvals.get_by_tool_call(conversation.id, "req-new")
        assert created is not None
        assert created.state == "pending"
        assert created.canonical_hash != old.canonical_hash
        assert await _approval_count(factory, conversation.id) == 2
    finally:
        await _cancel_late_completions(commands)


async def test_request_key_scope_is_one_conversation(stack) -> None:
    repositories, factory = stack
    binding = await _seed_binding(repositories)
    first = await repositories.agent_conversations.create(
        binding_id=binding.id, title="first"
    )
    second = await repositories.agent_conversations.create(
        binding_id=binding.id, title="second"
    )
    principal = _principal(binding)
    params = _text_params(first.id, request_key="req-1")
    approval = await _seed_approval(
        repositories, binding, principal, params, "send_text", state="consumed"
    )
    await _seed_outcome(
        repositories, binding, approval, event_type="consumed", outcome="confirmed"
    )
    commands = _commands(repositories, factory)

    result = await commands.send_text(principal, params, tool_call_id="req-1")
    assert result.ok is True

    other = _text_params(second.id, request_key="req-1")
    try:
        with pytest.raises(TermFlowToolError) as excinfo:
            await commands.send_text(principal, other, tool_call_id="req-1")
        assert excinfo.value.error_code is TermFlowErrorCode.APPROVAL_REQUIRED
        created = await repositories.approvals.get_by_tool_call(second.id, "req-1")
        assert created is not None and created.state == "pending"
    finally:
        await _cancel_late_completions(commands)


async def test_send_keys_retry_matches_its_own_reviewed_scope(stack) -> None:
    repositories, factory = stack
    binding = await _seed_binding(repositories)
    conversation = await repositories.agent_conversations.create(
        binding_id=binding.id, title="replay"
    )
    principal = _principal(binding)
    params = PaneSendKeysParams(
        pane_id="%0",
        request_key="keys-1",
        conversation_id=conversation.id,
        intent="interrupt",
        keys=("ctrl-c",),
    )
    approval = await _seed_approval(
        repositories, binding, principal, params, "send_keys", state="consumed"
    )
    await _seed_outcome(
        repositories, binding, approval, event_type="consumed", outcome="confirmed"
    )
    commands = _commands(repositories, factory)

    result = await commands.send_keys(principal, params, tool_call_id="keys-1")

    assert result.ok is True
    assert result.outcome == "confirmed"
    assert result.approval_id == approval.id

    with pytest.raises(TermFlowToolError) as excinfo:
        changed = PaneSendKeysParams(
            pane_id="%0",
            request_key="keys-1",
            conversation_id=conversation.id,
            keys=("ctrl-d",),
        )
        await commands.send_keys(principal, changed, tool_call_id="keys-1")

    assert excinfo.value.error_code is TermFlowErrorCode.APPROVAL_CONFLICT
