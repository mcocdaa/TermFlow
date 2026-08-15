"""Product-facing Agent Broker conversation API (plan §13.1, task M1.5).

Endpoints under ``/api/v1/agent/conversations`` create/list/delete durable
product conversations and page historical messages and canonical events.

Backend opacity contract: product responses expose only conversation-scoped
data.  Backend session internals (``BackendConversationRef.provider_ref``,
runtime ids, backend kinds) are never returned to product clients; backend
sessions stay an opaque mapping owned by the plugin boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from termflow_protocol.agent import MAX_AGENT_TEXT_BYTES
from termflow_protocol.messages import validate_plain_text

from termflow_control_plane.api.dependencies import get_repositories, require_admin
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentConversation,
    AgentEvent,
    AgentMessage,
)
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.agui_projection import (
    WIRE_AGUI,
    WIRE_CANONICAL,
    AgentEventProjector,
    validate_wire,
)
from termflow_control_plane.plugins.agent_broker.agent.pipeline import (
    AgentPipelineService,
    NoActiveRunError,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    AgentRuntimeRegistry,
)
from termflow_control_plane.plugins.agent_broker.agent.stream_hub import binding_is_closed

router = APIRouter(
    prefix="/api/v1/agent/conversations",
    tags=["agent"],
    dependencies=[Depends(require_admin)],
)


class AgentConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    title: str | None = Field(default=None, max_length=255)


class AgentConversationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    binding_id: UUID
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class AgentConversationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversations: list[AgentConversationResponse]


class AgentConversationBindingInfo(BaseModel):
    """Product-visible binding facts; backend/runtime internals stay opaque."""

    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    profile_id: UUID
    term_id: UUID
    status: str


class AgentConversationDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    binding_id: UUID
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    binding: AgentConversationBindingInfo


class AgentMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    conversation_id: UUID
    run_id: UUID | None
    role: str
    kind: str
    assembly_revision: int
    is_final: bool
    body_digest: str
    #: Bounded message text (M6b spec §6.6); ``None`` for pre-migration rows
    #: and digest-only messages, which clients render as a degraded
    #: "content unavailable" placeholder.
    body: str | None
    created_at: datetime


class AgentMessageListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[AgentMessageResponse]


class AgentEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    conversation_id: UUID
    run_id: UUID | None
    event_kind: str
    database_seq: int
    payload_digest: str
    ephemeral: bool
    created_at: datetime


class AgentEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[AgentEventResponse]
    #: Opaque cursor: the last returned database sequence.  ``None`` when the
    #: page is empty without a ``since`` cursor.
    next_cursor: int | None


class AgentSubmitMessageRequest(BaseModel):
    """Plain-text user message admission (M4.5 spec §2); no draft_ref yet."""

    model_config = ConfigDict(extra="forbid")

    text: str

    @field_validator("text")
    @classmethod
    def plain_text_only(cls, value: str) -> str:
        return validate_plain_text(value, max_bytes=MAX_AGENT_TEXT_BYTES)


class AgentSubmitMessageResponse(BaseModel):
    """202 admission receipt: the inbox item identity plus delivery state."""

    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    conversation_id: UUID
    admission_seq: int
    idempotency_key: str
    delivery_state: str
    submission_state: str


class AgentCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class AgentCancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str
    run_state: str


def _conversation_response(conversation: AgentConversation) -> AgentConversationResponse:
    return AgentConversationResponse(
        conversation_id=conversation.id,
        binding_id=conversation.binding_id,
        title=conversation.title,
        status=conversation.status,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _message_response(message: AgentMessage) -> AgentMessageResponse:
    return AgentMessageResponse(
        message_id=message.id,
        conversation_id=message.conversation_id,
        run_id=message.run_id,
        role=message.role,
        kind=message.kind,
        assembly_revision=message.assembly_revision,
        is_final=message.is_final,
        body_digest=message.body_digest,
        body=message.body,
        created_at=message.created_at,
    )


def _event_response(event: AgentEvent) -> AgentEventResponse:
    return AgentEventResponse(
        event_id=event.id,
        conversation_id=event.conversation_id,
        run_id=event.run_id,
        event_kind=event.event_kind,
        database_seq=event.database_seq,
        payload_digest=event.payload_digest,
        ephemeral=event.ephemeral,
        created_at=event.created_at,
    )


async def _require_conversation(
    conversation_id: UUID,
    repositories: RepositoryBundle,
) -> AgentConversation:
    conversation = await repositories.agent_conversations.get_by_id(conversation_id)
    if conversation is None:
        raise TermFlowError(
            "conversation_not_found",
            404,
            "The Agent Conversation does not exist.",
        )
    return conversation


def get_agent_runtime_registry(request: Request) -> AgentRuntimeRegistry | None:
    """The per-binding runtime registry, or ``None`` when not wired.

    The lifespan only builds it while the plugin is enabled; a missing
    registry (or an unmapped binding) must fail closed at the endpoint.
    """
    return cast(
        AgentRuntimeRegistry | None,
        getattr(request.app.state, "agent_runtime_registry", None),
    )


async def _require_open_binding(
    conversation: AgentConversation,
    repositories: RepositoryBundle,
) -> AgentBinding:
    """Resolve the conversation's binding, failing closed when revoked/disabled."""
    binding = await repositories.agent_bindings.get_by_id(conversation.binding_id)
    if binding is None or binding_is_closed(binding.status):
        raise TermFlowError(
            "binding_revoked",
            403,
            "The Agent Binding is revoked or disabled.",
        )
    return binding


