"""Agent Broker persistence model tests (plan §15, task M1.1 models).

These tests exercise the SQLAlchemy models directly against a fresh SQLite
database created from ``Base.metadata``; the Alembic migration is a separate
task and is intentionally not involved.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentCleanupJob,
    AgentConversation,
    AgentDiagnostic,
    AgentEvent,
    AgentInboxItem,
    AgentMemoryScope,
    AgentMessage,
    AgentProfile,
    AgentRun,
    AgentRuntimeBinding,
    AgentToken,
    AgentToolRequest,
    ApprovalRequest,
    BackendConversationRef,
    Base,
    Installation,
    Instance,
    PanePolicy,
    TranscriptDraft,
    Watch,
    WatchDelivery,
    WriteGrant,
)

# (model class, expected table name) pairs for every Agent Broker model.
MODEL_TABLE_PAIRS = [
    (AgentProfile, "agent_profiles"),
    (AgentBinding, "agent_bindings"),
    (AgentMemoryScope, "agent_memory_scopes"),
    (AgentConversation, "agent_conversations"),
    (BackendConversationRef, "agent_backend_conversations"),
    (AgentRuntimeBinding, "agent_runtime_bindings"),
    (AgentInboxItem, "agent_inbox_items"),
    (AgentToolRequest, "agent_tool_requests"),
    (AgentRun, "agent_runs"),
    (AgentMessage, "agent_messages"),
    (AgentEvent, "agent_events"),
    (AgentToken, "agent_tokens"),
    (PanePolicy, "pane_policies"),
    (ApprovalRequest, "approval_requests"),
    (WriteGrant, "write_grants"),
    (Watch, "watches"),
    (WatchDelivery, "watch_deliveries"),
    (TranscriptDraft, "transcript_drafts"),
    (AgentCleanupJob, "agent_cleanup_jobs"),
    (AgentDiagnostic, "agent_diagnostics"),
]

AGENT_TABLE_NAMES = [table_name for _, table_name in MODEL_TABLE_PAIRS]

# (table, column) pairs that are explicit string state columns.
STATE_COLUMNS = {
    ("agent_bindings", "status"),
    ("agent_conversations", "status"),
    ("agent_inbox_items", "delivery_state"),
    ("agent_runs", "run_state"),
    ("agent_runtime_bindings", "readiness"),
    ("approval_requests", "state"),
    ("write_grants", "state"),
    ("watches", "state"),
    ("transcript_drafts", "state"),
    ("agent_cleanup_jobs", "state"),
}

# Foreign keys that reference a Term (`instances.id`) or Installation and must
# cascade so parent deletion is never blocked by orphaned Agent rows.  Cleanup
# jobs are handled separately because they are durable tombstones.
ON_DELETE_CASCADE = {
    "agent_bindings": {("profile_id", "agent_profiles"), ("term_id", "instances")},
    "agent_memory_scopes": {
        ("binding_id", "agent_bindings"),
        ("term_id", "instances"),
        ("profile_id", "agent_profiles"),
    },
    "agent_conversations": {("binding_id", "agent_bindings")},
    "agent_backend_conversations": {("conversation_id", "agent_conversations")},
    "agent_runtime_bindings": {("binding_id", "agent_bindings")},
    "agent_inbox_items": {("conversation_id", "agent_conversations")},
    "agent_tool_requests": {("binding_id", "agent_bindings"), ("run_id", "agent_runs")},
    "agent_runs": {("conversation_id", "agent_conversations")},
    "agent_messages": {("conversation_id", "agent_conversations"), ("run_id", "agent_runs")},
    "agent_events": {("conversation_id", "agent_conversations"), ("run_id", "agent_runs")},
    "agent_tokens": {("binding_id", "agent_bindings")},
    "pane_policies": {("binding_id", "agent_bindings")},
    "approval_requests": {
        ("binding_id", "agent_bindings"),
        ("conversation_id", "agent_conversations"),
        ("run_id", "agent_runs"),
    },
    "write_grants": {("binding_id", "agent_bindings")},
    "watches": {("binding_id", "agent_bindings"), ("conversation_id", "agent_conversations")},
    "watch_deliveries": {("watch_id", "watches")},
    "transcript_drafts": {
        ("binding_id", "agent_bindings"),
        ("target_conversation_id", "agent_conversations"),
    },
    "agent_diagnostics": {("conversation_id", "agent_conversations")},
}


@pytest.fixture
def engine(tmp_path) -> Engine:
    """Fresh SQLite database with every metadata table and FK enforcement on."""
    database_path = tmp_path / "agent_models.db"
    engine = create_engine(f"sqlite:///{database_path}")

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _foreign_keys(inspector, table_name: str) -> dict[tuple[str, ...], tuple[str, dict]]:
    return {
        tuple(foreign_key["constrained_columns"] or ()): (
            foreign_key["referred_table"],
            foreign_key.get("options") or {},
        )
        for foreign_key in inspector.get_foreign_keys(table_name)
    }


def _unique_column_sets(inspector, table_name: str) -> set[frozenset[str]]:
    unique = set()
    for index in inspector.get_indexes(table_name):
        if index["unique"]:
            unique.add(frozenset(index["column_names"] or ()))
    for constraint in inspector.get_unique_constraints(table_name):
        unique.add(frozenset(constraint["column_names"] or ()))
    return unique


def _index_column_tuples(inspector, table_name: str) -> set[tuple[str, ...]]:
    return {tuple(index["column_names"] or ()) for index in inspector.get_indexes(table_name)}


def _seed_binding_chain(session: Session) -> dict[str, object]:
    """Create Installation -> Instance -> Profile -> Binding -> Conversation."""
    installation = Installation(id=uuid4(), token_hash="i" * 64)
    instance = Instance(
        id=uuid4(), installation_id=installation.id, name="term-1", token_hash="t" * 64
    )
    profile = AgentProfile(
        id=uuid4(), display_name="opencode-main", backend_kind="opencode", config="{}"
    )
    # Flush in FK dependency order: the unit of work does not topologically
    # order INSERTs for tables connected only by ForeignKey (no relationship).
    session.add_all([installation, instance, profile])
    session.flush()
    binding = AgentBinding(
        id=uuid4(), profile_id=profile.id, term_id=instance.id, status="active"
    )
    session.add(binding)
    session.flush()
    conversation = AgentConversation(id=uuid4(), binding_id=binding.id, status="open")
    session.add(conversation)
    session.commit()
    return {
        "installation": installation,
        "instance": instance,
        "profile": profile,
        "binding": binding,
        "conversation": conversation,
    }


def test_model_classes_map_to_expected_tables() -> None:
    for model_class, table_name in MODEL_TABLE_PAIRS:
        assert model_class.__tablename__ == table_name
        assert table_name in Base.metadata.tables


def test_create_all_creates_all_agent_tables(engine: Engine) -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert set(AGENT_TABLE_NAMES) <= table_names


def test_every_agent_table_has_a_primary_key(engine: Engine) -> None:
    inspector = inspect(engine)
    for table_name in AGENT_TABLE_NAMES:
        primary_key = inspector.get_pk_constraint(table_name)
        assert primary_key["constrained_columns"], f"{table_name} has no primary key"


def test_foreign_key_columns_exist(engine: Engine) -> None:
    inspector = inspect(engine)
    for table_name in AGENT_TABLE_NAMES:
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        for foreign_key in inspector.get_foreign_keys(table_name):
            for column_name in foreign_key["constrained_columns"] or []:
                assert column_name in columns, f"{table_name}.{column_name} missing"


def test_term_and_installation_foreign_keys_cascade(engine: Engine) -> None:
    inspector = inspect(engine)
    for table_name, expected in ON_DELETE_CASCADE.items():
        actual = _foreign_keys(inspector, table_name)
        for constrained, referred_table in expected:
            referred, options = actual[(constrained,)]
            assert referred == referred_table, f"{table_name}.{constrained}"
            assert options.get("ondelete") == "CASCADE", (
                f"{table_name}.{constrained} must use ON DELETE CASCADE"
            )


def test_cleanup_job_foreign_keys_are_set_null_tombstones(engine: Engine) -> None:
    inspector = inspect(engine)
    actual = _foreign_keys(inspector, "agent_cleanup_jobs")
    expected_links = (("term_id", "instances"), ("installation_id", "installations"))
    for constrained, referred_table in expected_links:
        referred, options = actual[(constrained,)]
        assert referred == referred_table
        assert options.get("ondelete") == "SET NULL", (
            f"agent_cleanup_jobs.{constrained} must use ON DELETE SET NULL "
            "so the tombstone outlives the parent"
        )


def test_watch_delivery_inbox_link_is_set_null(engine: Engine) -> None:
    inspector = inspect(engine)
    referred, options = _foreign_keys(inspector, "watch_deliveries")[("inbox_item_id",)]
    assert referred == "agent_inbox_items"
    assert options.get("ondelete") == "SET NULL", (
        "watch_deliveries.inbox_item_id must use ON DELETE SET NULL so the "
        "delivery receipt survives inbox-item purge"
    )


def test_required_unique_indexes_exist(engine: Engine) -> None:
    inspector = inspect(engine)
    expected_unique = {
        "agent_inbox_items": {"conversation_id", "admission_seq"},
        "agent_tool_requests": {"request_key"},
        "watch_deliveries": {"delivery_key"},
        "agent_backend_conversations": {"provider_ref"},
        "agent_tokens": {"token_hash"},
        "agent_events": {"conversation_id", "database_seq"},
        "agent_messages": {"conversation_id", "assembly_revision"},
        "agent_bindings": {"profile_id", "term_id", "status"},
        "agent_runtime_bindings": {"runtime_ref", "runtime_epoch"},
        "pane_policies": {"binding_id", "pane_id"},
        "approval_requests": {"conversation_id", "tool_call_id"},
        "agent_memory_scopes": {"binding_id"},
    }
    for table_name, columns in expected_unique.items():
        assert frozenset(columns) in _unique_column_sets(inspector, table_name), (
            f"{table_name} missing unique {sorted(columns)}"
        )


def test_required_indexes_exist(engine: Engine) -> None:
    inspector = inspect(engine)
    expected_indexes = [
        ("agent_runs", ("conversation_id", "run_state")),
        ("agent_messages", ("conversation_id", "created_at")),
        ("agent_inbox_items", ("delivery_state", "next_attempt_at", "claim_expires_at")),
        ("agent_tool_requests", ("tool_call_id",)),
        ("approval_requests", ("state", "expires_at")),
        ("watches", ("binding_id", "state")),
        ("watches", ("expiry_at",)),
        ("watch_deliveries", ("next_attempt_at",)),
        ("transcript_drafts", ("state", "expires_at")),
        ("agent_cleanup_jobs", ("state", "next_attempt_at")),
        ("agent_diagnostics", ("ttl_expires_at",)),
    ]
    for table_name, columns in expected_indexes:
        assert columns in _index_column_tuples(inspector, table_name), (
            f"{table_name} missing index on {columns}"
        )


def test_agent_tokens_has_no_raw_token_column(engine: Engine) -> None:
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("agent_tokens")}
    assert "token" not in columns
    assert columns == {
        "id",
        "binding_id",
        "token_hash",
        "scopes",
        "expiry_epoch",
        "binding_epoch",
        "revoked_at",
        "created_at",
    }


def test_state_columns_are_explicit_strings(engine: Engine) -> None:
    inspector = inspect(engine)
    for table_name, column_name in STATE_COLUMNS:
        column = next(
            column for column in inspector.get_columns(table_name) if column["name"] == column_name
        )
        assert str(column["type"]) == "VARCHAR(32)", f"{table_name}.{column_name}"


def test_explicit_state_values_round_trip(engine: Engine) -> None:
    session = Session(engine)
    try:
        chain = _seed_binding_chain(session)
        binding = session.get(AgentBinding, chain["binding"].id)
        assert binding is not None
        assert binding.status == "active"

        item = AgentInboxItem(
            conversation_id=chain["conversation"].id,
            kind="user_message",
            actor_id="actor-1",
            actor_kind="client",
            admission_seq=1,
            idempotency_key="idem-round-trip",
            payload_digest="a" * 64,
            source="user",
            delivery_state="pending",
            correlation_id=uuid4(),
        )
        session.add(item)
        session.commit()
        reloaded = session.get(AgentInboxItem, item.id)
        assert reloaded is not None
        assert reloaded.delivery_state == "pending"
        assert reloaded.attempt_count == 0
    finally:
        session.close()


def test_deleting_instance_cascades_to_agent_rows(engine: Engine) -> None:
    session = Session(engine)
    try:
        chain = _seed_binding_chain(session)
        scope = AgentMemoryScope(
            binding_id=chain["binding"].id,
            term_id=chain["instance"].id,
            profile_id=chain["profile"].id,
            byte_quota=1_000_000,
            count_quota=500,
        )
        session.add(scope)
        session.commit()
        binding_id = chain["binding"].id
        conversation_id = chain["conversation"].id
        scope_id = scope.id

        session.delete(chain["instance"])
        session.commit()

        assert session.get(AgentBinding, binding_id) is None
        assert session.get(AgentConversation, conversation_id) is None
        assert session.get(AgentMemoryScope, scope_id) is None
    finally:
        session.close()


def test_cleanup_job_survives_term_deletion(engine: Engine) -> None:
    session = Session(engine)
    try:
        installation = Installation(id=uuid4(), token_hash="i" * 64)
        instance = Instance(
            id=uuid4(), installation_id=installation.id, name="term-1", token_hash="t" * 64
        )
        session.add_all([installation, instance])
        session.flush()
        job = AgentCleanupJob(
            term_id=instance.id,
            target_kind="binding",
            target_ref=str(instance.id),
            state="pending",
        )
        session.add(job)
        session.commit()
        job_id = job.id

        session.delete(instance)
        session.commit()

        surviving = session.get(AgentCleanupJob, job_id)
        assert surviving is not None
        assert surviving.term_id is None
        assert surviving.target_ref == str(instance.id)
        assert surviving.state == "pending"
    finally:
        session.close()


def test_binding_unique_per_profile_term_and_state(engine: Engine) -> None:
    session = Session(engine)
    try:
        chain = _seed_binding_chain(session)
        duplicate = AgentBinding(
            profile_id=chain["profile"].id,
            term_id=chain["instance"].id,
            status="active",
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        retired = AgentBinding(
            profile_id=chain["profile"].id,
            term_id=chain["instance"].id,
            status="retired",
        )
        session.add(retired)
        session.commit()
        assert session.get(AgentBinding, retired.id).status == "retired"
    finally:
        session.close()


def test_duplicate_inbox_admission_seq_raises(engine: Engine) -> None:
    session = Session(engine)
    try:
        chain = _seed_binding_chain(session)

        def item(admission_seq: int, idempotency_key: str, actor_id: str) -> AgentInboxItem:
            return AgentInboxItem(
                conversation_id=chain["conversation"].id,
                kind="user_message",
                actor_id=actor_id,
                actor_kind="client",
                admission_seq=admission_seq,
                idempotency_key=idempotency_key,
                payload_digest="a" * 64,
                source="user",
                delivery_state="pending",
                correlation_id=uuid4(),
            )

        session.add(item(1, "idem-first", "actor-1"))
        session.add(item(1, "idem-second", "actor-2"))
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.close()


def test_duplicate_agent_token_hash_raises(engine: Engine) -> None:
    session = Session(engine)
    try:
        chain = _seed_binding_chain(session)
        token_kwargs = {
            "binding_id": chain["binding"].id,
            "scopes": "terminal.observe",
            "expiry_epoch": 1000,
            "binding_epoch": 1,
        }
        session.add_all(
            [
                AgentToken(token_hash="h" * 64, **token_kwargs),
                AgentToken(token_hash="h" * 64, **token_kwargs),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.close()


def test_duplicate_approval_tool_call_id_raises(engine: Engine) -> None:
    session = Session(engine)
    try:
        chain = _seed_binding_chain(session)

        def approval(tool_call_id: str) -> ApprovalRequest:
            return ApprovalRequest(
                binding_id=chain["binding"].id,
                conversation_id=chain["conversation"].id,
                tool_call_id=tool_call_id,
                canonical_hash="c" * 64,
                state="pending",
                expires_at=datetime.now(UTC),
                auth_epoch=1,
            )

        session.add(approval("tool-call-1"))
        session.add(approval("tool-call-1"))
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.close()
