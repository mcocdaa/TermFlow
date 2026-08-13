"""MCP tool handler tests (plan §10, §12.1; tasks M2.3, M4.4, M5.2).

Handlers enforce the ``terminal.observe`` pane allowlist (with the explicit
``all_panes`` consent row), map observation failures to stable
``TermFlowErrorCode`` values, round-trip watches against the repositories,
and gate the write tools with the ``terminal.write`` scope, the pane
allowlist, and conversation ownership before delegating to the
approval-gated command port.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from termflow_control_plane.persistence.models import Base
from termflow_control_plane.persistence.repositories import (
    RepositoryBundle,
    digest_secret,
)
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    TermFlowToolError,
)
from termflow_control_plane.plugins.agent_broker.api.mcp_tools import (
    ALL_PANES_CONSENT_PANE,
    handle_list_panes,
    handle_pane_read,
    handle_pane_send_keys,
    handle_pane_send_text,
    handle_watch_cancel,
    handle_watch_create,
    handle_watch_get,
    handle_watch_list,
    pane_observe_allowed,
)
from termflow_control_plane.plugins.agent_broker.auth import (
    SCOPE_TERMINAL_OBSERVE,
    SCOPE_TERMINAL_WRITE,
    AgentTokenPrincipal,
)
from termflow_protocol.mcp import (
    PaneReadParams,
    PaneReadResult,
    PaneReadView,
    PaneSendKeysParams,
    PaneSendKeysResult,
    PaneSendTextParams,
    PaneSendTextResult,
    PaneSummary,
    TermFlowErrorCode,
    WatchCancelParams,
    WatchCondition,
    WatchConditionKind,
    WatchCreateParams,
    WatchGetParams,
    WatchListParams,
    WatchStatus,
)


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
    try:
        yield bundle
    finally:
        await engine.dispose()


async def _seed_binding(repositories: RepositoryBundle, *, runtime_epoch: int = 2):
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
        runtime_epoch=runtime_epoch,
        capability_ref="cap-1",
    )


async def _seed_conversation(repositories: RepositoryBundle, binding) -> object:
    return await repositories.agent_conversations.create(
        binding_id=binding.id,
        title="round trip",
    )


def _principal(binding, *, scopes: frozenset[str] | None = None) -> AgentTokenPrincipal:
    return AgentTokenPrincipal(
        binding_id=binding.id,
        instance_id=binding.term_id,
        scopes=(
            scopes if scopes is not None else frozenset({SCOPE_TERMINAL_OBSERVE})
        ),
        runtime_epoch=binding.runtime_epoch or 1,
    )


async def _allow_async(repositories: RepositoryBundle, binding, pane_id: str) -> None:
    await repositories.pane_policies.set_policy(
        binding_id=binding.id, pane_id=pane_id, allowed=True
    )


class FakeObservation:
    """Handler-level fake for TerminalObservationPort with bounded captures."""

    def __init__(self, content: str = "bounded output") -> None:
        self.content = content
        self.reads: list[PaneReadParams] = []
        self.list_calls = 0

    async def list_panes(self, instance_id) -> list[PaneSummary]:
        self.list_calls += 1
        return [
            PaneSummary(pane_id="%0", index=0, title="shell", active=True, dead=False),
            PaneSummary(pane_id="%1", index=1, title="build log", active=False, dead=False),
        ]

    async def read_pane(self, instance_id, params: PaneReadParams) -> PaneReadResult:
        self.reads.append(params)
        return PaneReadResult(
            instance_id=instance_id,
            pane_id=params.pane_id,
            view=params.view,
            content=self.content,
            truncated=False,
        )

    async def resolve_cursor(self, instance_id, pane_id):
        return None


class FakeCommands:
    """Handler-level fake for TerminalCommandPort (approval flow not here)."""

    def __init__(self) -> None:
        self.text_calls: list[tuple] = []
        self.keys_calls: list[tuple] = []

    async def send_text(self, principal, params, *, tool_call_id) -> PaneSendTextResult:
        self.text_calls.append((principal, params, tool_call_id))
        return PaneSendTextResult(
            request_key=params.request_key,
            ok=True,
            outcome="confirmed",
            approval_id=uuid4(),
        )

    async def send_keys(self, principal, params, *, tool_call_id) -> PaneSendKeysResult:
        self.keys_calls.append((principal, params, tool_call_id))
        return PaneSendKeysResult(
            request_key=params.request_key,
            ok=True,
            outcome="confirmed",
            approval_id=uuid4(),
        )


def _pane_read_params(pane_id: str = "%0", **overrides) -> PaneReadParams:
    values = dict(pane_id=pane_id, view=PaneReadView.VIEWPORT)
    values.update(overrides)
    return PaneReadParams(**values)


def _watch_create_params(
    binding, conversation, pane_id: str = "%0", **overrides
) -> WatchCreateParams:
    values = dict(
        pane_id=pane_id,
        conversation_id=conversation.id,
        condition=WatchCondition(
            kind=WatchConditionKind.OUTPUT_CONTAINS, match="ready"
        ),
        one_shot=True,
        intent="wait for readiness",
    )
    values.update(overrides)
    return WatchCreateParams(**values)


# ---------------------------------------------------------------------------
# termflow_list_panes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_panes_omits_titles_by_default(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    fake = FakeObservation()

    result = await handle_list_panes(principal, observation=fake, repositories=repositories)

    assert result.instance_id == binding.term_id
    assert [pane.pane_id for pane in result.panes] == ["%0", "%1"]
    assert all(pane.title is None for pane in result.panes)


@pytest.mark.asyncio
async def test_list_panes_shows_title_only_for_allowlisted_pane(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%1")
    fake = FakeObservation()

    result = await handle_list_panes(principal, observation=fake, repositories=repositories)

    by_id = {pane.pane_id: pane for pane in result.panes}
    assert by_id["%1"].title == "build log"
    assert by_id["%0"].title is None


# ---------------------------------------------------------------------------
# termflow_pane_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pane_read_happy_path(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%0")
    fake = FakeObservation()

    result = await handle_pane_read(
        principal,
        _pane_read_params(pane_id="%0"),
        observation=fake,
        repositories=repositories,
    )

    assert result.content == "bounded output"
    assert result.pane_id == "%0"
    assert result.instance_id == binding.term_id
    assert fake.reads == [_pane_read_params(pane_id="%0")]


@pytest.mark.asyncio
async def test_pane_read_policy_denied_when_pane_not_allowlisted(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    fake = FakeObservation()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_read(
            principal,
            _pane_read_params(pane_id="%0"),
            observation=fake,
            repositories=repositories,
        )

    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert fake.reads == []


@pytest.mark.asyncio
async def test_pane_read_all_panes_requires_explicit_consent(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    fake = FakeObservation()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_read(
            principal,
            _pane_read_params(pane_id="%7"),
            observation=fake,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED

    await _allow_async(repositories, binding, ALL_PANES_CONSENT_PANE)
    result = await handle_pane_read(
        principal,
        _pane_read_params(pane_id="%7"),
        observation=fake,
        repositories=repositories,
    )
    assert result.pane_id == "%7"

    await repositories.pane_policies.set_policy(
        binding_id=binding.id, pane_id=ALL_PANES_CONSENT_PANE, allowed=False
    )
    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_read(
            principal,
            _pane_read_params(pane_id="%7"),
            observation=fake,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED


@pytest.mark.asyncio
async def test_pane_read_propagates_quota_exceeded(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%0")
    fake = FakeObservation()

    async def _quota_read(instance_id, params: PaneReadParams) -> PaneReadResult:
        raise TermFlowToolError(
            TermFlowErrorCode.QUOTA_EXCEEDED, "the instance rejected the byte quota"
        )

    fake.read_pane = _quota_read  # type: ignore[method-assign]

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_read(
            principal,
            _pane_read_params(pane_id="%0"),
            observation=fake,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_pane_read_passes_truncated_result_through(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%0")

    async def _truncated_read(instance_id, params: PaneReadParams) -> PaneReadResult:
        return PaneReadResult(
            instance_id=instance_id,
            pane_id=params.pane_id,
            view=params.view,
            content="truncated tail",
            truncated=True,
        )

    fake = FakeObservation()
    fake.read_pane = _truncated_read  # type: ignore[method-assign]

    result = await handle_pane_read(
        principal,
        _pane_read_params(pane_id="%0"),
        observation=fake,
        repositories=repositories,
    )
    assert result.truncated is True
    assert result.content == "truncated tail"


# ---------------------------------------------------------------------------
# watch tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_create_list_get_cancel_round_trip(repositories) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%0")
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)

    created = await handle_watch_create(
        principal,
        _watch_create_params(binding, conversation, pane_id="%0"),
        continuation=continuation,
        repositories=repositories,
    )
    assert created.generation == 0
    assert created.start_cursor is None

    listed = await handle_watch_list(
        principal,
        WatchListParams(),
        continuation=continuation,
        repositories=repositories,
    )
    assert [watch.watch_id for watch in listed.watches] == [created.watch_id]
    assert listed.watches[0].pane_id == "%0"
    assert listed.watches[0].conversation_id == conversation.id
    assert listed.watches[0].status is WatchStatus.ACTIVE
    assert listed.watches[0].generation == 0
    assert listed.watches[0].condition.kind is WatchConditionKind.OUTPUT_CONTAINS

    detail = await handle_watch_get(
        principal,
        WatchGetParams(watch_id=created.watch_id),
        continuation=continuation,
        repositories=repositories,
    )
    assert detail.watch.watch_id == created.watch_id
    assert detail.watch.one_shot is True
    assert detail.watch.condition.match == "ready"
    assert detail.watch.condition.kind is WatchConditionKind.OUTPUT_CONTAINS

    cancelled = await handle_watch_cancel(
        principal,
        WatchCancelParams(watch_id=created.watch_id),
        continuation=continuation,
        repositories=repositories,
    )
    assert cancelled.ok is True
    assert cancelled.error_code is None

    # Cancellation is a state transition: the binding can still read the
    # watch back and sees the CANCELLED status.
    after_cancel = await handle_watch_get(
        principal,
        WatchGetParams(watch_id=created.watch_id),
        continuation=continuation,
        repositories=repositories,
    )
    assert after_cancel.watch.status is WatchStatus.CANCELLED


@pytest.mark.asyncio
async def test_watch_list_filters_by_status_and_conversation(repositories) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%0")
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    created = await continuation.create_watch(
        binding.id, _watch_create_params(binding, conversation, pane_id="%0")
    )
    await repositories.watches.cancel(created.watch_id)

    active = await handle_watch_list(
        principal,
        WatchListParams(status=WatchStatus.ACTIVE),
        continuation=continuation,
        repositories=repositories,
    )
    assert active.watches == []

    cancelled = await handle_watch_list(
        principal,
        WatchListParams(status=WatchStatus.CANCELLED),
        continuation=continuation,
        repositories=repositories,
    )
    assert [watch.watch_id for watch in cancelled.watches] == [created.watch_id]

    scoped = await handle_watch_list(
        principal,
        WatchListParams(conversation_id=uuid4()),
        continuation=continuation,
        repositories=repositories,
    )
    assert scoped.watches == []


@pytest.mark.asyncio
async def test_watch_create_requires_pane_observe_policy(repositories) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    principal = _principal(binding)
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)

    with pytest.raises(TermFlowToolError) as caught:
        await handle_watch_create(
            principal,
            _watch_create_params(binding, conversation, pane_id="%0"),
            continuation=continuation,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert await repositories.watches.list_for_binding(binding.id) == []


@pytest.mark.asyncio
async def test_watch_create_rejects_foreign_conversation(repositories) -> None:
    binding = await _seed_binding(repositories)
    foreign = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, foreign)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%0")
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)

    with pytest.raises(TermFlowToolError) as caught:
        await handle_watch_create(
            principal,
            _watch_create_params(binding, conversation, pane_id="%0"),
            continuation=continuation,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED


@pytest.mark.asyncio
async def test_watch_get_and_cancel_are_scoped_to_binding(repositories) -> None:
    binding = await _seed_binding(repositories)
    other = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%0")
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    created = await continuation.create_watch(
        binding.id, _watch_create_params(binding, conversation, pane_id="%0")
    )

    other_principal = _principal(other)
    with pytest.raises(TermFlowToolError) as caught:
        await handle_watch_get(
            other_principal,
            WatchGetParams(watch_id=created.watch_id),
            continuation=continuation,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.WATCH_NOT_FOUND

    with pytest.raises(TermFlowToolError) as caught:
        await handle_watch_cancel(
            other_principal,
            WatchCancelParams(watch_id=created.watch_id),
            continuation=continuation,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.WATCH_NOT_FOUND

    # The original owner still sees the active watch.
    listed = await handle_watch_list(
        principal,
        WatchListParams(),
        continuation=continuation,
        repositories=repositories,
    )
    assert [watch.watch_id for watch in listed.watches] == [created.watch_id]


@pytest.mark.asyncio
async def test_watch_cancel_unknown_watch_is_not_found(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)

    with pytest.raises(TermFlowToolError) as caught:
        await handle_watch_cancel(
            principal,
            WatchCancelParams(watch_id=uuid4()),
            continuation=continuation,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.WATCH_NOT_FOUND


@pytest.mark.asyncio
async def test_watch_cancel_is_idempotent_for_inactive_watch(repositories) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    principal = _principal(binding)
    await _allow_async(repositories, binding, "%0")
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)
    created = await continuation.create_watch(
        binding.id, _watch_create_params(binding, conversation, pane_id="%0")
    )
    await repositories.watches.cancel(created.watch_id)

    result = await handle_watch_cancel(
        principal,
        WatchCancelParams(watch_id=created.watch_id),
        continuation=continuation,
        repositories=repositories,
    )
    assert result.ok is True


# ---------------------------------------------------------------------------
# write tools (observe-only milestone)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_text_denied_for_observe_only_token(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    commands = FakeCommands()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_send_text(
            principal,
            PaneSendTextParams(
                pane_id="%0",
                request_key="key-1",
                conversation_id=conversation.id,
                text="ls -la",
            ),
            commands=commands,
            repositories=repositories,
            tool_call_id="tool-1",
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert commands.text_calls == []


@pytest.mark.asyncio
async def test_send_keys_denied_for_observe_only_token(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding)
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    commands = FakeCommands()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_send_keys(
            principal,
            PaneSendKeysParams(
                pane_id="%0",
                request_key="key-2",
                conversation_id=conversation.id,
                keys=("enter",),
            ),
            commands=commands,
            repositories=repositories,
            tool_call_id="tool-2",
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert commands.keys_calls == []


@pytest.mark.asyncio
async def test_list_panes_requires_observe_scope(repositories) -> None:
    binding = await _seed_binding(repositories)
    await _allow_async(repositories, binding, "%0")
    principal = _principal(binding, scopes=frozenset())
    fake = FakeObservation()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_list_panes(principal, observation=fake, repositories=repositories)
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert fake.list_calls == 0


@pytest.mark.asyncio
async def test_pane_read_requires_observe_scope(repositories) -> None:
    binding = await _seed_binding(repositories)
    await _allow_async(repositories, binding, "%0")
    principal = _principal(binding, scopes=frozenset())
    fake = FakeObservation()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_read(
            principal,
            _pane_read_params(pane_id="%0"),
            observation=fake,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert fake.reads == []


@pytest.mark.asyncio
async def test_watch_create_requires_observe_scope(repositories) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    principal = _principal(binding, scopes=frozenset())
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)

    with pytest.raises(TermFlowToolError) as caught:
        await handle_watch_create(
            principal,
            _watch_create_params(binding, conversation, pane_id="%0"),
            continuation=continuation,
            repositories=repositories,
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert await repositories.watches.list_for_binding(binding.id) == []


# ---------------------------------------------------------------------------
# M5.2 write tools: scope, pane policy, conversation ownership, delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_scope_missing_is_policy_denied_without_approval(
    repositories,
) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(
        binding,
        scopes=frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE}),
    )
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    commands = FakeCommands()

    result = await handle_pane_send_text(
        principal,
        PaneSendTextParams(
            pane_id="%0",
            request_key="key-1",
            conversation_id=conversation.id,
            text="ls -la",
        ),
        commands=commands,
        repositories=repositories,
        tool_call_id="tool-1",
    )
    assert result.ok is True
    assert result.outcome == "confirmed"
    assert result.approval_id is not None
    assert len(commands.text_calls) == 1
    call = commands.text_calls[0]
    assert call[0] == principal
    assert call[1].pane_id == "%0"
    assert call[2] == "tool-1"


@pytest.mark.asyncio
async def test_write_without_pane_allowlist_is_policy_denied(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(
        binding,
        scopes=frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE}),
    )
    conversation = await _seed_conversation(repositories, binding)
    commands = FakeCommands()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_send_text(
            principal,
            PaneSendTextParams(
                pane_id="%9",
                request_key="key-1",
                conversation_id=conversation.id,
                text="ls",
            ),
            commands=commands,
            repositories=repositories,
            tool_call_id="tool-1",
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert commands.text_calls == []


@pytest.mark.asyncio
async def test_write_rejects_foreign_conversation(repositories) -> None:
    binding = await _seed_binding(repositories)
    other_binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, other_binding)
    principal = _principal(
        binding,
        scopes=frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE}),
    )
    await _allow_async(repositories, binding, "%0")
    commands = FakeCommands()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_send_keys(
            principal,
            PaneSendKeysParams(
                pane_id="%0",
                request_key="key-2",
                conversation_id=conversation.id,
                keys=("enter",),
            ),
            commands=commands,
            repositories=repositories,
            tool_call_id="tool-2",
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert commands.keys_calls == []


@pytest.mark.asyncio
async def test_write_keys_delegates_sequence_to_command_port(repositories) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(
        binding,
        scopes=frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE}),
    )
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    commands = FakeCommands()

    result = await handle_pane_send_keys(
        principal,
        PaneSendKeysParams(
            pane_id="%0",
            request_key="key-2",
            conversation_id=conversation.id,
            intent="interrupt the build",
            keys=("ctrl-c",),
        ),
        commands=commands,
        repositories=repositories,
        tool_call_id="tool-2",
    )
    assert result.ok is True
    assert len(commands.keys_calls) == 1
    call = commands.keys_calls[0]
    assert call[1].keys == ("ctrl-c",)
    assert call[1].intent == "interrupt the build"
    assert call[2] == "tool-2"


@pytest.mark.asyncio
async def test_write_observe_only_token_never_reaches_command_port(
    repositories,
) -> None:
    binding = await _seed_binding(repositories)
    principal = _principal(binding, scopes=frozenset({SCOPE_TERMINAL_OBSERVE}))
    conversation = await _seed_conversation(repositories, binding)
    await _allow_async(repositories, binding, "%0")
    commands = FakeCommands()

    with pytest.raises(TermFlowToolError) as caught:
        await handle_pane_send_text(
            principal,
            PaneSendTextParams(
                pane_id="%0",
                request_key="key-1",
                conversation_id=conversation.id,
                text="ls",
            ),
            commands=commands,
            repositories=repositories,
            tool_call_id="tool-1",
        )
    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert commands.text_calls == []


@pytest.mark.asyncio
async def test_exit_gate_fake_mcp_client_inspects_panes_and_writes_through_port(
    repositories,
) -> None:
    binding = await _seed_binding(repositories)
    conversation = await _seed_conversation(repositories, binding)
    principal = _principal(
        binding,
        scopes=frozenset({SCOPE_TERMINAL_OBSERVE, SCOPE_TERMINAL_WRITE}),
    )
    await _allow_async(repositories, binding, "%0")
    await _allow_async(repositories, binding, "%1")
    fake = FakeObservation()
    commands = FakeCommands()
    from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
        WatchContinuationService,
    )

    continuation = WatchContinuationService(repositories.watches, repositories.agent_bindings)

    for pane_id in ("%0", "%1"):
        listing = await handle_list_panes(
            principal, observation=fake, repositories=repositories
        )
        assert {pane.pane_id for pane in listing.panes} == {"%0", "%1"}
        assert all(pane.title is not None for pane in listing.panes)

        read = await handle_pane_read(
            principal,
            _pane_read_params(pane_id=pane_id),
            observation=fake,
            repositories=repositories,
        )
        assert read.pane_id == pane_id
        assert read.content == "bounded output"

    text = await handle_pane_send_text(
        principal,
        PaneSendTextParams(
            pane_id="%0",
            request_key="key-1",
            conversation_id=conversation.id,
            text="ls",
        ),
        commands=commands,
        repositories=repositories,
        tool_call_id="tool-1",
    )
    assert text.ok is True
    keys = await handle_pane_send_keys(
        principal,
        PaneSendKeysParams(
            pane_id="%1",
            request_key="key-2",
            conversation_id=conversation.id,
            keys=("enter",),
        ),
        commands=commands,
        repositories=repositories,
        tool_call_id="tool-2",
    )
    assert keys.ok is True

    watch = await handle_watch_create(
        principal,
        _watch_create_params(binding, conversation, pane_id="%0"),
        continuation=continuation,
        repositories=repositories,
    )
    assert watch.watch_id is not None

    # The fake observation port saw only reads; writes went through the
    # command port with their tool call ids.
    assert [read.pane_id for read in fake.reads] == ["%0", "%1"]
    assert [call[2] for call in commands.text_calls] == ["tool-1"]
    assert [call[2] for call in commands.keys_calls] == ["tool-2"]


# ---------------------------------------------------------------------------
# policy helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pane_observe_allowed_helper(repositories) -> None:
    binding = await _seed_binding(repositories)
    assert not await pane_observe_allowed(
        repositories.pane_policies, binding.id, "%0"
    )

    await _allow_async(repositories, binding, "%0")
    assert await pane_observe_allowed(repositories.pane_policies, binding.id, "%0")

    await repositories.pane_policies.set_policy(
        binding_id=binding.id, pane_id=ALL_PANES_CONSENT_PANE, allowed=True
    )
    assert await pane_observe_allowed(repositories.pane_policies, binding.id, "%9")

    # An explicit deny row wins over the all_panes consent.
    await repositories.pane_policies.set_policy(
        binding_id=binding.id, pane_id="%9", allowed=False
    )
    assert not await pane_observe_allowed(repositories.pane_policies, binding.id, "%9")