def _require_pipeline(
    binding: AgentBinding,
    registry: AgentRuntimeRegistry | None,
) -> AgentPipelineService:
    """Resolve the binding's pipeline, failing closed with 503 when unmapped.

    An unmapped binding means its runtime was never activated (missing
    runtime fields or a rejected supervisor gate), so submission must not
    pretend the backend is reachable (M4.5 spec §2 / plan §17).
    """
    pipeline = registry.pipeline_for(binding.id) if registry is not None else None
    if pipeline is None:
        raise TermFlowError(
            "binding_runtime_unavailable",
            503,
            "The binding's runtime is not available.",
        )
    return pipeline


@router.post(
    "",
    response_model=AgentConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_conversation(
    request: AgentConversationCreateRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentConversationResponse:
    if await repositories.agent_bindings.get_by_id(request.binding_id) is None:
        raise TermFlowError("binding_not_found", 404, "The Agent Binding does not exist.")
    conversation = await repositories.agent_conversations.create(
        binding_id=request.binding_id,
        title=request.title,
    )
    return _conversation_response(conversation)


@router.get("", response_model=AgentConversationListResponse)
async def list_agent_conversations(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    binding_id: Annotated[UUID, Query()],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentConversationListResponse:
    if await repositories.agent_bindings.get_by_id(binding_id) is None:
        raise TermFlowError("binding_not_found", 404, "The Agent Binding does not exist.")
    conversations = await repositories.agent_conversations.list_for_binding(
        binding_id,
        limit=limit,
        offset=offset,
    )
    return AgentConversationListResponse(
        conversations=[_conversation_response(item) for item in conversations]
    )


@router.get("/{conversation_id}", response_model=AgentConversationDetailResponse)
async def get_agent_conversation(
    conversation_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentConversationDetailResponse:
    conversation = await _require_conversation(conversation_id, repositories)
    binding = await repositories.agent_bindings.get_by_id(conversation.binding_id)
    if binding is None:
        raise TermFlowError(
            "binding_not_found",
            404,
            "The Agent Binding for this conversation does not exist.",
        )
    return AgentConversationDetailResponse(
        conversation_id=conversation.id,
        binding_id=conversation.binding_id,
        title=conversation.title,
        status=conversation.status,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        binding=_binding_info(binding),
    )


def _binding_info(binding: AgentBinding) -> AgentConversationBindingInfo:
    return AgentConversationBindingInfo(
        binding_id=binding.id,
        profile_id=binding.profile_id,
        term_id=binding.term_id,
        status=binding.status,
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_conversation(
    conversation_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> Response:
    if not await repositories.agent_conversations.delete(conversation_id):
        raise TermFlowError(
            "conversation_not_found",
            404,
            "The Agent Conversation does not exist.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{conversation_id}/messages", response_model=AgentMessageListResponse)
async def list_agent_messages(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    conversation_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentMessageListResponse:
    await _require_conversation(conversation_id, repositories)
    messages = await repositories.agent_messages.list_for_conversation(
        conversation_id,
        limit=limit,
        offset=offset,
    )
    # Messages are returned in conversation-assembly order, which is the
    # canonical creation order (DB-assigned assembly_revision).
    return AgentMessageListResponse(
        messages=[_message_response(message) for message in messages]
    )


@router.get("/{conversation_id}/events", response_model=AgentEventListResponse)
async def list_agent_events(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    conversation_id: UUID,
    since: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    wire: Annotated[str, Query()] = WIRE_CANONICAL,
) -> AgentEventListResponse | JSONResponse:
    """Paginated canonical events, or AG-UI projections with ``?wire=agui``.

    The envelope is identical in both modes: ``events`` plus ``next_cursor``
    with its ``database_seq`` semantics (M6a spec §4.5).  In agui mode the
    event objects are stateless per-page AG-UI projections (a message
    chunk+END pair stays complete across pages), and unprojectable events are
    omitted without shifting the cursor.
    """
    validate_wire(wire)
    await _require_conversation(conversation_id, repositories)
    if since is not None:
        events = await repositories.agent_events.list_since_cursor(
            conversation_id,
            since,
            limit=limit,
        )
        next_cursor = events[-1].database_seq if events else since
    else:
        events = await repositories.agent_events.list_for_conversation(
            conversation_id,
            limit=limit,
            offset=offset,
        )
        next_cursor = events[-1].database_seq if events else None
    if wire == WIRE_AGUI:
        # A fresh projector per request: projection is stateless and the drop
        # counters are request-scoped diagnostics only (spec §4.6).
        projector = AgentEventProjector()
        agui_events: list[dict[str, object]] = []
        for event in events:
            agui_events.extend(projector.project(event))
        return JSONResponse(
            content={"events": agui_events, "next_cursor": next_cursor}
        )
    return AgentEventListResponse(
        events=[_event_response(event) for event in events],
        next_cursor=next_cursor,
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=AgentSubmitMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_agent_message(
    conversation_id: UUID,
    request: AgentSubmitMessageRequest,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentSubmitMessageResponse:
    """Admit one plain-text user message into the binding's pipeline (M4.5 §2).

    The admission is durably enqueued and the response is ``202 Accepted``;
    the backend's 204 confirmation stays adapter-internal.  Request text
    validation runs first in the request validator (``422
    invalid_request``); the fail-closed checks then run in order:
    conversation exists (404), binding open (403), runtime pipeline mapped
    and ready (503).
    """
    conversation = await _require_conversation(conversation_id, repositories)
    binding = await _require_open_binding(conversation, repositories)
    pipeline = _require_pipeline(binding, get_agent_runtime_registry(http_request))
    admission = await pipeline.submit_user_message(
        conversation_id, request.text, actor="admin"
    )
    return AgentSubmitMessageResponse(
        message_id=admission.message_id,
        conversation_id=admission.conversation_id,
        admission_seq=admission.admission_seq,
        idempotency_key=admission.idempotency_key,
        delivery_state=admission.delivery_state,
        submission_state=admission.submission_state,
    )


@router.post(
    "/{conversation_id}/cancel",
    response_model=AgentCancelResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_agent_conversation(
    conversation_id: UUID,
    request: AgentCancelRequest,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentCancelResponse:
    """Cancel the conversation's active run (M4.5 spec §8).

    A conversation without a queued/running run fails with ``409
    no_active_run``; the outcome reflects what the pipeline could prove
    (``confirmed`` only when a terminal state was reached or reconciled,
    ``unknown`` otherwise - never an invented cancellation).
    """
    conversation = await _require_conversation(conversation_id, repositories)
    binding = await _require_open_binding(conversation, repositories)
    pipeline = _require_pipeline(binding, get_agent_runtime_registry(http_request))
    try:
        result = await pipeline.cancel_conversation(
            conversation_id, reason=request.reason
        )
    except NoActiveRunError:
        raise TermFlowError(
            "no_active_run",
            409,
            "The conversation has no active run to cancel.",
        ) from None
    return AgentCancelResponse(
        outcome=result.outcome.value,
        run_state=result.run_state,
    )
