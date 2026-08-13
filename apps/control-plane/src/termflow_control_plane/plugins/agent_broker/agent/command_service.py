"""Approval-gated pane write orchestration (plan §12.1, task M5.2).

:class:`CommandService` implements :class:`TerminalCommandPort`: every write
tool call becomes a persistent single-use approval, waits synchronously for
the human decision (DB polling), rechecks the target before executing, and
settles the approval from the A-side outcome.  The flow (spec §1):

.. code-block:: text

    execute(principal, tool_call_id, params)
      -> create pending approval (hash bound to the exact write)
      -> poll for a decision (deadline = approval_wait_timeout_seconds)
         approved -> preflight rechecks -> send -> settle
         denied   -> approval_denied
         revoked / expired -> approval_revoked / approval_expired
         wait timeout      -> revoke(pending, system:tool_timeout)
                              -> approval_required + approval_id
      finally: a still-pending approval is revoked best-effort

The preflight rechecks (spec §4) run in order: auth epoch, expiry, binding
state, and a **recomputed canonical hash against the current ledger**.  The
recomputation uses the approval row's stored ``expires_at`` (frozen at
creation) as the hash ``expiry`` - a fresh ``now + ttl`` would never match
the stored hash for an approval decided late in its TTL window.

Settling (spec §4): ``ok`` -> ``consume``; known failure -> ``consume``;
uncertain outcome (``OutcomeUnknownError``) -> ``mark_unknown``.  A settle
CAS that loses to a concurrent revoke changes nothing but the factual
receipt is still returned: the write happened.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from termflow_protocol import CommandResultPayload
from termflow_protocol.keys import NAMED_KEYS, canonical_key_bytes
from termflow_protocol.mcp import (
    PaneSendKeysParams,
    PaneSendKeysResult,
    PaneSendTextParams,
    PaneSendTextResult,
    TermFlowErrorCode,
)

from termflow_control_plane.auth.epoch import persisted_authentication_epoch
from termflow_control_plane.connections.registry import CapabilityUnavailable
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import ApprovalRequest
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.approval_audit import (
    ApprovalAuditWriter,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import (
    ApprovalArgsHashInput,
    ApprovalError,
    ApprovalPolicy,
    ApprovalState,
    ApprovalToolCallConflict,
    canonical_hash,
)
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    TermFlowToolError,
    TerminalObservationPort,
)
from termflow_control_plane.plugins.agent_broker.auth import AgentTokenPrincipal
from termflow_control_plane.routing.router import CommandRouter

logger = logging.getLogger(__name__)

#: Binding states under which an approved write may still execute (spec §4.3).
_ACTIVE_BINDING_STATES = frozenset({"pending", "ready"})

#: Wait states that exit the poll loop without executing (all but pending).
_FINAL_WAIT_STATES = frozenset(
    {
        ApprovalState.APPROVED,
        ApprovalState.DENIED,
        ApprovalState.REVOKED,
        ApprovalState.EXPIRED,
        ApprovalState.CONSUMED,
        ApprovalState.UNKNOWN,
    }
)


class OutcomeUnknownError(TermFlowToolError):
    """The write may have been sent but its outcome cannot be proven (M5.2).

    Raised by the :class:`CommandRouterGateway` for ``command_timeout`` and
    ``outcome_unknown`` failures; the service settles the approval as
    ``unknown`` (terminal, never automatically replayed).
    """

    def __init__(self, message: str) -> None:
        super().__init__(TermFlowErrorCode.OUTCOME_UNKNOWN, message)


class CommandGateway(Protocol):
    """The A-side send surface the service depends on (testable seam, spec §2)."""

    async def send_text(
        self,
        instance_id: UUID,
        pane_id: str,
        text: str,
        submit: bool,
        idempotency_key: UUID,
        pane_incarnation: int | None,
    ) -> CommandResultPayload: ...

    async def send_keys(
        self,
        instance_id: UUID,
        pane_id: str,
        keys: tuple[str, ...],
        idempotency_key: UUID,
        pane_incarnation: int | None,
    ) -> CommandResultPayload: ...


class CommandRouterGateway:
    """Production :class:`CommandGateway` over :class:`CommandRouter`.

    Every known router failure maps to a stable
    :class:`~termflow_protocol.mcp.TermFlowErrorCode` (spec §2): offline /
    topology / backpressure failures are transient ``INTERNAL_ERROR``, a
    missing pane is ``PANE_NOT_FOUND``, A-side receipt codes translate
    through the stable table, an unnegotiated ``typed_keys`` capability is
    ``POLICY_DENIED``, and the two unprovable outcomes surface as
    :class:`OutcomeUnknownError`.
    """

    #: A-side receipt ``error_code`` values mapped to stable tool codes
    #: (spec §2, mirroring the observation ``_WIRE_ERROR_CODES`` pattern).
    _RECEIPT_ERROR_CODES: dict[str, TermFlowErrorCode] = {
        "pane_not_found": TermFlowErrorCode.PANE_NOT_FOUND,
        "invalid_key": TermFlowErrorCode.INVALID_REQUEST,
    }

    def __init__(self, router: CommandRouter) -> None:
        self._router = router

    async def send_text(
        self,
        instance_id: UUID,
        pane_id: str,
        text: str,
        submit: bool,
        idempotency_key: UUID,
        pane_incarnation: int | None,
    ) -> CommandResultPayload:
        try:
            return await self._router.send_input(
                instance_id,
                pane_id,
                text,
                submit,
                idempotency_key,
                pane_incarnation=pane_incarnation,
            )
        except TermFlowError as exc:
            raise self._map_error(exc) from exc

    async def send_keys(
        self,
        instance_id: UUID,
        pane_id: str,
        keys: tuple[str, ...],
        idempotency_key: UUID,
        pane_incarnation: int | None,
    ) -> CommandResultPayload:
        try:
            return await self._router.send_keys(
                instance_id,
                pane_id,
                keys,
                idempotency_key,
                pane_incarnation=pane_incarnation,
            )
        except TermFlowError as exc:
            raise self._map_error(exc) from exc
        except CapabilityUnavailable as exc:
            raise TermFlowToolError(
                TermFlowErrorCode.POLICY_DENIED,
                "the instance did not negotiate typed key input; "
                "key writes are refused (fail closed)",
            ) from exc

    @staticmethod
    def _map_error(exc: TermFlowError) -> TermFlowToolError:
        if exc.code in ("command_timeout", "outcome_unknown"):
            # The command may have been sent; the outcome cannot be proven.
            # The service settles the approval as ``unknown``.
            return OutcomeUnknownError(
                f"the write may have been sent but the outcome is unknown ({exc.code})"
            )
        code = CommandRouterGateway._RECEIPT_ERROR_CODES.get(
            exc.code, TermFlowErrorCode.INTERNAL_ERROR
        )
        return TermFlowToolError(code, exc.message or f"the command failed: {exc.code}")


@dataclass(frozen=True, slots=True)
class _WriteContext:
    """The exact write B would send, captured from the current ledger."""

    canonical_hash: str
    pane_incarnation: int | None
    input_bytes: int


class CommandService:
    """Concrete approval-gated :class:`TerminalCommandPort` (spec §2, §4)."""

    def __init__(
        self,
        *,
        repositories: RepositoryBundle,
        sessions: async_sessionmaker[AsyncSession],
        gateway: CommandGateway,
        observation: TerminalObservationPort,
        clock: Callable[[], datetime] | None = None,
        approval_wait_timeout_seconds: float = 25.0,
        approval_ttl_seconds: float = 300.0,
        poll_interval_seconds: float = 0.5,
        audit: ApprovalAuditWriter | None = None,
        policy: ApprovalPolicy | None = None,
    ) -> None:
        self._repositories = repositories
        self._sessions = sessions
        self._gateway = gateway
        self._observation = observation
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wait_timeout = approval_wait_timeout_seconds
        self._ttl = approval_ttl_seconds
        self._poll_interval = poll_interval_seconds
        self._audit = audit
        if policy is None:
            policy = ApprovalPolicy(repositories, sessions, clock=self._clock, audit=audit)
        self.policy = policy

    async def send_text(
        self,
        principal: AgentTokenPrincipal,
        params: PaneSendTextParams,
        *,
        tool_call_id: str,
    ) -> PaneSendTextResult:
        return cast(
            PaneSendTextResult,
            await self._execute(principal, params, tool_call_id, operation="send_text"),
        )

    async def send_keys(
        self,
        principal: AgentTokenPrincipal,
        params: PaneSendKeysParams,
        *,
        tool_call_id: str,
    ) -> PaneSendKeysResult:
        return cast(
            PaneSendKeysResult,
            await self._execute(principal, params, tool_call_id, operation="send_keys"),
        )

    # ------------------------------------------------------------------
    # main flow
    # ------------------------------------------------------------------

    async def _execute(
        self,
        principal: AgentTokenPrincipal,
        params: PaneSendTextParams | PaneSendKeysParams,
        tool_call_id: str,
        *,
        operation: str,
    ) -> PaneSendTextResult | PaneSendKeysResult:
        # B-side early rejection: unknown named keys never reach the
        # approval flow (spec §2: vocabulary check fails closed).
        if operation == "send_keys":
            keys = params.keys  # type: ignore[union-attr]
            unknown = [key for key in keys if key not in NAMED_KEYS]
            if unknown:
                raise TermFlowToolError(
                    TermFlowErrorCode.INVALID_REQUEST,
                    f"unknown named keys: {sorted(unknown)}",
                )

        observed = self._clock()
        expiry = observed + timedelta(seconds=self._ttl)
        approval = await self._create_approval(
            principal, params, tool_call_id=tool_call_id, operation=operation, expiry=expiry
        )
        try:
            deadline = observed + timedelta(seconds=self._wait_timeout)
            state = await self._wait_for_decision(approval.id, deadline)
            if state is not ApprovalState.APPROVED:
                raise self._decision_failure(approval.id, state)
            incarnation = await self._preflight(approval, principal, params, operation)
            final = await self._repositories.approvals.get_by_id(approval.id)
            if final is None or final.state != ApprovalState.APPROVED.value:
                state = ApprovalState(final.state) if final is not None else ApprovalState.REVOKED
                logger.warning(
                    "approval %s changed state (%s) between preflight and send; not sending",
                    approval.id,
                    state,
                )
                raise self._decision_failure(approval.id, state)
            return await self._send_and_settle(
                approval, principal, params, operation, incarnation
            )
        finally:
            # Any non-execution exit with the approval still pending revokes
            # it best-effort (guard timeout cancellation, internal error).
            await self._revoke_pending_if_any(approval.id)

    async def _create_approval(
        self,
        principal: AgentTokenPrincipal,
        params: PaneSendTextParams | PaneSendKeysParams,
        *,
        tool_call_id: str,
        operation: str,
        expiry: datetime,
    ) -> ApprovalRequest:
        context = await self._write_context(principal, params, operation, expiry=expiry)
        try:
            return await self.policy.create_approval(
                binding_id=principal.binding_id,
                conversation_id=params.conversation_id,
                tool_call_id=tool_call_id,
                canonical_hash=context.canonical_hash,
                auth_epoch=await persisted_authentication_epoch(self._repositories),
                expires_at=expiry,
                run_id=await self._current_run_id(params.conversation_id),
                pane_id=params.pane_id,
                operation=operation,
                intent_summary=params.intent,
                input_bytes=context.input_bytes,
            )
        except ApprovalToolCallConflict as exc:
            existing = await self._repositories.approvals.get_by_tool_call(
                params.conversation_id, tool_call_id
            )
            state = existing.state if existing is not None else "unknown"
            raise TermFlowToolError(
                TermFlowErrorCode.APPROVAL_CONFLICT,
                f"tool call {tool_call_id!r} already produced an approval "
                f"(state={state}); replay is rejected",
                data={
                    "approval_id": str(existing.id) if existing is not None else None,
                    "state": state,
                },
            ) from exc

    async def _wait_for_decision(self, approval_id: UUID, deadline: datetime) -> ApprovalState:
        """Poll the persisted approval until it leaves ``pending`` or times out."""
        while True:
            approval = await self._repositories.approvals.get_by_id(approval_id)
            if approval is None:
                raise TermFlowToolError(
                    TermFlowErrorCode.INTERNAL_ERROR,
                    f"approval {approval_id} disappeared while waiting",
                )
            state = ApprovalState(approval.state)
            if state in _FINAL_WAIT_STATES:
                return state
            if self._clock() >= deadline:
                # Waiting timed out: revoke the pending approval so a later
                # decision can never create an approved-but-unexecuted zombie
                # (spec §1 timeout semantics).
                try:
                    await self.policy.revoke(approval_id, actor="system:tool_timeout")
                except ApprovalError:
                    pass
                raise TermFlowToolError(
                    TermFlowErrorCode.APPROVAL_REQUIRED,
                    f"approval {approval_id} is still pending after "
                    f"{self._wait_timeout:.0f}s; the write was not executed",
                    data={"approval_id": str(approval_id)},
                )
            await asyncio.sleep(self._poll_interval)

    async def _preflight(
        self,
        approval: ApprovalRequest,
        principal: AgentTokenPrincipal,
        params: PaneSendTextParams | PaneSendKeysParams,
        operation: str,
    ) -> int | None:
        """Recheck the approved request against the world before sending (§4)."""
        # 1) The auth epoch must still match: auth resets invalidate every
        #    approval (fail closed).  The epoch read here is reused for the
        #    hash recomputation below so both rechecks observe one consistent
        #    world; a reset landing later is an in-flight race (disclosed).
        current_epoch = await persisted_authentication_epoch(self._repositories)
        if approval.auth_epoch != current_epoch:
            await self._best_effort_revoke(approval.id, actor="system:auth_epoch_changed")
            raise TermFlowToolError(
                TermFlowErrorCode.POLICY_DENIED,
                "the authentication epoch changed after the approval; "
                "the write was not executed",
            )
        # 2) The approval must not have expired (the sweep finishes the row).
        expires_at = self._aware(approval.expires_at)
        if expires_at is None or self._clock() >= expires_at:
            raise TermFlowToolError(
                TermFlowErrorCode.APPROVAL_EXPIRED,
                f"approval {approval.id} expired before execution",
                data={"approval_id": str(approval.id)},
            )
        # 3) The binding must still be active.
        binding = await self._repositories.agent_bindings.get_by_id(approval.binding_id)
        if binding is None or binding.status not in _ACTIVE_BINDING_STATES:
            await self._best_effort_revoke(approval.id, actor="system:target_changed")
            raise TermFlowToolError(
                TermFlowErrorCode.POLICY_DENIED,
                "the binding is no longer active; the write was not executed",
            )
        # 4) Recomputed hash against the CURRENT ledger must still match the
        #    stored hash; any drift (text, pane, incarnation, cursor, run,
        #    epoch) means the reviewed target changed.  The expiry used for
        #    the recomputation is the stored ``expires_at`` frozen at
        #    creation - never a fresh ``now + ttl`` - so an approval decided
        #    late in its TTL window still matches (spec §4.4 + M5.2 note).
        context = await self._write_context(
            principal, params, operation, expiry=expires_at, policy_epoch=current_epoch
        )
        if context.canonical_hash != approval.canonical_hash:
            await self._best_effort_revoke(approval.id, actor="system:target_changed")
            raise TermFlowToolError(
                TermFlowErrorCode.INCARNATION_CHANGED,
                "the pane target changed after the approval (cursor or "
                "incarnation drifted); re-read the pane and retry with a "
                "new tool call",
                data={"approval_id": str(approval.id)},
            )
        return context.pane_incarnation

    async def _send_and_settle(
        self,
        approval: ApprovalRequest,
        principal: AgentTokenPrincipal,
        params: PaneSendTextParams | PaneSendKeysParams,
        operation: str,
        pane_incarnation: int | None,
    ) -> PaneSendTextResult | PaneSendKeysResult:
        idempotency_key = uuid4()
        if operation == "send_text":
            input_bytes = len(params.text.encode("utf-8"))  # type: ignore[union-attr]
        else:
            input_bytes = len(canonical_key_bytes(params.keys))  # type: ignore[union-attr]
        try:
            if operation == "send_text":
                assert isinstance(params, PaneSendTextParams)
                await self._gateway.send_text(
                    principal.instance_id,
                    params.pane_id,
                    params.text,
                    params.submit,
                    idempotency_key,
                    pane_incarnation,
                )
            else:
                assert isinstance(params, PaneSendKeysParams)
                await self._gateway.send_keys(
                    principal.instance_id,
                    params.pane_id,
                    params.keys,
                    idempotency_key,
                    pane_incarnation,
                )
        except OutcomeUnknownError as exc:
            await self._settle_mark_unknown(
                approval.id,
                outcome="outcome_unknown",
                error_code="outcome_unknown",
                input_bytes=input_bytes,
            )
            raise TermFlowToolError(
                TermFlowErrorCode.OUTCOME_UNKNOWN,
                "the write may have been sent but the outcome is unknown",
                data={"approval_id": str(approval.id)},
            ) from exc
        except TermFlowToolError as exc:
            await self._settle_consume(
                approval.id,
                outcome="failed",
                error_code=exc.error_code.value,
                input_bytes=input_bytes,
            )
            raise
        await self._settle_consume(
            approval.id, outcome="confirmed", input_bytes=input_bytes
        )
        return self._result(params, operation, approval.id)

    def _result(
        self,
        params: PaneSendTextParams | PaneSendKeysParams,
        operation: str,
        approval_id: UUID,
    ) -> PaneSendTextResult | PaneSendKeysResult:
        result_cls = PaneSendTextResult if operation == "send_text" else PaneSendKeysResult
        return result_cls(
            request_key=params.request_key,
            ok=True,
            outcome="confirmed",
            approval_id=approval_id,
        )

    # ------------------------------------------------------------------
    # settle helpers
    # ------------------------------------------------------------------

    async def _settle_consume(
        self,
        approval_id: UUID,
        *,
        outcome: str | None = None,
        error_code: str | None = None,
        input_bytes: int | None = None,
    ) -> None:
        try:
            await self.policy.consume(
                approval_id,
                outcome=outcome,
                error_code=error_code,
                input_bytes=input_bytes,
            )
        except ApprovalError as exc:
            # A settle CAS that loses (e.g. to a concurrent human revoke)
            # changes nothing; the factual receipt is still returned because
            # the write did happen (spec §4 race disclosure).
            logger.warning("approval %s settle(consume) lost: %s", approval_id, exc)

    async def _settle_mark_unknown(
        self,
        approval_id: UUID,
        *,
        outcome: str | None = None,
        error_code: str | None = None,
        input_bytes: int | None = None,
    ) -> None:
        try:
            await self.policy.mark_unknown(
                approval_id,
                outcome=outcome,
                error_code=error_code,
                input_bytes=input_bytes,
            )
        except ApprovalError as exc:
            logger.warning("approval %s settle(mark_unknown) lost: %s", approval_id, exc)

    async def _revoke_pending_if_any(self, approval_id: UUID) -> None:
        try:
            approval = await self._repositories.approvals.get_by_id(approval_id)
            if approval is not None and approval.state == ApprovalState.PENDING.value:
                await self.policy.revoke(approval_id, actor="system:tool_timeout")
        except ApprovalError:
            pass

    async def _best_effort_revoke(self, approval_id: UUID, *, actor: str) -> None:
        try:
            await self.policy.revoke(approval_id, actor=actor)
        except ApprovalError:
            pass

    # ------------------------------------------------------------------
    # hash context
    # ------------------------------------------------------------------

    async def _write_context(
        self,
        principal: AgentTokenPrincipal,
        params: PaneSendTextParams | PaneSendKeysParams,
        operation: str,
        *,
        expiry: datetime,
        policy_epoch: int | None = None,
    ) -> _WriteContext:
        """Compute the canonical hash for the exact write (spec §3).

        ``expiry`` is the value bound into the hash: ``now + ttl`` at
        creation, the stored ``expires_at`` at preflight recheck (frozen).
        ``policy_epoch`` pins the auth epoch observed by the caller (the
        preflight reuses the epoch from recheck ① so both rechecks are
        consistent); when omitted the persisted epoch is read fresh.
        """
        if operation == "send_text":
            assert isinstance(params, PaneSendTextParams)
            encoded = params.text.encode("utf-8")
            submit = params.submit
            input_bytes = len(encoded)
        else:
            assert isinstance(params, PaneSendKeysParams)
            encoded = canonical_key_bytes(params.keys)
            submit = False
            input_bytes = len(encoded)
        cursor = await self._observation.resolve_cursor(principal.instance_id, params.pane_id)
        if cursor is None:
            pane_incarnation = "1"
            cursor_precondition = None
        else:
            pane_incarnation = str(cursor.pane_incarnation)
            cursor_precondition = f"{cursor.stream_id}:{cursor.seq}"
        return _WriteContext(
            canonical_hash=canonical_hash(
                ApprovalArgsHashInput(
                    schema_version=1,
                    operation=operation,
                    instance_id=principal.instance_id,
                    pane_id=params.pane_id,
                    pane_incarnation=pane_incarnation,
                    encoded_bytes=encoded,
                    submit=submit,
                    cursor_precondition=cursor_precondition,
                    run_id=await self._current_run_id(params.conversation_id),
                    grant_id=None,
                    expiry=expiry,
                    policy_epoch=(
                        policy_epoch
                        if policy_epoch is not None
                        else await persisted_authentication_epoch(self._repositories)
                    ),
                )
            ),
            pane_incarnation=cursor.pane_incarnation if cursor is not None else None,
            input_bytes=input_bytes,
        )

    async def _current_run_id(self, conversation_id: UUID) -> UUID | None:
        """The conversation's running run, if any (spec §3: ``run_id``)."""
        for run in await self._repositories.agent_runs.list_for_conversation(conversation_id):
            if run.run_state == "running":
                return run.id
        return None

    @staticmethod
    def _decision_failure(approval_id: UUID, state: ApprovalState) -> TermFlowToolError:
        data: dict[str, object] = {"approval_id": str(approval_id)}
        if state is ApprovalState.DENIED:
            return TermFlowToolError(
                TermFlowErrorCode.APPROVAL_DENIED,
                f"approval {approval_id} was denied; the write was not executed",
                data=data,
            )
        if state is ApprovalState.REVOKED:
            return TermFlowToolError(
                TermFlowErrorCode.APPROVAL_REVOKED,
                f"approval {approval_id} was revoked; the write was not executed",
                data=data,
            )
        if state is ApprovalState.EXPIRED:
            return TermFlowToolError(
                TermFlowErrorCode.APPROVAL_EXPIRED,
                f"approval {approval_id} expired; the write was not executed",
                data=data,
            )
        return TermFlowToolError(
            TermFlowErrorCode.APPROVAL_REVOKED,
            f"approval {approval_id} is in state {state.value}; the write was not executed",
            data=data,
        )

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)
