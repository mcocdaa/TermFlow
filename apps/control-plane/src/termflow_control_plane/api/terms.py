"""Online tmux Term management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from termflow_protocol import TermRenameRequest, TermSummary

from termflow_control_plane.api.dashboard import term_summary
from termflow_control_plane.api.dependencies import (
    get_command_router,
    get_registry,
    get_repositories,
    require_admin,
)
from termflow_control_plane.connections.registry import (
    InstanceOnline,
    InstanceRetired,
    LiveInstanceRegistry,
)
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.routing.router import CommandRouter

router = APIRouter(prefix="/api/v1/terms", tags=["terms"])


@router.delete(
    "/{instance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_term(
    instance_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> Response:
    if await repositories.instances.get(instance_id) is None:
        raise TermFlowError("instance_not_found", 404, "The Term does not exist.")
    try:
        await registry.begin_retirement(instance_id)
    except InstanceOnline as exc:
        raise TermFlowError("instance_online", 409, "The Term is online.") from exc
    except InstanceRetired as exc:
        raise TermFlowError("instance_not_found", 404, "The Term does not exist.") from exc

    try:
        deleted = await repositories.instances.delete(instance_id)
    except BaseException:
        await registry.cancel_retirement(instance_id)
        raise
    if not deleted:
        await registry.cancel_retirement(instance_id)
        raise TermFlowError("instance_not_found", 404, "The Term does not exist.")

    await repositories.audit.record(
        "term.delete",
        instance_id,
        None,
        None,
        "ok",
        None,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{instance_id}",
    response_model=TermSummary,
    dependencies=[Depends(require_admin)],
)
async def rename_term(
    instance_id: UUID,
    request: TermRenameRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
    command_router: Annotated[CommandRouter, Depends(get_command_router)],
) -> TermSummary:
    instance = await repositories.instances.get(instance_id)
    if instance is None:
        raise TermFlowError("instance_not_found", 404, "The Term does not exist.")
    await command_router.rename_term(instance_id, request.name)
    renamed = await repositories.instances.rename(instance_id, request.name)
    if renamed is None:
        raise TermFlowError("instance_not_found", 404, "The Term does not exist.")
    return term_summary(renamed, await registry.maybe_get(instance_id))
