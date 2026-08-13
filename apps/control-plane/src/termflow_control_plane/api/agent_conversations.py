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
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

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
