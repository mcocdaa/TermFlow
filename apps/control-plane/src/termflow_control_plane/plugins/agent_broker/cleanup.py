"""Durable cleanup-manifest orchestration.

The coordinator is deliberately independent of Docker or provider clients. B
owns the manifest and its rows; deployment-owned systems can only complete a
receipt through the narrow confirmation seam exposed by the API layer.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select

from termflow_control_plane.persistence.models import AgentCleanupJob, AgentCleanupReceipt
from termflow_control_plane.persistence.repositories import RepositoryBundle

CleanupReceiptState = Literal["pending", "confirmed", "not_applicable", "dead_letter"]
_RECEIPT_STATES = frozenset({"pending", "confirmed", "not_applicable", "dead_letter"})
_RETRY_BACKOFF = timedelta(minutes=5)
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_KIND = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass(frozen=True, slots=True)
class CleanupReceiptSeed:
    artifact_kind: str
    artifact_ref: str
    state: Literal["pending", "not_applicable"] = "pending"
    policy_reason: str | None = None
    policy_version: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupJobResult:
    job_id: UUID
    state: str
    receipts: tuple[AgentCleanupReceipt, ...]
    reason_code: str | None = None


CleanupHandler = Callable[..., object]


def _safe_text(value: object, *, limit: int = 512) -> str:
    """Bound handler errors and remove obvious credential-shaped fragments."""

    text = str(value).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = re.sub(
        r"(?i)(authorization|bearer|token|api[_-]?key|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    return text[:limit] or "cleanup handler failed"


def validate_artifact_ref(value: str) -> str:
    """Validate an opaque reference without accepting credentials/payloads."""

    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("artifact reference must be a bounded non-empty string")
    if value != value.strip() or any(ord(char) < 0x20 for char in value):
        raise ValueError("artifact reference contains invalid whitespace")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value) is None:
        raise ValueError("artifact reference must be an opaque identifier")
    lowered = value.lower()
    if any(
        marker in lowered
        for marker in ("authorization:", "bearer ", "api_key", "api-key", "password=", "secret=")
    ):
        raise ValueError("artifact reference must not contain credentials")
    if "?" in value or "#" in value or "{" in value or "}" in value:
        raise ValueError("artifact reference must not contain query or payload data")
    # Userinfo in a URL is an unambiguous credential leak.  A plain opaque
    # identifier may still contain a colon, so only reject an at-sign here.
    if "@" in value:
        raise ValueError("artifact reference must not contain credentials")
    return value


def _validate_kind(value: str) -> str:
    if not isinstance(value, str) or _SAFE_KIND.fullmatch(value) is None:
        raise ValueError("artifact kind is invalid")
    return value


class AgentCleanupCoordinator:
    """Create manifests and advance receipts with aggregate-state CAS rules."""

    def __init__(
        self,
        repositories: RepositoryBundle,
        handlers: Mapping[str, CleanupHandler] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repositories = repositories
        self._handlers: Mapping[str, CleanupHandler] = handlers or {}
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _normalize_seed(seed: CleanupReceiptSeed | Mapping[str, object]) -> CleanupReceiptSeed:
        if isinstance(seed, CleanupReceiptSeed):
            value = seed
        elif isinstance(seed, Mapping):
            try:
                value = CleanupReceiptSeed(
                    artifact_kind=cast(str, seed["artifact_kind"]),
                    artifact_ref=cast(str, seed["artifact_ref"]),
                    state=cast(Literal["pending", "not_applicable"], seed.get("state", "pending")),
                    policy_reason=cast(str | None, seed.get("policy_reason")),
                    policy_version=cast(str | None, seed.get("policy_version")),
                )
            except (KeyError, TypeError) as exc:
                raise ValueError("invalid cleanup receipt seed") from exc
        else:
            raise ValueError("invalid cleanup receipt seed")
        _validate_kind(value.artifact_kind)
        validate_artifact_ref(value.artifact_ref)
        if value.state not in {"pending", "not_applicable"}:
            raise ValueError("new cleanup receipts must be pending or not_applicable")
        if value.state == "not_applicable" and (
            not value.policy_reason
            or not value.policy_reason.strip()
            or not value.policy_version
            or not value.policy_version.strip()
        ):
            raise ValueError("not_applicable receipts require policy reason and version")
        if value.policy_reason is not None and len(value.policy_reason) > 128:
            raise ValueError("cleanup policy reason is too long")
        if value.policy_version is not None and len(value.policy_version) > 64:
            raise ValueError("cleanup policy version is too long")
        return value

    async def create_or_get_manifest(
        self,
        *,
        target_kind: str,
        target_ref: str,
        receipts: Sequence[CleanupReceiptSeed | Mapping[str, object]],
        term_id: UUID | None = None,
        installation_id: UUID | None = None,
        manifest_version: int = 1,
    ) -> AgentCleanupJob:
        _validate_kind(target_kind)
        validate_artifact_ref(target_ref)
        if (
            not isinstance(manifest_version, int)
            or isinstance(manifest_version, bool)
            or manifest_version < 1
        ):
            raise ValueError("manifest version must be a positive integer")
        normalized = [self._normalize_seed(seed) for seed in receipts]
        if not normalized:
            raise ValueError("cleanup manifest requires at least one receipt")
        keys = [(seed.artifact_kind, seed.artifact_ref) for seed in normalized]
        if len(set(keys)) != len(keys):
            raise ValueError("cleanup receipt entries must be unique")
        values: list[dict[str, object]] = []
        now = self._now()
        for seed in normalized:
            values.append(
                {
                    "artifact_kind": seed.artifact_kind,
                    "artifact_ref": seed.artifact_ref,
                    "state": seed.state,
                    "attempt_count": 0,
                    "last_error": None,
                    "next_attempt_at": None,
                    "evidence_digest": None,
                    "policy_reason": seed.policy_reason,
                    "policy_version": seed.policy_version,
                    "confirmed_at": now if seed.state == "not_applicable" else None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        job, owner = await self._repositories.cleanup_jobs.create_manifest(
            target_kind=target_kind,
            target_ref=target_ref,
            receipts=values,
            term_id=term_id,
            installation_id=installation_id,
            manifest_version=manifest_version,
        )
        if not owner and job.manifest_version != manifest_version:
            raise ValueError("cleanup manifest version conflicts with existing job")
        return job

    async def get_job(self, job_id: UUID) -> AgentCleanupJob | None:
        job = await self._repositories.cleanup_jobs.refresh_state(job_id, now=self._now())
        return job

    async def _invoke_handler(
        self,
        handler: CleanupHandler,
        job: AgentCleanupJob,
        receipt: AgentCleanupReceipt,
    ) -> object:
        try:
            signature = inspect.signature(handler)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError):
            arity = 2
        else:
            arity = len(positional)
        if arity >= 2:
            result = handler(job, receipt)
        elif arity == 1:
            result = handler(receipt)
        else:
            result = handler()
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _evidence_for(job: AgentCleanupJob, receipt: AgentCleanupReceipt, result: object) -> str:
        if isinstance(result, str) and _HEX_DIGEST.fullmatch(result):
            return result
        if isinstance(result, bytes):
            return hashlib.sha256(result).hexdigest()
        return hashlib.sha256(
            f"cleanup:{job.id}:{receipt.artifact_kind}:{receipt.artifact_ref}".encode()
        ).hexdigest()

    async def process_due_receipts(
        self,
        job_id: UUID,
        now: datetime | None = None,
    ) -> CleanupJobResult:
        observed = (now or self._now()).astimezone(UTC)
        job = await self._repositories.cleanup_jobs.get(job_id)
        if job is None:
            raise KeyError(f"cleanup job {job_id} not found")
        receipts = await self._repositories.cleanup_jobs.list_receipts(job.id)
        for receipt in receipts:
            if receipt.state != "pending":
                continue
            if receipt.next_attempt_at is not None and receipt.next_attempt_at > observed:
                continue
            handler = self._handlers.get(receipt.artifact_kind)
            if handler is None and receipt.artifact_kind == job.target_kind:
                # A target-level B handler is a compatibility seam for old
                # tombstones; a missing handler remains retryable pending.
                handler = self._handlers.get(job.target_kind)
            if handler is None:
                await self._repositories.cleanup_jobs.mark_receipt_attempt(
                    receipt.id,
                    next_attempt_at=observed + _RETRY_BACKOFF,
                    last_error="no cleanup handler registered",
                    now=observed,
                )
                continue
            try:
                outcome = await self._invoke_handler(handler, job, receipt)
                if outcome is False:
                    raise RuntimeError("cleanup handler did not confirm receipt")
                await self._repositories.cleanup_jobs.mark_receipt_confirmed(
                    receipt.id,
                    evidence_digest=self._evidence_for(job, receipt, outcome),
                    now=observed,
                )
            except Exception as exc:
                await self._repositories.cleanup_jobs.mark_receipt_attempt(
                    receipt.id,
                    next_attempt_at=observed + _RETRY_BACKOFF,
                    last_error=_safe_text(exc),
                    now=observed,
                )
        refreshed = await self._repositories.cleanup_jobs.refresh_state(job.id, now=observed)
        if refreshed is None:
            raise KeyError(f"cleanup job {job_id} not found")
        return CleanupJobResult(
            job_id=refreshed.id,
            state=refreshed.state,
            receipts=tuple(await self._repositories.cleanup_jobs.list_receipts(refreshed.id)),
            reason_code=refreshed.last_error,
        )

    async def confirm_receipt(
        self,
        receipt_id: UUID,
        artifact_ref: str,
        evidence_digest: str,
    ) -> AgentCleanupReceipt:
        validate_artifact_ref(artifact_ref)
        if _HEX_DIGEST.fullmatch(evidence_digest) is None:
            raise ValueError("evidence digest must be 64 lowercase hex characters")
        receipts = self._repositories.cleanup_jobs
        # RepositoryBundle has no direct receipt lookup by id; scan the small
        # manifest set through a direct session when needed.
        async with self._repositories._sessions() as session:
            receipt = await session.get(AgentCleanupReceipt, receipt_id)
            if receipt is None:
                raise KeyError(f"cleanup receipt {receipt_id} not found")
            if receipt.artifact_ref != artifact_ref:
                raise ValueError("artifact reference does not match receipt")
            if receipt.state == "confirmed":
                if receipt.evidence_digest != evidence_digest:
                    raise ValueError("receipt evidence conflicts with prior confirmation")
                return receipt
        confirmed = await receipts.mark_receipt_confirmed(
            receipt_id,
            evidence_digest=evidence_digest,
            now=self._now(),
        )
        if confirmed is None:
            raise KeyError(f"cleanup receipt {receipt_id} not found")
        return confirmed


async def create_deletion_manifest(
    repositories: RepositoryBundle,
    *,
    target_kind: str,
    target_id: UUID,
    term_ids: Sequence[UUID] = (),
) -> AgentCleanupJob:
    """Snapshot every B and deployment-owned artifact before parent cascade.

    The manifest vocabulary is intentionally explicit.  A missing artifact is
    represented by a policy-bearing ``not_applicable`` receipt; it is never
    silently omitted (which would make a manifest appear complete) and a
    missing helper handler remains retryable ``pending``.
    """
    from termflow_control_plane.persistence.models import (
        AgentBinding,
        AgentConversation,
        AgentEvent,
        AgentInboxItem,
        AgentMessage,
        AgentRun,
        AgentRuntimeBinding,
        AgentToken,
        AgentToolRequest,
        ApprovalRequest,
        BackendConversationRef,
        PanePolicy,
        Watch,
    )

    existing = await repositories.cleanup_jobs.get_by_target(
        target_kind=target_kind, target_ref=str(target_id)
    )
    if existing is not None:
        return existing
    seeds: list[CleanupReceiptSeed] = [
        CleanupReceiptSeed(
            target_kind,
            str(target_id),
            policy_reason="b_owned",
            policy_version="v0.2.0",
        )
    ]

    def add_b(kind: str, ref: object) -> None:
        seeds.append(
            CleanupReceiptSeed(
                kind,
                validate_artifact_ref(str(ref)),
                policy_reason="b_owned",
                policy_version="v0.2.0",
            )
        )

    def add_external(kind: str, ref: object) -> None:
        seeds.append(
            CleanupReceiptSeed(
                kind,
                validate_artifact_ref(str(ref)),
                policy_reason="deployment_owned",
                policy_version="v0.2.0",
            )
        )

    def add_not_applicable(kind: str, ref: object, *, reason: str) -> None:
        seeds.append(
            CleanupReceiptSeed(
                kind,
                validate_artifact_ref(str(ref)),
                state="not_applicable",
                policy_reason=reason,
                policy_version="v0.2.0",
            )
        )

    async with repositories._sessions() as session:
        query = select(AgentBinding)
        if target_kind == "binding":
            query = query.where(AgentBinding.id == target_id)
        elif target_kind == "profile":
            query = query.where(AgentBinding.profile_id == target_id)
        elif target_kind == "conversation":
            query = query.join(
                AgentConversation, AgentConversation.binding_id == AgentBinding.id
            ).where(AgentConversation.id == target_id)
        else:
            query = query.where(AgentBinding.term_id.in_(term_ids))
        bindings = list(await session.scalars(query))
        seen_b_categories: set[str] = set()
        seen_external_categories: set[str] = set()
        for binding in bindings:
            binding_scope = target_kind != "conversation"
            if binding_scope:
                add_b("b_row", f"agent_binding:{binding.id}")
            observed = await session.scalar(
                select(AgentRuntimeBinding).where(AgentRuntimeBinding.binding_id == binding.id)
            )
            # B-owned rows are captured individually where they have a durable
            # identity.  Target handlers may confirm these after the parent
            # cascade, while an unregistered handler can never auto-complete.
            conversations = list(
                await session.scalars(
                    select(AgentConversation).where(AgentConversation.binding_id == binding.id)
                )
            )
            if target_kind == "conversation":
                conversations = [row for row in conversations if row.id == target_id]
            for conversation in conversations:
                add_b("b_row", f"agent_conversation:{conversation.id}")
                for row_id in await session.scalars(
                    select(AgentEvent.id).where(AgentEvent.conversation_id == conversation.id)
                ):
                    add_b("b_row", f"agent_event:{row_id}")
                for row_id in await session.scalars(
                    select(AgentMessage.id).where(AgentMessage.conversation_id == conversation.id)
                ):
                    add_b("b_row", f"agent_message:{row_id}")
                for row_id in await session.scalars(
                    select(AgentInboxItem.id).where(
                        AgentInboxItem.conversation_id == conversation.id
                    )
                ):
                    add_b("b_row", f"agent_inbox:{row_id}")
                for row_id in await session.scalars(
                    select(AgentRun.id).where(AgentRun.conversation_id == conversation.id)
                ):
                    add_b("b_row", f"agent_run:{row_id}")
                tool_query = select(AgentToolRequest).where(
                    AgentToolRequest.binding_id == binding.id
                )
                if target_kind == "conversation":
                    tool_query = tool_query.join(
                        AgentRun,
                        AgentToolRequest.run_id == AgentRun.id,
                    ).where(AgentRun.conversation_id == conversation.id)
                tool_rows = list(await session.scalars(tool_query))
                for row in tool_rows:
                    add_b("b_row", f"agent_tool_request:{row.id}")
            watch_predicate = (
                Watch.conversation_id == target_id
                if target_kind == "conversation"
                else Watch.binding_id == binding.id
            )
            watch_ids = list(await session.scalars(select(Watch.id).where(watch_predicate)))
            for row_id in watch_ids:
                add_b("watch", f"watch:{row_id}")
            if watch_ids:
                seen_b_categories.add("watch")
            approval_predicate = (
                ApprovalRequest.conversation_id == target_id
                if target_kind == "conversation"
                else ApprovalRequest.binding_id == binding.id
            )
            approval_ids = list(
                await session.scalars(select(ApprovalRequest.id).where(approval_predicate))
            )
            for row_id in approval_ids:
                add_b("approval", f"approval:{row_id}")
            if approval_ids:
                seen_b_categories.add("approval")
            if binding_scope:
                token_ids = list(
                    await session.scalars(
                        select(AgentToken.id).where(AgentToken.binding_id == binding.id)
                    )
                )
                for row_id in token_ids:
                    add_b("agent_token", f"agent_token:{row_id}")
                if token_ids:
                    seen_b_categories.add("agent_token")
                for row_id in await session.scalars(
                    select(PanePolicy.id).where(PanePolicy.binding_id == binding.id)
                ):
                    add_b("b_row", f"pane_policy:{row_id}")

            if binding_scope:
                runtime_refs = {
                    binding.runtime_ref,
                    observed.observed_runtime_ref if observed else None,
                } - {None}
                for runtime_ref in runtime_refs:
                    for kind in ("runtime_attestation", "runtime_volume", "container_log"):
                        add_external(kind, runtime_ref)
                        seen_external_categories.add(kind)
            refs = (
                select(BackendConversationRef)
                .join(
                    AgentConversation,
                    BackendConversationRef.conversation_id == AgentConversation.id,
                )
                .where(AgentConversation.binding_id == binding.id)
            )
            if target_kind == "conversation":
                refs = refs.where(AgentConversation.id == target_id)
            for ref in await session.scalars(refs):
                # Parent cascades remove the B session rows: external cleanup
                # is driven by B through its backend adapter and confirmed only
                # after that call proves deletion.
                add_b("backend_session", ref.provider_ref)
                seen_b_categories.add("backend_session")
                seen_external_categories.add("backend_session")
                add_external("provider_retention", f"provider:{ref.provider_ref}")
                seen_external_categories.add("provider_retention")

        # Keep category coverage explicit even when no corresponding row or
        # runtime exists.  These policy decisions are auditable and do not
        # grant a missing deployment helper permission to complete anything.
        target_ref = f"{target_kind}:{target_id}"
        for category, reason in (
            ("b_row", "no_b_rows"),
            ("watch", "no_watches"),
            ("approval", "no_approvals"),
            ("agent_token", "no_agent_tokens"),
            ("backend_session", "no_backend_sessions"),
        ):
            if category != "b_row" and category not in seen_b_categories:
                add_not_applicable(category, f"{category}:{target_ref}", reason=reason)
            elif category == "b_row" and not bindings:
                add_not_applicable(category, f"{category}:{target_ref}", reason=reason)
        for category, reason in (
            ("runtime_attestation", "no_runtime_attestation"),
            ("runtime_volume", "no_runtime_volume"),
            ("container_log", "no_container_log"),
            ("provider_retention", "no_provider_retention"),
        ):
            if category not in seen_external_categories:
                add_not_applicable(category, f"{category}:{target_ref}", reason=reason)
        # Only parent Term/installation deletion has a database-backup scope.
        # Narrower Agent rows share the same DB and carry an explicit N/A policy
        # rather than inventing a path or silently omitting the category.
        if target_kind in {"term", "installation"}:
            add_external("sqlite_wal_backup", f"sqlite:{target_ref}")
        else:
            add_not_applicable(
                "sqlite_wal_backup",
                f"sqlite:{target_ref}",
                reason="target_has_no_database_backup_scope",
            )
    unique = {(seed.artifact_kind, seed.artifact_ref): seed for seed in seeds}
    return await AgentCleanupCoordinator(repositories).create_or_get_manifest(
        target_kind=target_kind,
        target_ref=str(target_id),
        receipts=list(unique.values()),
        term_id=target_id if target_kind == "term" else None,
        installation_id=target_id if target_kind == "installation" else None,
    )
