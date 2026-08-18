"""Administration API for the Agent Broker plugin (plan §13.1, task M1.5).

Admin-only endpoints under ``/api/v1/agent/admin`` for Agent Profiles,
Bindings, and binding-scoped AgentTokens.  Every route requires admin
authentication (``require_admin``).

Token security contract: ``POST /tokens`` returns the raw AgentToken exactly
once in the response body; only its SHA-256 hash is persisted.  The raw token
is never stored at rest, logged, or returned by any list/detail endpoint, so
callers must capture it from the create response and treat it as a secret.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.api.dependencies import (
    get_repositories,
    get_session_factory,
    require_admin,
)
from termflow_control_plane.auth.tokens import hash_token, issue_token
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import AgentBinding, AgentProfile, AgentToken
from termflow_control_plane.persistence.repositories import RepositoryBundle, decode_scopes
from termflow_control_plane.plugins.agent_broker.agent.permissions import ApprovalPolicy

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/agent/admin",
    tags=["agent"],
    dependencies=[Depends(require_admin)],
)

#: Binding states an admin may set explicitly.  ``revoked``/``disabled`` are
#: terminal; ``pending``/``ready`` are the active states a runtime needs.
_BINDING_STATUSES = frozenset({"pending", "ready", "revoked", "disabled"})


class AgentProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    backend_kind: str = Field(min_length=1, max_length=32)
    config: str = "{}"


class AgentProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    config: str | None = None


class AgentProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    display_name: str
    backend_kind: str
    config: str
    created_at: datetime
    updated_at: datetime


class AgentProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[AgentProfileResponse]


class AgentBindingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    term_id: UUID


class AgentBindingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    runtime_ref: str | None = None
    runtime_epoch: int | None = None
    capability_ref: str | None = None


class AgentBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    profile_id: UUID
    term_id: UUID
    status: str
    runtime_ref: str | None
    runtime_epoch: int | None
    capability_ref: str | None
    created_at: datetime
    updated_at: datetime


class AgentBindingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bindings: list[AgentBindingResponse]


class AgentTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    scopes: list[str] = []
    expires_at: datetime


class AgentTokenCreatedResponse(BaseModel):
    """Token issuance response.

    ``raw_token`` is returned exactly once and is never persisted; only its
    SHA-256 hash is stored at rest.  Callers must store it with the client
    immediately and treat it as a secret.
    """

    model_config = ConfigDict(extra="forbid")

    token_id: UUID
    binding_id: UUID
    scopes: list[str]
    expires_at: datetime
    raw_token: str
    created_at: datetime


class AgentTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: UUID
    binding_id: UUID
    scopes: list[str]
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


class AgentTokenListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: list[AgentTokenResponse]


def _binding_response(binding: AgentBinding) -> AgentBindingResponse:
    return AgentBindingResponse(
        binding_id=binding.id,
        profile_id=binding.profile_id,
        term_id=binding.term_id,
        status=binding.status,
        runtime_ref=binding.runtime_ref,
        runtime_epoch=binding.runtime_epoch,
        capability_ref=binding.capability_ref,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _profile_response(profile: AgentProfile) -> AgentProfileResponse:
    return AgentProfileResponse(
        profile_id=profile.id,
        display_name=profile.display_name,
        backend_kind=profile.backend_kind,
        config=profile.config,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _token_response(token: AgentToken) -> AgentTokenResponse:
    return AgentTokenResponse(
        token_id=token.id,
        binding_id=token.binding_id,
        scopes=list(decode_scopes(token.scopes)),
        expires_at=datetime.fromtimestamp(token.expiry_epoch, tz=UTC),
        revoked_at=token.revoked_at,
        created_at=token.created_at,
    )


async def _require_binding(
    binding_id: UUID,
    repositories: RepositoryBundle,
) -> AgentBinding:
    binding = await repositories.agent_bindings.get_by_id(binding_id)
    if binding is None:
        raise TermFlowError("binding_not_found", 404, "The Agent Binding does not exist.")
    return binding


async def _update_profile(
    sessions: async_sessionmaker[AsyncSession],
    profile_id: UUID,
    *,
    display_name: str | None,
    config: str | None,
) -> AgentProfile | None:
    """Apply the rename/config patch; the profile repository only exposes rename."""
    observed_at = datetime.now(UTC)
    values: dict[str, object] = {"updated_at": observed_at}
    if display_name is not None:
        values["display_name"] = display_name
    if config is not None:
        values["config"] = config
    async with sessions() as session:
        result = await session.execute(
            update(AgentProfile)
            .where(AgentProfile.id == profile_id)
            .values(**values)
            .returning(AgentProfile)
        )
        profile = result.scalar_one_or_none()
        await session.commit()
        return profile


async def _list_bindings(
    sessions: async_sessionmaker[AsyncSession],
    *,
    profile_id: UUID | None,
    term_id: UUID | None,
) -> list[AgentBinding]:
    """List bindings with optional filters; the repository has no unfiltered list."""
    statement = select(AgentBinding).order_by(AgentBinding.created_at)
    if profile_id is not None:
        statement = statement.where(AgentBinding.profile_id == profile_id)
    if term_id is not None:
        statement = statement.where(AgentBinding.term_id == term_id)
    async with sessions() as session:
        rows = await session.scalars(statement)
        return list(rows)


async def _get_agent_token(
    sessions: async_sessionmaker[AsyncSession],
    token_id: UUID,
) -> AgentToken | None:
    async with sessions() as session:
        return await session.get(AgentToken, token_id)


async def _revoke_agent_token(
    sessions: async_sessionmaker[AsyncSession],
    token_id: UUID,
) -> bool:
    """Revoke by id; the token repository only exposes revoke-by-hash."""
    observed_at = datetime.now(UTC)
    async with sessions() as session:
        result = await session.execute(
            update(AgentToken)
            .where(AgentToken.id == token_id, AgentToken.revoked_at.is_(None))
            .values(revoked_at=observed_at)
            .returning(AgentToken.id)
        )
        revoked = result.scalar_one_or_none() is not None
        await session.commit()
        return revoked


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@router.post(
    "/profiles",
    response_model=AgentProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_profile(
    request: AgentProfileCreateRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentProfileResponse:
    profile = await repositories.agent_profiles.create(
        display_name=request.display_name,
        backend_kind=request.backend_kind,
        config=request.config,
    )
    return _profile_response(profile)


@router.get("/profiles", response_model=AgentProfileListResponse)
async def list_agent_profiles(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentProfileListResponse:
    profiles = await repositories.agent_profiles.list()
    return AgentProfileListResponse(
        profiles=[_profile_response(profile) for profile in profiles]
    )


@router.get("/profiles/{profile_id}", response_model=AgentProfileResponse)
async def get_agent_profile(
    profile_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentProfileResponse:
    profile = await repositories.agent_profiles.get_by_id(profile_id)
    if profile is None:
        raise TermFlowError("profile_not_found", 404, "The Agent Profile does not exist.")
    return _profile_response(profile)


@router.patch("/profiles/{profile_id}", response_model=AgentProfileResponse)
async def update_agent_profile(
    profile_id: UUID,
    request: AgentProfileUpdateRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AgentProfileResponse:
    if request.display_name is None and request.config is None:
        raise TermFlowError(
            "invalid_request",
            422,
            "Provide at least one of display_name or config.",
        )
    profile = await _update_profile(
        sessions,
        profile_id,
        display_name=request.display_name,
        config=request.config,
    )
    if profile is None:
        raise TermFlowError("profile_not_found", 404, "The Agent Profile does not exist.")
    return _profile_response(profile)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_profile(
    profile_id: UUID,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> Response:
    """Delete a profile through the durable tombstone/cleanup path.

    Plan §15/§17 delete contract: a durable cleanup tombstone is created
    BEFORE the row deletion, the profile's active runs are cancelled, and
    every backend session of its bindings is proven deleted before any row
    goes away.  When the backend deletion cannot be proven, the rows stay
    and the request fails with ``deletion_pending`` while the tombstone
    keeps retrying.
    """
    from termflow_control_plane.api.agent_conversations import get_agent_runtime_registry
    from termflow_control_plane.plugins.agent_broker.plugin import (
        BackendRuntimeUnavailableError,
        delete_binding_backend_sessions,
        record_cleanup_failure,
    )

    if await repositories.agent_profiles.get_by_id(profile_id) is None:
        raise TermFlowError("profile_not_found", 404, "The Agent Profile does not exist.")
    registry = get_agent_runtime_registry(http_request)

    # 1) Durable cleanup tombstone BEFORE the row deletion (plan §15), so a
    #    crash mid-delete leaves a retryable job instead of orphaned rows.
    job = await repositories.cleanup_jobs.create(
        target_kind="profile",
        target_ref=str(profile_id),
    )

    # 2) Cancel the profile's active runs and prove every backend session of
    #    its bindings is gone before any row goes away (plan §17).
    failures: list[str] = []
    for binding in await repositories.agent_bindings.list_for_profile(profile_id):
        for run in await repositories.agent_runs.list_active_for_binding(binding.id):
            await repositories.agent_runs.set_state(
                run.id, "cancelled", expected_state=run.run_state
            )
        try:
            failures.extend(
                await delete_binding_backend_sessions(repositories, registry, binding.id)
            )
        except BackendRuntimeUnavailableError as exc:
            failures.append(str(exc))
    if failures:
        await record_cleanup_failure(job, repositories, "; ".join(failures))
        raise TermFlowError(
            "deletion_pending",
            503,
            "The profile's backend sessions could not be deleted; the "
            "cleanup job stays pending and will retry.",
        )

    # 3) Delete the profile row (cascading its bindings and conversations).
    await repositories.agent_profiles.delete(profile_id)
    await repositories.cleanup_jobs.complete(job.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


@router.post(
    "/bindings",
    response_model=AgentBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_binding(
    request: AgentBindingCreateRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentBindingResponse:
    if await repositories.instances.get(request.term_id) is None:
        raise TermFlowError(
            "instance_not_found",
            404,
            "The Term does not exist.",
        )
    if await repositories.agent_bindings.active_binding_for(
        request.profile_id,
        request.term_id,
    ) is not None:
        raise TermFlowError(
            "binding_already_exists",
            409,
            "An active Agent Binding already exists for this Profile and Term.",
        )
    binding = await repositories.agent_bindings.create(
        profile_id=request.profile_id,
        term_id=request.term_id,
    )
    return _binding_response(binding)


@router.get("/bindings", response_model=AgentBindingListResponse)
async def list_agent_bindings(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    profile_id: Annotated[UUID | None, Query()] = None,
    term_id: Annotated[UUID | None, Query()] = None,
) -> AgentBindingListResponse:
    bindings = await _list_bindings(
        sessions,
        profile_id=profile_id,
        term_id=term_id,
    )
    return AgentBindingListResponse(
        bindings=[_binding_response(binding) for binding in bindings]
    )


@router.get("/bindings/{binding_id}", response_model=AgentBindingResponse)
async def get_agent_binding(
    binding_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentBindingResponse:
    binding = await _require_binding(binding_id, repositories)
    return _binding_response(binding)


@router.patch("/bindings/{binding_id}", response_model=AgentBindingResponse)
async def update_agent_binding(
    binding_id: UUID,
    request: AgentBindingUpdateRequest,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AgentBindingResponse:
    await _require_binding(binding_id, repositories)

    binding: AgentBinding | None = None
    if request.status is not None:
        if request.status not in _BINDING_STATUSES:
            raise TermFlowError(
                "invalid_binding_status",
                422,
                "Unknown binding status.",
            )
        binding = await repositories.agent_bindings.set_status(binding_id, request.status)
        if request.status in ("revoked", "disabled"):
            # A revoked/disabled binding must not keep pending or approved
            # approvals alive (spec §5): every request of the binding is
            # revoked so no waiter can ever execute a reviewed write.
            # NOTE: the binding status is already committed at this point.  A
            # revocation failure still fails the call with 409, but the status
            # change stays.  That is safe (not a security gap): CommandService
            # rechecks the binding state before every execution, so a write
            # can never run under a revoked binding even when this sweep
            # missed its approvals.
            shared = getattr(http_request.app.state, "approval_policy", None)
            policy = shared or ApprovalPolicy(repositories, sessions)
            try:
                revoked_count = await policy.revoke_for_binding(binding_id, actor="admin")
            except Exception as exc:
                raise TermFlowError(
                    "binding_approval_revoke_failed",
                    409,
                    "The binding status changed but its approvals could not be revoked.",
                ) from exc
            if revoked_count:
                logger.info("revoked %s approvals of binding %s", revoked_count, binding_id)
    runtime_fields = {
        request.runtime_ref,
        request.runtime_epoch,
        request.capability_ref,
    }
    if any(value is not None for value in runtime_fields):
        if not all(value is not None for value in runtime_fields):
            raise TermFlowError(
                "invalid_runtime_update",
                422,
                "A runtime update requires runtime_ref, runtime_epoch, and capability_ref.",
            )
        binding = await repositories.agent_bindings.update_runtime(
            binding_id,
            runtime_ref=request.runtime_ref,  # type: ignore[arg-type]
            runtime_epoch=request.runtime_epoch,  # type: ignore[arg-type]
            capability_ref=request.capability_ref,  # type: ignore[arg-type]
        )
    if binding is None:
        raise TermFlowError(
            "invalid_request",
            422,
            "Provide status or a complete runtime update.",
        )
    return _binding_response(binding)


@router.delete("/bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_binding(
    binding_id: UUID,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> Response:
    """Delete a binding through the durable tombstone/cleanup path.

    Plan §15/§17 delete contract: the binding's active runs are cancelled
    first, a durable cleanup tombstone is created BEFORE the row deletion,
    and every backend session of the binding's conversations is proven
    deleted before any row goes away.  When the backend deletion cannot be
    proven, the rows stay and the request fails with ``deletion_pending``
    while the tombstone keeps retrying.
    """
    from termflow_control_plane.api.agent_conversations import get_agent_runtime_registry
    from termflow_control_plane.plugins.agent_broker.plugin import (
        BackendRuntimeUnavailableError,
        delete_binding_backend_sessions,
        record_cleanup_failure,
    )

    await _require_binding(binding_id, repositories)
    registry = get_agent_runtime_registry(http_request)

    # 1) Cancel the binding's active runs before any deletion (plan §17).
    for run in await repositories.agent_runs.list_active_for_binding(binding_id):
        await repositories.agent_runs.set_state(
            run.id, "cancelled", expected_state=run.run_state
        )

    # 2) Durable cleanup tombstone BEFORE the row deletion (plan §15), so a
    #    crash mid-delete leaves a retryable job instead of orphaned rows.
    job = await repositories.cleanup_jobs.create(
        target_kind="binding",
        target_ref=str(binding_id),
    )

    # 3) Prove every backend session of the binding's conversations is gone
    #    before deleting the rows (an OpenCode session is never orphaned).
    try:
        failures = await delete_binding_backend_sessions(
            repositories, registry, binding_id
        )
    except BackendRuntimeUnavailableError as exc:
        await record_cleanup_failure(job, repositories, str(exc))
        raise TermFlowError(
            "binding_runtime_unavailable",
            503,
            "The binding has backend sessions but its runtime is not "
            "available; the cleanup job stays pending.",
        ) from exc
    if failures:
        await record_cleanup_failure(job, repositories, "; ".join(failures))
        raise TermFlowError(
            "deletion_pending",
            503,
            "The binding's backend sessions could not be deleted; the "
            "cleanup job stays pending and will retry.",
        )

    # 4) Delete the binding row (cascading its conversations and events).
    await repositories.agent_bindings.delete(binding_id)
    await repositories.cleanup_jobs.complete(job.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@router.post(
    "/tokens",
    response_model=AgentTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_token(
    request: AgentTokenCreateRequest,
    response: Response,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentTokenCreatedResponse:
    binding = await _require_binding(request.binding_id, repositories)
    expiry_epoch = int(request.expires_at.timestamp())
    if expiry_epoch <= int(datetime.now(UTC).timestamp()):
        raise TermFlowError(
            "invalid_expiry",
            422,
            "expires_at must be in the future.",
        )
    raw_token = issue_token()
    token = await repositories.agent_tokens.create(
        binding_id=binding.id,
        token_hash=hash_token(raw_token),
        scopes=tuple(request.scopes),
        expiry_epoch=expiry_epoch,
        binding_epoch=binding.runtime_epoch or 1,
    )
    # Secrets must never be cached by intermediaries or browsers.
    response.headers["Cache-Control"] = "no-store"
    return AgentTokenCreatedResponse(
        token_id=token.id,
        binding_id=token.binding_id,
        scopes=list(request.scopes),
        expires_at=datetime.fromtimestamp(token.expiry_epoch, tz=UTC),
        raw_token=raw_token,
        created_at=token.created_at,
    )


@router.get("/tokens", response_model=AgentTokenListResponse)
async def list_agent_tokens(
    binding_id: Annotated[UUID, Query()],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentTokenListResponse:
    await _require_binding(binding_id, repositories)
    tokens = await repositories.agent_tokens.list_for_binding(binding_id)
    return AgentTokenListResponse(tokens=[_token_response(token) for token in tokens])


@router.post("/tokens/{token_id}/revoke", response_model=AgentTokenResponse)
async def revoke_agent_token(
    token_id: UUID,
    response: Response,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AgentTokenResponse:
    token = await _get_agent_token(sessions, token_id)
    if token is None:
        raise TermFlowError("token_not_found", 404, "The Agent Token does not exist.")
    await _revoke_agent_token(sessions, token_id)
    revoked = await _get_agent_token(sessions, token_id)
    assert revoked is not None
    response.headers["Cache-Control"] = "no-store"
    return _token_response(revoked)
