"""Metadata-only approval lifecycle auditor (plan §12.1, task M5.2).

:class:`ApprovalAuditWriter` persists one row per successful approval
lifecycle transition into ``approval_audit_events`` (migration 0008).  Agent
identity is binding + instance (the Term) + runtime epoch + run id; approval
context is approval id + tool call id + canonical hash + auth epoch +
pane/operation/input byte count.  Raw text/keys never reach the audit table:
only the bounded byte count does (``raw_storage=False``, ``redacted=True``,
``APPROVAL_AUDIT_METADATA`` 90-day retention contract).

The writer is injected into :class:`ApprovalPolicy` by the composition root
and shared with the approval REST API and the CommandService; audit
recording is best-effort (a failure is logged, never allowed to break the
approval workflow).
"""

from __future__ import annotations

import logging

from termflow_control_plane.persistence.models import ApprovalRequest
from termflow_control_plane.persistence.repositories import RepositoryBundle

logger = logging.getLogger(__name__)

#: The closed set of lifecycle event types (spec §7).
EVENT_CREATED = "created"
EVENT_DECIDED = "decided"
EVENT_REVOKED = "revoked"
EVENT_CONSUMED = "consumed"
EVENT_UNKNOWN = "unknown"
EVENT_EXPIRED = "expired"


class ApprovalAuditWriter:
    """Persists approval lifecycle metadata; never raw text/keys."""

    def __init__(self, repositories: RepositoryBundle) -> None:
        self._repositories = repositories

    async def record(
        self,
        *,
        event_type: str,
        approval: ApprovalRequest,
        actor: str | None = None,
        outcome: str | None = None,
        error_code: str | None = None,
        input_bytes: int | None = None,
    ) -> None:
        """Record one event row derived from the approval row.

        ``outcome``/``error_code`` carry the A-side write result of the
        ``consumed``/``unknown`` transitions (metadata only, the CAS
        semantics are untouched); ``input_bytes`` is the bounded byte count
        of the write (never its content).
        """
        binding = await self._repositories.agent_bindings.get_by_id(approval.binding_id)
        await self._repositories.approval_audit.create(
            event_type=event_type,
            approval_id=approval.id,
            binding_id=approval.binding_id,
            instance_id=binding.term_id if binding is not None else None,
            runtime_epoch=binding.runtime_epoch if binding is not None else None,
            conversation_id=approval.conversation_id,
            run_id=approval.run_id,
            tool_call_id=approval.tool_call_id,
            pane_id=approval.pane_id,
            operation=approval.operation,
            input_bytes=input_bytes,
            canonical_hash=approval.canonical_hash,
            auth_epoch=approval.auth_epoch,
            actor=actor,
            outcome=outcome,
            error_code=error_code,
        )
