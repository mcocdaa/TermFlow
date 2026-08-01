"""Online tmux Term management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from termflow_protocol import TermRenameRequest, TermSummary

from termflow_control_plane.api.dashboard import term_summary
from termflow_control_plane.api.dependencies import (
    get_command_router,
    get_registry,
    get_repositories,
    require_admin,
)
from termflow_control_plane.connections.registry import LiveInstanceRegistry
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.routing.router import CommandRouter

router = APIRouter(prefix="/api/v1/terms", tags=["terms"])


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
