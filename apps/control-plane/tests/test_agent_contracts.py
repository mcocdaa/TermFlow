"""Acceptance tests for the Agent Broker privacy, retention, quota, and disclosure contracts.

These tests freeze the executable contracts behind M0.4 (plan sections 4.3, 16.1, 17, 20).
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from termflow_control_plane.agent_contracts import (
    CURRENT_EXTERNAL_DISCLOSURE_POLICY_VERSION,
    QUOTAS,
    RETENTION_MATRIX,
    AgentAdmissionSequence,
    AgentCursorPolicy,
    AgentQuotas,
    CursorResetMarker,
    CursorScope,
    DataClass,
    ExternalProviderDisclosure,
    RetentionPolicy,
    StartupFencingOrder,
    StartupStep,
    validate_admission_order,
)

KIB = 1024


def test_retention_matrix_matches_plan_16_1_defaults() -> None:
    """Every data class from the §16.1 table carries its documented default ceiling."""
    assert RETENTION_MATRIX[DataClass.FINAL_MESSAGES].retention == timedelta(days=30)
    assert RETENTION_MATRIX[DataClass.ASSEMBLY_CHECKPOINTS].retention == timedelta(hours=24)
    assert RETENTION_MATRIX[DataClass.TERMINAL_WATCH_EXCERPTS].retention == timedelta(hours=24)
    assert RETENTION_MATRIX[DataClass.MEMORY_FACTS].retention == timedelta(days=90)
    assert RETENTION_MATRIX[DataClass.APPROVAL_AUDIT_METADATA].retention == timedelta(days=90)
    assert RETENTION_MATRIX[DataClass.DEBUG_DIAGNOSTICS].retention == timedelta(hours=24)
    assert RETENTION_MATRIX[DataClass.TRANSCRIPT_DRAFT].retention == timedelta(hours=1)
    assert RETENTION_MATRIX[DataClass.CONTAINER_LOGS].retention == timedelta(days=7)
    assert RETENTION_MATRIX[DataClass.CLEANUP_TOMBSTONE].retention == timedelta(days=30)
    # The OpenCode volume is governed by the conversation policy, not a fixed B ceiling.
    assert RETENTION_MATRIX[DataClass.OPENCODE_VOLUME].retention is None
    # Raw audio is deleted immediately after transcription or failure.
    assert RETENTION_MATRIX[DataClass.TRANSCRIPT_RAW_AUDIO].retention == timedelta(0)


def test_terminal_watch_excerpts_are_capped_at_64_kib() -> None:
    assert RETENTION_MATRIX[DataClass.TERMINAL_WATCH_EXCERPTS].max_bytes == 64 * KIB


def test_matrix_covers_every_declared_data_class() -> None:
    assert set(RETENTION_MATRIX) == set(DataClass)


def test_raw_storage_and_redaction_flags() -> None:
    """Raw terminal/watch content and raw audio are never persisted."""
    assert RETENTION_MATRIX[DataClass.TERMINAL_WATCH_EXCERPTS].raw_storage is False
    assert RETENTION_MATRIX[DataClass.TERMINAL_WATCH_EXCERPTS].redacted is True
    assert RETENTION_MATRIX[DataClass.TRANSCRIPT_RAW_AUDIO].raw_storage is False
    assert RETENTION_MATRIX[DataClass.APPROVAL_AUDIT_METADATA].raw_storage is False
    assert RETENTION_MATRIX[DataClass.APPROVAL_AUDIT_METADATA].redacted is True
    assert RETENTION_MATRIX[DataClass.DEBUG_DIAGNOSTICS].raw_storage is False
    assert RETENTION_MATRIX[DataClass.CONTAINER_LOGS].redacted is True


def test_retention_policy_rejects_negative_retention_and_non_positive_byte_caps() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(data_class=DataClass.FINAL_MESSAGES, retention=timedelta(days=-1))
    with pytest.raises(ValueError):
        RetentionPolicy(data_class=DataClass.FINAL_MESSAGES, max_bytes=0)
    with pytest.raises(ValueError):
        RetentionPolicy(data_class=DataClass.FINAL_MESSAGES, max_bytes=-64 * KIB)


def test_retention_policy_accepts_immediate_deletion_and_unbounded_retention() -> None:
    immediate = RetentionPolicy(
        data_class=DataClass.TRANSCRIPT_RAW_AUDIO,
        retention=timedelta(0),
    )
    policy_governed = RetentionPolicy(data_class=DataClass.OPENCODE_VOLUME, retention=None)
    assert immediate.retention == timedelta(0)
    assert policy_governed.retention is None


def test_retention_policy_rejects_unknown_data_class() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(data_class="not_a_data_class")  # type: ignore[arg-type]


def test_quota_defaults_are_positive_and_bounded() -> None:
    for value in (
        QUOTAS.max_inbox_items,
        QUOTAS.max_concurrent_watches,
        QUOTAS.max_concurrent_runs,
        QUOTAS.max_output_bytes_per_excerpt,
        QUOTAS.max_token_bytes,
    ):
        assert isinstance(value, int)
        assert 0 < value < 1_000_000
    assert QUOTAS.max_run_cost.is_finite()
    assert QUOTAS.max_run_cost > 0
    # The excerpt byte cap in the quota contract matches the retention matrix ceiling.
    assert QUOTAS.max_output_bytes_per_excerpt == 64 * KIB


def _valid_quota() -> AgentQuotas:
    return AgentQuotas(
        max_inbox_items=256,
        max_concurrent_watches=16,
        max_concurrent_runs=1,
        max_output_bytes_per_excerpt=64 * KIB,
        max_token_bytes=32 * KIB,
        max_run_cost=Decimal("25.00"),
    )


def test_agent_quotas_reject_non_positive_limits() -> None:
    for kwarg in (
        "max_inbox_items",
        "max_concurrent_watches",
        "max_concurrent_runs",
        "max_output_bytes_per_excerpt",
        "max_token_bytes",
    ):
        invalid = vars(_valid_quota())
        invalid[kwarg] = 0
        with pytest.raises(ValueError):
            AgentQuotas(**invalid)


def test_agent_quotas_reject_non_finite_cost_ceiling() -> None:
    for bad_cost in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1.00")):
        invalid = vars(_valid_quota())
        invalid["max_run_cost"] = bad_cost
        with pytest.raises(ValueError):
            AgentQuotas(**invalid)


def test_cursor_policy_is_global_or_conversation_scoped() -> None:
    global_policy = AgentCursorPolicy(scope=CursorScope.GLOBAL, epoch=1)
    conversation_policy = AgentCursorPolicy(scope=CursorScope.CONVERSATION, epoch=1)
    assert global_policy.scope is CursorScope.GLOBAL
    assert conversation_policy.scope is CursorScope.CONVERSATION
    assert global_policy.has_epoch_reset_component is True
    assert conversation_policy.has_epoch_reset_component is True


def test_cursor_policy_rejects_unknown_scope_and_negative_epoch() -> None:
    with pytest.raises(ValueError):
        AgentCursorPolicy(scope="not-a-scope", epoch=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AgentCursorPolicy(scope=CursorScope.GLOBAL, epoch=-1)


def test_cursor_too_old_yields_snapshot_reset_marker() -> None:
    policy = AgentCursorPolicy(scope=CursorScope.GLOBAL, epoch=3)
    assert policy.is_too_old(2) is True
    assert policy.is_too_old(3) is False

    marker = policy.resolve(2)
    assert isinstance(marker, CursorResetMarker)
    assert marker.epoch == 3
    assert marker.scope is CursorScope.GLOBAL

    # A client without any cursor starts from the snapshot/reset marker too.
    assert isinstance(policy.resolve(None), CursorResetMarker)


def test_cursor_within_epoch_resolves_to_the_position() -> None:
    policy = AgentCursorPolicy(scope=CursorScope.CONVERSATION, epoch=3)
    assert policy.resolve(3) == 3


def test_admission_sequences_are_monotonic_per_conversation() -> None:
    conversation_id = uuid4()
    validate_admission_order(
        [
            AgentAdmissionSequence(conversation_id=conversation_id, seq=0),
            AgentAdmissionSequence(conversation_id=conversation_id, seq=1),
            AgentAdmissionSequence(conversation_id=conversation_id, seq=2),
        ]
    )


def test_admission_sequences_reject_reordering_and_duplicates() -> None:
    conversation_id = uuid4()
    with pytest.raises(ValueError):
        validate_admission_order(
            [
                AgentAdmissionSequence(conversation_id=conversation_id, seq=0),
                AgentAdmissionSequence(conversation_id=conversation_id, seq=2),
                AgentAdmissionSequence(conversation_id=conversation_id, seq=1),
            ]
        )
    with pytest.raises(ValueError):
        validate_admission_order(
            [
                AgentAdmissionSequence(conversation_id=conversation_id, seq=1),
                AgentAdmissionSequence(conversation_id=conversation_id, seq=1),
            ]
        )
    with pytest.raises(ValueError):
        validate_admission_order(
            [
                AgentAdmissionSequence(conversation_id=conversation_id, seq=0),
                AgentAdmissionSequence(conversation_id=conversation_id, seq=-1),
            ]
        )


def test_admission_sequences_are_ordered_independently_per_conversation() -> None:
    first, second = uuid4(), uuid4()
    validate_admission_order(
        [
            AgentAdmissionSequence(conversation_id=first, seq=0),
            AgentAdmissionSequence(conversation_id=second, seq=0),
            AgentAdmissionSequence(conversation_id=first, seq=1),
            AgentAdmissionSequence(conversation_id=second, seq=1),
        ]
    )


def test_startup_fencing_accepts_the_canonical_order() -> None:
    StartupFencingOrder.validate(list(StartupFencingOrder.REQUIRED_ORDER))


def test_startup_fencing_rejects_reordered_steps() -> None:
    with pytest.raises(ValueError):
        StartupFencingOrder.validate(
            [
                StartupStep.RECONNECT_A_TOPOLOGY,
                StartupStep.DB_INTEGRITY_AND_MIGRATION,
                StartupStep.RUNTIME_EPOCH_AND_STALE_FENCE,
                StartupStep.RECONNECT_BACKEND_SSE,
                StartupStep.REBUILD_WATCH_CURSORS,
                StartupStep.DISPATCH_PENDING_INBOX,
            ]
        )


def test_startup_fencing_requires_first_and_last_steps() -> None:
    middle = [
        StartupStep.RUNTIME_EPOCH_AND_STALE_FENCE,
        StartupStep.RECONNECT_A_TOPOLOGY,
        StartupStep.RECONNECT_BACKEND_SSE,
        StartupStep.REBUILD_WATCH_CURSORS,
    ]
    with pytest.raises(ValueError):
        StartupFencingOrder.validate(
            [StartupStep.RUNTIME_EPOCH_AND_STALE_FENCE, *middle]
        )
    with pytest.raises(ValueError):
        StartupFencingOrder.validate(
            [StartupStep.DB_INTEGRITY_AND_MIGRATION, *middle]
        )
    with pytest.raises(ValueError):
        StartupFencingOrder.validate([])


def test_startup_fencing_rejects_duplicated_steps() -> None:
    with pytest.raises(ValueError):
        StartupFencingOrder.validate(
            [
                StartupStep.DB_INTEGRITY_AND_MIGRATION,
                StartupStep.RUNTIME_EPOCH_AND_STALE_FENCE,
                StartupStep.RUNTIME_EPOCH_AND_STALE_FENCE,
                StartupStep.RECONNECT_A_TOPOLOGY,
                StartupStep.RECONNECT_BACKEND_SSE,
                StartupStep.REBUILD_WATCH_CURSORS,
                StartupStep.DISPATCH_PENDING_INBOX,
            ]
        )


def _complete_disclosure() -> ExternalProviderDisclosure:
    return ExternalProviderDisclosure(
        endpoint="https://model-provider.example/v1",
        model="provider-model-1",
        region="eu-central-1",
        retention_terms="28 days, no training",
        no_training=True,
        user_consent=True,
        policy_version=CURRENT_EXTERNAL_DISCLOSURE_POLICY_VERSION,
    )


def test_complete_disclosure_with_consent_enables_binding() -> None:
    assert _complete_disclosure().binding_enabled() is True


def test_disclosure_fails_closed_when_incomplete() -> None:
    assert replace(_complete_disclosure(), region=None).binding_enabled() is False
    assert replace(_complete_disclosure(), retention_terms=None).binding_enabled() is False
    assert replace(_complete_disclosure(), endpoint="").binding_enabled() is False
    assert replace(_complete_disclosure(), model="").binding_enabled() is False


def test_disclosure_fails_closed_without_consent() -> None:
    assert replace(_complete_disclosure(), user_consent=False).binding_enabled() is False


def test_disclosure_fails_closed_when_no_training_is_not_agreed() -> None:
    assert replace(_complete_disclosure(), no_training=False).binding_enabled() is False


def test_disclosure_fails_closed_on_unknown_or_drifted_policy() -> None:
    assert (
        replace(_complete_disclosure(), policy_version="2025-01-01").binding_enabled()
        is False
    )
    assert (
        replace(_complete_disclosure(), policy_version=None).binding_enabled() is False
    )


def test_empty_disclosure_disables_binding() -> None:
    assert ExternalProviderDisclosure().binding_enabled() is False
