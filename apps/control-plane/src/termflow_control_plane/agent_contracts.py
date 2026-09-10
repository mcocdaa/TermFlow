"""Executable privacy, retention, and external-disclosure contracts for the Agent Broker.

This module freezes the acceptance contracts behind milestone M0.4:

- §16.1 data retention defaults: explicit, configurable ceilings rather than an
  unspecified "bounded" promise. ``RETENTION_MATRIX`` maps every data class to its
  default retention/limit, redaction, and raw-storage rules.
- §4.3 unknown-backend-event diagnostic restrictions: metadata-only persistence,
  no raw parts/tool results/headers/SSE bodies/reasoning/terminal excerpts.
- §20 privacy matrix: no raw token, provider credential, or unbounded terminal
  content in logs/database; external provider disclosure and
  enablement confirmation (``ExternalProviderDisclosure`` fails closed).
- §17 B-restart fencing order (``StartupFencingOrder``), the DB-assigned
  per-conversation admission sequence (``AgentAdmissionSequence``), opaque Agent
  cursor scope/reset semantics (``AgentCursorPolicy``), and per-binding
  ``AgentQuotas``.

These are executable contracts/config only: no runtime feature behavior lives
here. Consumers (P1+ later milestones) import the defaults and enforce them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

KIB = 1024


class AgentProfileConfig(BaseModel):
    """The complete client-controlled portion of an Agent Profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=128)


