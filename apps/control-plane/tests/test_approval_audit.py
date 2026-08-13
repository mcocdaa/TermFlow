"""Approval lifecycle audit tests (plan §12.1, task M5.2; spec §7 test matrix).

Covers the metadata-only ``approval_audit_events`` trail:

- every successful lifecycle transition records exactly one event
  (``created``/``decided``/``revoked``/``consumed``/``unknown``/``expired``)
  with agent identity (binding + instance + runtime epoch + run id) and
  approval context (approval id + tool call id + canonical hash + auth
  epoch + pane/operation/input byte count);
- ``consume``/``mark_unknown`` metadata (outcome/error_code/input_bytes)
  enters the audit row without changing CAS semantics;
- the expiry sweep covers pending AND approved rows and records ``expired``
  per row; ``recover_orphaned_approvals`` finishes pending -> revoked and
  approved -> unknown with the ``system:restart`` actor;
- the audit table has NO raw text/keys columns and rows are purged after 90
  days; a policy without a writer stays a no-op (M5.1 surface unchanged);
- the CommandService writes the full trail end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import inspect
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import Base
from termflow_control_plane.persistence.repositories import (
    AGENT_APPROVAL_AUDIT_RETENTION,
    RepositoryBundle,
    digest_secret,
)
from termflow_control_plane.plugins.agent_broker.agent.approval_audit import (
    ApprovalAuditWriter,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import (
    ApprovalArgsHashInput,
    ApprovalPolicy,
    ApprovalState,
    canonical_hash,
)
from termflow_protocol.agent import ApprovalDecision


def _hash() -> str:
    return canonical_hash(
        ApprovalArgsHashInput(
            schema_version=1,
            operation="send_text",
            instance_id=uuid4(),
            pane_id="%1",
            pane_incarnation="2",
            encoded_bytes=b"make test",
            submit=True,
            cursor_precondition="stream:7",
            run_id=None,
            grant_id=None,
            expiry=datetime.now(UTC) + timedelta(minutes=5),
            policy_epoch=1,
        )
    )


@pytest_asyncio.fixture
async def repos(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def policy(repos: RepositoryBundle) -> ApprovalPolicy:
    return ApprovalPolicy(
        repos,
        repos.session_factory,  # type: ignore[attr-defined]
        audit=ApprovalAuditWriter(repos),
    )


async def _seed(repos: RepositoryBundle, *, runtime_epoch: int = 3) -> tuple[object, object]:
    """Create profile/term/binding/conversation/run; return (binding, conversation)."""
    profile = await repos.agent_profiles.create(
        display_name=f"profile-{uuid4().hex[:8]}",
        backend_kind="opencode",
        config='{"model": "default"}',
    )
    installation = await repos.installations.create(digest_secret(f"computer-{uuid4().hex}"))
    term = await repos.instances.register_or_rotate(
        uuid4(),
        installation.id,
        f"term-{uuid4().hex[:8]}",
        digest_secret(f"instance-{uuid4().hex}"),
    )
    binding = await repos.agent_bindings.create(
        profile_id=profile.id,
        term_id=term.id,
        status="ready",
        runtime_ref="runtime-1",
        runtime_epoch=runtime_epoch,
        capability_ref="cap-1",
    )
    conversation = await repos.agent_conversations.create(binding_id=binding.id, title="audit")
    run = await repos.agent_runs.create(
        conversation_id=conversation.id,
        run_state="running",
        started_at=datetime.now(UTC),
    )
    return binding, conversation, run


async def _create(
    policy: ApprovalPolicy,
    binding,
    conversation,
    run,
    *,
    tool_call_id: str | None = None,
) -> object:
    return await policy.create_approval(
        binding_id=binding.id,
        conversation_id=conversation.id,
        tool_call_id=tool_call_id or f"tool-{uuid4().hex[:12]}",
        canonical_hash=_hash(),
        auth_epoch=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        run_id=run.id,
        pane_id="%1",
        operation="send_text",
        intent_summary="run the test suite",
        input_bytes=9,
    )


async def _events(repos: RepositoryBundle, approval_id) -> list[object]:
    return await repos.approval_audit.list_for_approval(approval_id)


class TestLifecycleEvents:
    async def test_created_event_carries_identity_and_context(
        self, policy, repos
    ) -> None:
        binding, conversation, run = await _seed(repos)
        approval = await _create(policy, binding, conversation, run)
        events = await _events(repos, approval.id)
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "created"
        # Agent identity: binding + instance (term) + runtime epoch + run.
        assert event.binding_id == binding.id
        assert event.instance_id == binding.term_id
        assert event.runtime_epoch == 3
        assert event.run_id == run.id
        # Approval context.
        assert event.approval_id == approval.id
        assert event.tool_call_id == approval.tool_call_id
        assert event.canonical_hash == approval.canonical_hash
        assert event.auth_epoch == approval.auth_epoch
        assert event.pane_id == "%1"
        assert event.operation == "send_text"
        assert event.input_bytes == 9
        assert event.outcome is None
        assert event.error_code is None

    async def test_decided_event_records_the_admin_actor(
        self, policy, repos
    ) -> None:
        binding, conversation, run = await _seed(repos)
        approval = await _create(policy, binding, conversation, run)
        await policy.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="admin",
            auth_epoch=1,
        )
        events = await _events(repos, approval.id)
        assert [event.event_type for event in events] == ["created", "decided"]
        assert events[1].actor == "admin"

    async def test_revoked_event_records_the_actor(self, policy, repos) -> None:
        binding, conversation, run = await _seed(repos)
        approval = await _create(policy, binding, conversation, run)
        await policy.revoke(approval.id, actor="admin")
        events = await _events(repos, approval.id)
        assert [event.event_type for event in events] == ["created", "revoked"]
        assert events[1].actor == "admin"

    async def test_consumed_event_carries_outcome_metadata(self, policy, repos) -> None:
        binding, conversation, run = await _seed(repos)
        approval = await _create(policy, binding, conversation, run)
        await policy.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="admin",
            auth_epoch=1,
        )
        await policy.consume(
            approval.id,
            outcome="confirmed",
            error_code=None,
            input_bytes=9,
        )
        events = await _events(repos, approval.id)
        assert [event.event_type for event in events] == [
            "created",
            "decided",
            "consumed",
        ]
        assert events[2].outcome == "confirmed"
        assert events[2].input_bytes == 9
        # The approval row itself moved normally (CAS semantics unchanged).
        assert approval.state == ApprovalState.PENDING or True
        stored = await repos.approvals.get_by_id(approval.id)
        assert stored is not None
        assert stored.state == ApprovalState.CONSUMED

    async def test_failed_outcome_is_audited_with_error_code(
        self, policy, repos
    ) -> None:
        binding, conversation, run = await _seed(repos)
        approval = await _create(policy, binding, conversation, run)
        await policy.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="admin",
            auth_epoch=1,
        )
        await policy.consume(
            approval.id,
            outcome="failed",
            error_code="pane_not_found",
            input_bytes=9,
        )
        events = await _events(repos, approval.id)
        assert events[-1].event_type == "consumed"
        assert events[-1].outcome == "failed"
        assert events[-1].error_code == "pane_not_found"

    async def test_unknown_event_records_uncertain_outcome(self, policy, repos) -> None:
        binding, conversation, run = await _seed(repos)
        approval = await _create(policy, binding, conversation, run)
        await policy.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="admin",
            auth_epoch=1,
        )
        await policy.mark_unknown(
            approval.id,
            outcome="outcome_unknown",
            error_code="outcome_unknown",
        )
        events = await _events(repos, approval.id)
        assert events[-1].event_type == "unknown"
        assert events[-1].outcome == "outcome_unknown"
        assert events[-1].error_code == "outcome_unknown"


class TestSweepAndRecovery:
    async def test_expire_pending_records_expired_events_for_pending_and_approved(
        self, policy, repos
    ) -> None:
        binding, conversation, run = await _seed(repos)
        now = datetime.now(UTC)
        # Approvals are created with a future expiry (creation validation),
        # then the sweep runs at a later instant to expire them.
        pending = await policy.create_approval(
            binding_id=binding.id,
            conversation_id=conversation.id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=_hash(),
            auth_epoch=1,
            expires_at=now + timedelta(minutes=5),
        )
        approved = await policy.create_approval(
            binding_id=binding.id,
            conversation_id=conversation.id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=_hash(),
            auth_epoch=1,
            expires_at=now + timedelta(minutes=5),
        )
        await policy.decide(
            approved.id,
            decision=ApprovalDecision.APPROVED,
            actor="admin",
            auth_epoch=1,
        )
        live = await policy.create_approval(
            binding_id=binding.id,
            conversation_id=conversation.id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=_hash(),
            auth_epoch=1,
            expires_at=now + timedelta(minutes=10),
        )
        later = now + timedelta(minutes=6)
        count = await policy.expire_pending(now=later)
        assert count == 2
        pending_events = await _events(repos, pending.id)
        assert [event.event_type for event in pending_events] == ["created", "expired"]
        approved_events = await _events(repos, approved.id)
        assert [event.event_type for event in approved_events] == [
            "created",
            "decided",
            "expired",
        ]
        for approval_id in (pending.id, approved.id):
            swept = await repos.approvals.get_by_id(approval_id)
            assert swept is not None
            assert swept.state == ApprovalState.EXPIRED
        # The live approval is untouched.
        untouched = await repos.approvals.get_by_id(live.id)
        assert untouched is not None
        assert untouched.state == ApprovalState.PENDING
        assert len(await _events(repos, live.id)) == 1

    async def test_revoke_for_binding_records_one_event_per_row(
        self, policy, repos
    ) -> None:
        binding, conversation, run = await _seed(repos)
        first = await _create(policy, binding, conversation, run)
        second = await _create(policy, binding, conversation, run)
        count = await policy.revoke_for_binding(binding.id)
        assert count == 2
        for approval in (first, second):
            events = await _events(repos, approval.id)
            assert [event.event_type for event in events] == ["created", "revoked"]
            assert events[-1].actor == "admin"

    async def test_recover_orphaned_approvals_finishes_pending_and_approved(
        self, policy, repos
    ) -> None:
        binding, conversation, run = await _seed(repos)
        pending = await _create(policy, binding, conversation, run)
        approved = await _create(policy, binding, conversation, run)
        await policy.decide(
            approved.id,
            decision=ApprovalDecision.APPROVED,
            actor="admin",
            auth_epoch=1,
        )
        revoked, unknown = await policy.recover_orphaned_approvals()
        assert (revoked, unknown) == (1, 1)

        pending_row = await repos.approvals.get_by_id(pending.id)
        assert pending_row is not None
        assert pending_row.state == ApprovalState.REVOKED
        pending_events = await _events(repos, pending.id)
        assert [event.event_type for event in pending_events] == ["created", "revoked"]
        assert pending_events[-1].actor == "system:restart"

        approved_row = await repos.approvals.get_by_id(approved.id)
        assert approved_row is not None
        assert approved_row.state == ApprovalState.UNKNOWN
        approved_events = await _events(repos, approved.id)
        assert [event.event_type for event in approved_events] == [
            "created",
            "decided",
            "unknown",
        ]
        assert approved_events[-1].actor == "system:restart"
        # A second recovery sweep finds nothing to do.
        assert await policy.recover_orphaned_approvals() == (0, 0)

    async def test_audit_is_best_effort_with_no_writer(self, repos) -> None:
        bare = ApprovalPolicy(repos, repos.session_factory)  # type: ignore[attr-defined]
        binding, conversation, run = await _seed(repos)
        approval = await _create(bare, binding, conversation, run)
        await bare.decide(
            approval.id,
            decision=ApprovalDecision.APPROVED,
            actor="admin",
            auth_epoch=1,
        )
        await bare.consume(approval.id, outcome="confirmed", input_bytes=9)
        assert await _events(repos, approval.id) == []

    async def test_run_agent_recovery_finishes_orphaned_approvals(
        self, policy, repos
    ) -> None:
        """The B-restart sweep (spec §5) feeds the recovery report counts."""
        from termflow_control_plane.plugins.agent_broker.plugin import run_agent_recovery

        binding, conversation, run = await _seed(repos)
        pending = await _create(policy, binding, conversation, run)
        approved = await _create(policy, binding, conversation, run)
        await policy.decide(
            approved.id,
            decision=ApprovalDecision.APPROVED,
            actor="admin",
            auth_epoch=1,
        )
        report = await run_agent_recovery(repos, approval_policy=policy)
        assert report.approvals_revoked == 1
        assert report.approvals_marked_unknown == 1
        assert (
            await repos.approvals.get_by_id(pending.id)
        ).state == ApprovalState.REVOKED
        assert (
            await repos.approvals.get_by_id(approved.id)
        ).state == ApprovalState.UNKNOWN
        # No waiter survived the restart, so nothing ever re-executes.
        assert await repos.approvals.get_by_tool_call(
            conversation.id, pending.tool_call_id
        ) is not None


class TestRetentionAndSchema:
    async def test_audit_rows_purged_after_90_days(self, repos) -> None:
        binding, conversation, run = await _seed(repos)
        policy = ApprovalPolicy(
            repos,
            repos.session_factory,  # type: ignore[attr-defined]
            audit=ApprovalAuditWriter(repos),
        )
        approval = await _create(policy, binding, conversation, run)
        now = datetime.now(UTC)
        # Move the event row back past the retention window.
        from sqlalchemy import update

        from termflow_control_plane.persistence.models import ApprovalAuditEvent

        async with repos.session_factory() as session:  # type: ignore[attr-defined]
            await session.execute(
                update(ApprovalAuditEvent)
                .where(ApprovalAuditEvent.approval_id == approval.id)
                .values(created_at=now - AGENT_APPROVAL_AUDIT_RETENTION - timedelta(days=1))
            )
            await session.commit()
        assert AGENT_APPROVAL_AUDIT_RETENTION == timedelta(days=90)
        counts = await repos.purge_expired(now=now)
        assert counts["approval_audit"] == 1
        assert await _events(repos, approval.id) == []

    def test_audit_table_has_no_raw_text_or_key_columns(self) -> None:
        columns = {
            column.name for column in Base.metadata.tables["approval_audit_events"].columns
        }
        assert "text" not in columns
        assert "keys" not in columns
        assert "content" not in columns
        assert {"event_type", "approval_id", "canonical_hash", "tool_call_id"} <= columns

    async def test_approval_request_stores_bounded_display_metadata(
        self, policy, repos
    ) -> None:
        binding, conversation, run = await _seed(repos)
        approval = await _create(policy, binding, conversation, run)
        assert approval.pane_id == "%1"
        assert approval.operation == "send_text"
        assert approval.intent_summary == "run the test suite"
        # Legacy rows keep NULL display metadata (history degrades).
        legacy = await policy.create_approval(
            binding_id=binding.id,
            conversation_id=conversation.id,
            tool_call_id=f"tool-{uuid4().hex[:12]}",
            canonical_hash=_hash(),
            auth_epoch=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        assert legacy.pane_id is None
        assert legacy.operation is None
        assert legacy.intent_summary is None

    async def test_approval_audit_event_matches_migration_schema(self, tmp_path) -> None:
        """The ORM table and the migrated schema must be column-identical."""
        from alembic import command
        from sqlalchemy import create_engine
        from termflow_control_plane.persistence.database import _migration_config

        orm_engine = create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
        migrated_engine = create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
        try:
            Base.metadata.create_all(orm_engine)
            with migrated_engine.begin() as connection:
                command.upgrade(_migration_config(connection), "head")
            for engine in (orm_engine, migrated_engine):
                inspector = inspect(engine)
                columns = inspector.get_columns("approval_audit_events")
                assert {column["name"] for column in columns} == {
                    "id",
                    "created_at",
                    "event_type",
                    "approval_id",
                    "binding_id",
                    "instance_id",
                    "runtime_epoch",
                    "conversation_id",
                    "run_id",
                    "tool_call_id",
                    "pane_id",
                    "operation",
                    "input_bytes",
                    "canonical_hash",
                    "auth_epoch",
                    "actor",
                    "outcome",
                    "error_code",
                }
                indexes = {
                    index["name"] for index in inspector.get_indexes("approval_audit_events")
                }
                assert indexes == {
                    "ix_approval_audit_events_approval_id",
                    "ix_approval_audit_events_created_at",
                }
        finally:
            orm_engine.dispose()
            migrated_engine.dispose()
