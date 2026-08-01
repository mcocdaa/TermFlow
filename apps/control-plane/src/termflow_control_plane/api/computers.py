"""Computer list and display-name management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from termflow_protocol import ComputerListResponse, ComputerRenameRequest, ComputerSummary

from termflow_control_plane.api.dashboard import computer_summaries
from termflow_control_plane.api.dependencies import (
    get_registry,
    get_repositories,
    require_admin,
)
from termflow_control_plane.connections.registry import LiveInstanceRegistry
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle

router = APIRouter(prefix="/api/v1/computers", tags=["computers"])


async def _computer(
    installation_id: UUID,
    repositories: RepositoryBundle,
    registry: LiveInstanceRegistry,
) -> ComputerSummary:
    for computer in await computer_summaries(repositories, registry):
        if computer.installation_id == installation_id:
            return computer
    raise TermFlowError("computer_not_found", 404, "The Computer does not exist.")


@router.get(
    "",
    response_model=ComputerListResponse,
    dependencies=[Depends(require_admin)],
)
async def list_computers(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> ComputerListResponse:
    return ComputerListResponse(computers=await computer_summaries(repositories, registry))


@router.get(
    "/{installation_id}",
    response_model=ComputerSummary,
    dependencies=[Depends(require_admin)],
)
async def get_computer(
    installation_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> ComputerSummary:
    return await _computer(installation_id, repositories, registry)


@router.patch(
    "/{installation_id}",
    response_model=ComputerSummary,
    dependencies=[Depends(require_admin)],
)
async def rename_computer(
    installation_id: UUID,
    request: ComputerRenameRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> ComputerSummary:
    if await repositories.installations.rename(installation_id, request.display_name) is None:
        raise TermFlowError("computer_not_found", 404, "The Computer does not exist.")
    return await _computer(installation_id, repositories, registry)