class DataClass(StrEnum):
    """The data classes governed by the §16.1 retention matrix."""

    FINAL_MESSAGES = "final_messages"
    ASSEMBLY_CHECKPOINTS = "assembly_checkpoints"
    TERMINAL_WATCH_EXCERPTS = "terminal_watch_excerpts"
    MEMORY_FACTS = "memory_facts"
    APPROVAL_AUDIT_METADATA = "approval_audit_metadata"
    DEBUG_DIAGNOSTICS = "debug_diagnostics"
    OPENCODE_VOLUME = "opencode_volume"
    CONTAINER_LOGS = "container_logs"
    CLEANUP_TOMBSTONE = "cleanup_tombstone"


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention and storage ceiling for one §16.1 data class.

    ``retention`` is ``None`` only when the ceiling is governed by an explicit
    product policy (the OpenCode volume follows the conversation policy rather
    than a fixed B default). ``raw_storage`` is ``False`` wherever the raw form
    of the data class is never persisted (raw pane streams, metadata
    only, or hash/redacted text); ``redacted`` marks classes whose content must
    be redacted or hashed before persistence.
    """

    data_class: DataClass
    retention: timedelta | None = None
    max_bytes: int | None = None
    redacted: bool = False
    raw_storage: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.data_class, DataClass):
            raise ValueError(f"unknown data class: {self.data_class!r}")
        if self.retention is not None and self.retention < timedelta(0):
            raise ValueError("retention must be non-negative")
        if self.max_bytes is not None and self.max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")


RETENTION_MATRIX: dict[DataClass, RetentionPolicy] = {
    # §16.1: final product messages and durable timeline, 30 days.
    DataClass.FINAL_MESSAGES: RetentionPolicy(
        data_class=DataClass.FINAL_MESSAGES,
        retention=timedelta(days=30),
        raw_storage=True,
    ),
    # §16.1: streaming assembly checkpoints, 24 hours + per-run byte quota.
    DataClass.ASSEMBLY_CHECKPOINTS: RetentionPolicy(
        data_class=DataClass.ASSEMBLY_CHECKPOINTS,
        retention=timedelta(hours=24),
        raw_storage=True,
    ),
    # §16.1: terminal/watch excerpts, 24 hours, redacted, ≤ 64 KiB; raw pane
    # stream is never persisted (§4.3 diagnostic path excludes raw excerpts).
    DataClass.TERMINAL_WATCH_EXCERPTS: RetentionPolicy(
        data_class=DataClass.TERMINAL_WATCH_EXCERPTS,
        retention=timedelta(hours=24),
        max_bytes=64 * KIB,
        redacted=True,
        raw_storage=False,
    ),
    # §16.1: user-approved memory facts, 90 days; per-scope count/byte quota.
    DataClass.MEMORY_FACTS: RetentionPolicy(
        data_class=DataClass.MEMORY_FACTS,
        retention=timedelta(days=90),
        raw_storage=True,
    ),
    # §16.1: approval/audit metadata, 90 days; text as hash/redacted metadata.
    DataClass.APPROVAL_AUDIT_METADATA: RetentionPolicy(
        data_class=DataClass.APPROVAL_AUDIT_METADATA,
        retention=timedelta(days=90),
        redacted=True,
        raw_storage=False,
    ),
    # §16.1: unknown/debug diagnostics, 24 hours; metadata only (§4.3).
    DataClass.DEBUG_DIAGNOSTICS: RetentionPolicy(
        data_class=DataClass.DEBUG_DIAGNOSTICS,
        retention=timedelta(hours=24),
        redacted=True,
        raw_storage=False,
    ),
    # §16.1: OpenCode volume retention is governed by the conversation policy.
    DataClass.OPENCODE_VOLUME: RetentionPolicy(
        data_class=DataClass.OPENCODE_VOLUME,
        retention=None,
        raw_storage=True,
    ),
    # §16.1: container logs, 7 days with size rotation and redaction.
    DataClass.CONTAINER_LOGS: RetentionPolicy(
        data_class=DataClass.CONTAINER_LOGS,
        retention=timedelta(days=7),
        redacted=True,
        raw_storage=False,
    ),
    # §16.1: confirmed cleanup tombstone, 30 days; no copied content.
    DataClass.CLEANUP_TOMBSTONE: RetentionPolicy(
        data_class=DataClass.CLEANUP_TOMBSTONE,
        retention=timedelta(days=30),
        raw_storage=True,
    ),
}


@dataclass(frozen=True)
class AgentQuotas:
    """Bounded per-binding quotas (§9/§10/§11 limits, §20 privacy bounds).

    All values must be positive and finite; per-binding defaults are bounded so
    an Agent Binding cannot grow unbounded terminal content, inbox, or cost.
    """

    max_inbox_items: int
    max_concurrent_watches: int
    max_concurrent_runs: int
    max_output_bytes_per_excerpt: int
    max_token_bytes: int
    max_run_cost: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "max_inbox_items",
            "max_concurrent_watches",
            "max_concurrent_runs",
            "max_output_bytes_per_excerpt",
            "max_token_bytes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        try:
            cost = Decimal(self.max_run_cost)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError("max_run_cost must be a finite positive Decimal") from exc
        if not cost.is_finite() or cost <= 0:
            raise ValueError("max_run_cost must be a finite positive Decimal")


QUOTAS = AgentQuotas(
    max_inbox_items=256,
    max_concurrent_watches=16,
    max_concurrent_runs=1,
    max_output_bytes_per_excerpt=64 * KIB,
    max_token_bytes=32 * KIB,
    max_run_cost=Decimal("25.00"),
)


class CursorScope(StrEnum):
    """Opaque Agent cursor scope (§13.1): global or explicitly conversation-scoped."""

    GLOBAL = "global"
    CONVERSATION = "conversation"


@dataclass(frozen=True)
class CursorResetMarker:
    """Snapshot/reset marker returned instead of silently skipping events."""

    epoch: int
    scope: CursorScope


@dataclass(frozen=True)
class AgentCursorPolicy:
    """Opaque cursor policy with an epoch/reset component (§13.1).

    B may assign the cursor from SQLite commit order internally, but storage
    details are never exposed to C. When retention/deletion makes a client
    cursor too old, the stream returns a snapshot/reset marker rather than
    silently skipping events.
    """

    scope: CursorScope
    epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CursorScope):
            raise ValueError(f"unknown cursor scope: {self.scope!r}")
        if self.epoch < 0:
            raise ValueError("cursor epoch must be non-negative")

    @property
    def has_epoch_reset_component(self) -> bool:
        return True

    def is_too_old(self, cursor_epoch: int) -> bool:
        return cursor_epoch < self.epoch

    def reset_marker(self) -> CursorResetMarker:
        return CursorResetMarker(epoch=self.epoch, scope=self.scope)

    def resolve(self, cursor_epoch: int | None) -> CursorResetMarker | int:
        """Resolve a client cursor: too old (or absent) yields a reset marker."""
        if cursor_epoch is None or self.is_too_old(cursor_epoch):
            return self.reset_marker()
        return cursor_epoch


@dataclass(frozen=True)
class AgentAdmissionSequence:
    """Per-conversation admission sequence (§4.2).

    ``seq`` is assigned by the B database transaction at insert time, never by a
    client clock; per-conversation insertion and delivery order is deterministic.
    """

    conversation_id: UUID
    seq: int

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise ValueError("admission_seq must be non-negative")


def validate_admission_order(sequences: Sequence[AgentAdmissionSequence]) -> None:
    """Enforce strictly increasing, per-conversation admission ordering."""
    per_conversation: dict[UUID, list[AgentAdmissionSequence]] = {}
    for sequence in sequences:
        per_conversation.setdefault(sequence.conversation_id, []).append(sequence)
    for conversation_id, items in per_conversation.items():
        previous = -1
        for item in items:
            if item.seq <= previous:
                raise ValueError(
                    f"admission sequences for conversation {conversation_id} "
                    "must be strictly increasing"
                )
            previous = item.seq


class StartupStep(StrEnum):
    """One step of the ordered B-startup fencing sequence (§17)."""

    DB_INTEGRITY_AND_MIGRATION = "db_integrity_and_migration"
    RUNTIME_EPOCH_AND_STALE_FENCE = "runtime_epoch_and_stale_fence"
    RECONNECT_A_TOPOLOGY = "reconnect_a_topology"
    RECONNECT_BACKEND_SSE = "reconnect_backend_sse"
    REBUILD_WATCH_CURSORS = "rebuild_watch_cursors"
    DISPATCH_PENDING_INBOX = "dispatch_pending_inbox"


class StartupFencingOrder:
    """Ordered startup fencing contract (§17 B-restart row).

    Recover in order: DB integrity/migration; increment runtime epoch and fence
    stale runs/claims; reconnect/reconcile A topology; reconnect backend/SSE and
    reconcile nonterminal sessions/runs; rebuild watch cursors/deadlines; only
    then dispatch the pending Inbox. The steps cannot be reordered and the first
    and last steps are mandatory.
    """

    REQUIRED_ORDER: tuple[StartupStep, ...] = (
        StartupStep.DB_INTEGRITY_AND_MIGRATION,
        StartupStep.RUNTIME_EPOCH_AND_STALE_FENCE,
        StartupStep.RECONNECT_A_TOPOLOGY,
        StartupStep.RECONNECT_BACKEND_SSE,
        StartupStep.REBUILD_WATCH_CURSORS,
        StartupStep.DISPATCH_PENDING_INBOX,
    )

    @classmethod
    def validate(cls, steps: Sequence[StartupStep]) -> None:
        if not steps:
            raise ValueError("startup fencing order must not be empty")
        for step in steps:
            if not isinstance(step, StartupStep):
                raise ValueError(f"unknown startup step: {step!r}")
        if steps[0] is not cls.REQUIRED_ORDER[0]:
            raise ValueError("startup fencing must begin with DB integrity/migration")
        if steps[-1] is not cls.REQUIRED_ORDER[-1]:
            raise ValueError("startup fencing must end with dispatching the pending Inbox")
        position = -1
        for step in steps:
            index = cls.REQUIRED_ORDER.index(step)
            if index <= position:
                raise ValueError("startup fencing steps are reordered or duplicated")
            position = index


#: Version of the external-disclosure policy the release ships. Any other value
#: (including ``None``) is an unknown or drifted policy and disables the binding.
CURRENT_EXTERNAL_DISCLOSURE_POLICY_VERSION = "2026-08-01"


@dataclass(frozen=True)
class ExternalProviderDisclosure:
    """External provider disclosure required before enabling a binding (§16, §20).

    Recording endpoint, model, region, retention/no-training terms, and user
    consent must all be present; an incomplete, unknown, or drifted policy
    disables the binding (fail closed). ``region`` and ``retention_terms`` are
    ``None`` by default so a missing disclosure is visibly incomplete.
    """

    endpoint: str = ""
    model: str = ""
    region: str | None = None
    retention_terms: str | None = None
    no_training: bool = False
    user_consent: bool = False
    policy_version: str | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.endpoint and self.model and self.region and self.retention_terms)

    def binding_enabled(self) -> bool:
        return (
            self.is_complete
            and self.no_training
            and self.user_consent
            and self.policy_version == CURRENT_EXTERNAL_DISCLOSURE_POLICY_VERSION
        )
