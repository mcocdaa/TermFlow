"""Provider verification stays revision-fenced and independent from runtime health."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import select
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import AgentEvent
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.agui_projection import AgentEventProjector
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
from termflow_control_plane.plugins.agent_broker.agent.pipeline import AgentPipelineService
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import AgentStreamHub
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendConversationRef,
    BackendEventScope,
    BackendNotification,
    NotificationPayload,
)
from termflow_protocol.agent import AgentEventKind


@pytest_asyncio.fixture
async def repositories(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'provider-verification.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    try:
        yield bundle
    finally:
        await database.dispose()


class _ProviderBackend:
    runtime_id = "runtime-provider-test"
    directory = "/tmp"


def _capabilities() -> AgentBackendCapabilities:
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


@dataclass(frozen=True, slots=True)
class _ReadyContext:
    binding_id: UUID
    conversation_id: UUID
    fingerprint: str
    pipeline: AgentPipelineService
    run_id: UUID


async def _seed_binding(repositories: RepositoryBundle, *, suffix: str) -> tuple[UUID, str]:
    installation = await repositories.installations.create(
        digest_secret(f"provider-installation-{suffix}")
    )
    term = await repositories.instances.register_or_rotate(
        uuid4(),
        installation.id,
        f"provider-term-{suffix}",
        digest_secret(f"provider-term-{suffix}"),
    )
    profile = await repositories.agent_profiles.create(
        display_name=f"provider-profile-{suffix}",
        backend_kind="opencode",
        config='{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
    )
    binding = await repositories.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="enabled",
        runtime_ref=_ProviderBackend.runtime_id,
        runtime_epoch=1,
        capability_ref="termflow-mcp:provider-test",
    )
    fingerprint = digest_secret(f"provider-disclosure-{suffix}")
    await repositories.agent_provider_disclosures.create(
        binding_id=binding.id,
        disclosure_fingerprint=fingerprint,
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        endpoint_origin="https://api.deepseek.com",
        region="global",
        retention_terms="provider policy",
        retention_version="v1",
        no_training=True,
        policy_version="v1",
        accepted_at=datetime(2026, 9, 7, tzinfo=UTC),
        accepted_auth_epoch=1,
        actor_kind="admin",
        actor_ref="admin:provider-test",
    )
    return binding.id, fingerprint


async def _activate_runtime(
    repositories: RepositoryBundle,
    *,
    suffix: str,
) -> tuple[UUID, str]:
    binding_id, fingerprint = await _seed_binding(repositories, suffix=suffix)
    reconciling = await repositories.agent_runtime_bindings.mark_reconciling(
        binding_id,
        1,
        fingerprint,
    )
    assert reconciling is not None
    assert await repositories.agent_runtime_bindings.compare_and_set_ready(
        binding_id,
        1,
        fingerprint,
        1,
    )
    return binding_id, fingerprint


async def _ready_context(
    repositories: RepositoryBundle,
    *,
    suffix: str,
) -> _ReadyContext:
    binding_id, fingerprint = await _activate_runtime(repositories, suffix=suffix)
    conversation = await repositories.agent_conversations.create(binding_id=binding_id)
    provider_ref = f"provider-session-{suffix}"
    await repositories.agent_backend_conversations.create(
        conversation_id=conversation.id,
        backend_kind="fake",
        backend_version="0.1.0",
        runtime_id=_ProviderBackend.runtime_id,
        binding_capability_epoch=1,
        provider_ref=provider_ref,
    )
    pipeline = AgentPipelineService(
        binding_id=binding_id,
        adapter=_ProviderBackend(),  # type: ignore[arg-type]
        scope=BackendEventScope(binding_id=str(binding_id), runtime_epoch=1),
        capabilities=_capabilities(),
        repositories=repositories,
        sessions=repositories.agent_bindings._sessions,
        hub=AgentStreamHub(),
        supervisor=None,
        runtime_ref=_ProviderBackend.runtime_id,
        runtime_epoch=1,
        applied_config_revision=1,
    )
    run_id = await pipeline.run_machine.create_for_conversation(conversation.id)
    await pipeline.run_machine.start(run_id)
    pipeline._run_config_revisions[run_id] = 1
    return _ReadyContext(
        binding_id=binding_id,
        conversation_id=conversation.id,
        fingerprint=fingerprint,
        pipeline=pipeline,
        run_id=run_id,
    )


def _notification(
    context: _ReadyContext,
    *,
    kind: AgentEventKind,
    text: str | None = None,
    summary: str | None = None,
    tool_call_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> BackendNotification:
    return BackendNotification(
        conversation_ref=BackendConversationRef(
            backend_kind="fake",
            backend_version="0.1.0",
            runtime_id=_ProviderBackend.runtime_id,
            binding_capability_epoch=1,
            provider_ref=f"provider-session-{context.conversation_id.hex[:8]}",
        ),
        scope=BackendEventScope(binding_id=str(context.binding_id), runtime_epoch=1),
        kind=kind,
        run_id=context.run_id,
        message_id=uuid4(),
        tool_call_id=tool_call_id,
        dedup_key=f"provider-verification-{uuid4()}",
        payload=NotificationPayload(
            text=text,
            summary=summary,
            error_code=error_code,
            error_message=error_message,
        ),
    )


async def test_runtime_activation_sets_configured_unverified(repositories) -> None:
    binding_id, _ = await _activate_runtime(repositories, suffix="activation")

    runtime = await repositories.agent_runtime_bindings.get_by_binding(binding_id)

    assert runtime is not None
    assert runtime.readiness == "ready"
    assert runtime.provider_readiness == "configured_unverified"
    assert runtime.provider_verified_revision is None
    assert runtime.provider_reason_code is None


async def test_first_assistant_output_marks_current_revision_verified(repositories) -> None:
    context = await _ready_context(repositories, suffix="assistant-output")
    notification = _notification(
        context,
        kind=AgentEventKind.MESSAGE_DELTA,
        text="provider path works",
    )
    notification = notification.model_copy(
        update={
            "conversation_ref": notification.conversation_ref.model_copy(
                update={"provider_ref": "provider-session-assistant-output"}
            )
        }
    )

    await context.pipeline._handle_notification(notification)

    runtime = await repositories.agent_runtime_bindings.get_by_binding(context.binding_id)
    assert runtime is not None
    assert runtime.readiness == "ready"
    assert runtime.provider_readiness == "verified"
    assert runtime.provider_verified_revision == 1
    assert runtime.provider_reason_code is None


async def test_stale_run_cannot_verify_new_revision(repositories) -> None:
    context = await _ready_context(repositories, suffix="stale-run")
    advanced = await repositories.agent_bindings.update_desired_runtime(
        context.binding_id,
        _ProviderBackend.runtime_id,
        "termflow-mcp:provider-test",
        1,
        False,
    )
    assert advanced is not None and advanced.config_revision == 2
    notification = _notification(
        context,
        kind=AgentEventKind.MESSAGE_COMPLETED,
        text="late output from revision one",
    )
    notification = notification.model_copy(
        update={
            "conversation_ref": notification.conversation_ref.model_copy(
                update={"provider_ref": "provider-session-stale-run"}
            )
        }
    )

    await context.pipeline._handle_notification(notification)

    runtime = await repositories.agent_runtime_bindings.get_by_binding(context.binding_id)
    assert runtime is not None
    assert runtime.readiness == "ready"
    assert runtime.provider_readiness == "configured_unverified"
    assert runtime.provider_verified_revision is None


async def test_provider_auth_failure_sets_safe_failed_reason(repositories) -> None:
    context = await _ready_context(repositories, suffix="auth-failure")
    raw_error = "invalid_api_key credential=provider-secret-detail"
    notification = _notification(
        context,
        kind=AgentEventKind.RUN_FAILED,
        text=raw_error,
        summary=raw_error,
        error_code="provider_http_401",
        error_message=raw_error,
    )
    notification = notification.model_copy(
        update={
            "conversation_ref": notification.conversation_ref.model_copy(
                update={"provider_ref": "provider-session-auth-failure"}
            )
        }
    )

    await context.pipeline._handle_notification(notification)

    runtime = await repositories.agent_runtime_bindings.get_by_binding(context.binding_id)
    assert runtime is not None
    assert runtime.readiness == "ready"
    assert runtime.provider_readiness == "failed"
    assert runtime.provider_verified_revision is None
    assert runtime.provider_reason_code == "provider_auth_failed"
    assert raw_error not in repr(
        (
            runtime.reason_code,
            runtime.provider_reason_code,
            runtime.provider_readiness,
        )
    )


async def test_provider_error_detail_never_reaches_event_or_agui(repositories) -> None:
    context = await _ready_context(repositories, suffix="event-redaction")
    raw_error = "provider-secret-detail-should-never-persist"
    notification = _notification(
        context,
        kind=AgentEventKind.RUN_FAILED,
        text=raw_error,
        summary=raw_error,
        error_code="provider_http_401",
        error_message=raw_error,
    )
    notification = notification.model_copy(
        update={
            "conversation_ref": notification.conversation_ref.model_copy(
                update={"provider_ref": "provider-session-event-redaction"}
            )
        }
    )
    await context.pipeline._handle_notification(notification)

    async with repositories.agent_bindings._sessions() as session:
        event = await session.scalar(
            select(AgentEvent)
            .where(AgentEvent.conversation_id == context.conversation_id)
            .order_by(AgentEvent.database_seq.desc())
        )
    assert event is not None and event.payload is not None
    payload = json.loads(event.payload)
    assert payload["error_code"] == "provider_auth_failed"
    assert payload["error_message"] is None
    assert raw_error not in event.payload
    projected = AgentEventProjector().project(event)
    assert projected and raw_error not in json.dumps(projected)


async def test_tool_error_detail_never_reaches_event_or_agui(repositories) -> None:
    context = await _ready_context(repositories, suffix="tool-error-redaction")
    raw_error = "tool-secret-detail-should-never-persist"
    notification = _notification(
        context,
        kind=AgentEventKind.TOOL_COMPLETED,
        text=raw_error,
        summary=raw_error,
        tool_call_id="tool-call-redacted",
        error_code="raw_tool_failure",
        error_message=raw_error,
    )
    notification = notification.model_copy(
        update={
            "conversation_ref": notification.conversation_ref.model_copy(
                update={"provider_ref": "provider-session-tool-error-redaction"}
            )
        }
    )
    await context.pipeline._handle_notification(notification)

    async with repositories.agent_bindings._sessions() as session:
        event = await session.scalar(
            select(AgentEvent)
            .where(AgentEvent.conversation_id == context.conversation_id)
            .order_by(AgentEvent.database_seq.desc())
        )
    assert event is not None and event.payload is not None
    payload = json.loads(event.payload)
    assert payload["error_code"] == "tool_failed"
    assert payload["error_message"] is None
    assert payload["text"] is None
    assert payload["summary"] is None
    assert raw_error not in event.payload
    projected = AgentEventProjector().project(event)
    assert projected and raw_error not in json.dumps(projected)
    assert "tool_failed" in json.dumps(projected)
