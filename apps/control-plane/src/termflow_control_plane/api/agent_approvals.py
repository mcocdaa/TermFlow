"""Product-facing Agent Broker approval request API (plan §12.1, task M5.1).

Endpoints under ``/api/v1/agent/approvals`` let an authenticated admin
session inspect pending assisted-write approvals, decide them once
(``approve``/``deny``), revoke them, and list them by conversation.

Decision semantics: :meth:`ApprovalPolicy.decide` is an atomic CAS bound to
the caller's auth epoch, so a stale UI replay, a double decision, an expired
request, or a revoked request can never win twice.  The caller's epoch is the
persisted authentication epoch that ``require_admin`` validated the session
against; if it no longer matches the epoch recorded when the request was
created, the decision is rejected with 409 ``approval_auth_epoch_stale``.

Display-safety: the response exposes only persisted metadata (identity
fields, the canonical arguments hash, state, expiry, timestamps, and the
target binding/Term).  The exact text/keys, pane, agent identity, and reason
are covered by the canonical hash but are not persisted as raw columns by the
M1.1 model; they surface with the tool-call context in M5.2+.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from termflow_protocol.agent import ApprovalDecision

from termflow_control_plane.api.dependencies import (
    get_repositories,
    get_session_factory,
    require_admin,
)
from termflow_control_plane.auth.epoch import persisted_authentication_epoch
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import (
    AgentBinding,
    ApprovalRequest,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.permissions import (
    ApprovalAlreadyConsumed,
    ApprovalAlreadyDecided,
    ApprovalAuthEpochStale,
    ApprovalError,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalPolicy,
    ApprovalRevoked,
)

router = APIRouter(
    prefix="/api/v1/agent/approvals",
    tags=["agent"],
    dependencies=[Depends(require_admin)],
)

#: The admin session store keeps no per-user identity, so M5.1 labels every
#: deciding actor ``admin``; precise identity capture lands with the M5.3
#: audit wiring.  ``decide`` still validates the actor label.
_ACTOR = "admin"


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: UUID
    binding_id: UUID
    conversation_id: UUID
    run_id: UUID | None
    tool_call_id: str
    canonical_hash: str
    state: str
    expires_at: datetime
    decided_at: datetime | None
    decision: str | None
    auth_epoch: int
    created_at: datetime
    # M5.2 display metadata for the M6 approval UI; legacy rows degrade to
    # None.  Raw text/keys are never exposed (canonical hash only).
    pane_id: str | None = None
    operation: str | None = None
    intent_summary: str | None = None


class ApprovalBindingInfo(BaseModel):
    """Product-visible binding facts; backend/runtime internals stay opaque."""

    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    profile_id: UUID
    term_id: UUID
    status: str


class ApprovalDetailResponse(ApprovalResponse):
    model_config = ConfigDict(extra="forbid")

    binding: ApprovalBindingInfo


class ApprovalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approvals: list[ApprovalResponse]


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny"]


def _approval_response(approval: ApprovalRequest) -> ApprovalResponse:
    return ApprovalResponse(
        approval_id=approval.id,
        binding_id=approval.binding_id,
        conversation_id=approval.conversation_id,
        run_id=approval.run_id,
        tool_call_id=approval.tool_call_id,
        canonical_hash=approval.canonical_hash,
        state=approval.state,
        expires_at=approval.expires_at,
        decided_at=approval.decided_at,
        decision=approval.decision,
        auth_epoch=approval.auth_epoch,
        created_at=approval.created_at,
        pane_id=approval.pane_id,
        operation=approval.operation,
        intent_summary=approval.intent_summary,
    )


def _shared_policy(
    request: Request,
    repositories: RepositoryBundle,
    sessions: async_sessionmaker[AsyncSession],
) -> ApprovalPolicy:
    """The composition root's shared policy (audit writer included).

    The M5.2 composition root puts one policy on ``app.state``; API paths
    fall back to a fresh policy (no audit writer) when the app does not
    provide one, e.g. in isolated test mounts.
    """
    shared = getattr(request.app.state, "approval_policy", None)
    if shared is not None:
        return shared
    return ApprovalPolicy(repositories, sessions)


def _binding_info(binding: AgentBinding) -> ApprovalBindingInfo:
    return ApprovalBindingInfo(
        binding_id=binding.id,
        profile_id=binding.profile_id,
        term_id=binding.term_id,
        status=binding.status,
    )


def _map_approval_error(exc: ApprovalError) -> TermFlowError:
    if isinstance(exc, ApprovalNotFound):
        return TermFlowError(
            "approval_not_found", 404, "The Approval Request does not exist."
        )
    if isinstance(exc, ApprovalExpired):
        return TermFlowError(
            "approval_expired", 410, "The Approval Request has expired."
        )
    if isinstance(exc, ApprovalRevoked):
        return TermFlowError(
            "approval_revoked", 409, "The Approval Request has been revoked."
        )
    if isinstance(exc, ApprovalAlreadyDecided):
        return TermFlowError(
            "approval_already_decided",
            409,
            "The Approval Request has already been decided.",
        )
    if isinstance(exc, ApprovalAuthEpochStale):
        return TermFlowError(
            "approval_auth_epoch_stale",
            409,
            "The session auth epoch is stale; re-authenticate and retry.",
        )
    if isinstance(exc, ApprovalAlreadyConsumed):
        return TermFlowError(
            "approval_already_consumed",
            409,
            "The Approval Request has already been consumed.",
        )
    return TermFlowError(
        "approval_conflict",
        409,
        "The Approval Request cannot be changed in its current state.",
    )


async def _approval_detail(
    approval_id: UUID,
    repositories: RepositoryBundle,
) -> ApprovalDetailResponse:
    approval = await repositories.approvals.get_by_id(approval_id)
    if approval is None:
        raise TermFlowError(
            "approval_not_found", 404, "The Approval Request does not exist."
        )
    binding = await repositories.agent_bindings.get_by_id(approval.binding_id)
    if binding is None:
        raise TermFlowError(
            "binding_not_found",
            404,
            "The Agent Binding for this approval does not exist.",
        )
    return ApprovalDetailResponse(
        **_approval_response(approval).model_dump(),
        binding=_binding_info(binding),
    )


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    conversation_id: Annotated[UUID | None, Query()] = None,
) -> ApprovalListResponse:
    if conversation_id is not None:
        conversation = await repositories.agent_conversations.get_by_id(conversation_id)
        if conversation is None:
            raise TermFlowError(
                "conversation_not_found",
                404,
                "The Agent Conversation does not exist.",
            )
    statement = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())
    if conversation_id is not None:
        statement = statement.where(ApprovalRequest.conversation_id == conversation_id)
    async with sessions() as session:
        rows = await session.scalars(statement)
        approvals = list(rows)
    return ApprovalListResponse(
        approvals=[_approval_response(approval) for approval in approvals]
    )


@router.get("/{approval_id}", response_model=ApprovalDetailResponse)
async def get_approval(
    approval_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> ApprovalDetailResponse:
    return await _approval_detail(approval_id, repositories)


@router.post("/{approval_id}/decide", response_model=ApprovalDetailResponse)
async def decide_approval(
    approval_id: UUID,
    request: ApprovalDecisionRequest,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ApprovalDetailResponse:
    policy = _shared_policy(http_request, repositories, sessions)
    epoch = await persisted_authentication_epoch(repositories)
    decision = (
        ApprovalDecision.APPROVED
        if request.decision == "approve"
        else ApprovalDecision.DENIED
    )
    try:
        await policy.decide(
            approval_id,
            decision=decision,
            actor=_ACTOR,
            auth_epoch=epoch,
        )
    except ApprovalError as exc:
        raise _map_approval_error(exc) from exc
    return await _approval_detail(approval_id, repositories)


@router.post("/{approval_id}/revoke", response_model=ApprovalDetailResponse)
async def revoke_approval(
    approval_id: UUID,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ApprovalDetailResponse:
    policy = _shared_policy(http_request, repositories, sessions)
    try:
        await policy.revoke(approval_id, actor=_ACTOR)
    except ApprovalError as exc:
        raise _map_approval_error(exc) from exc
    return await _approval_detail(approval_id, repositories)
