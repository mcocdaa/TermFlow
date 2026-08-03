"""Computer list and display-name management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from termflow_protocol import ComputerListResponse, ComputerRenameRequest, ComputerSummary

from termflow_control_plane.api.dashboard import computer_summaries
from termflow_control_plane.api.dependencies import (
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


@router.delete(
    "/{installation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_computer(
    installation_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> Response:
    installation = await repositories.installations.get(installation_id)
    if installation is None or installation.revoked_at is not None:
        raise TermFlowError("computer_not_found", 404, "The Computer does not exist.")

    instances = [
        instance
        for instance in await repositories.instances.list_all()
        if instance.installation_id == installation_id
    ]
    retired: list[UUID] = []
    try:
        for instance in instances:
            try:
                await registry.begin_retirement(instance.id)
            except InstanceOnline as exc:
                raise TermFlowError(
                    "computer_online",
                    409,
                    "The Computer has an online Term.",
                ) from exc
            except InstanceRetired as exc:
                raise TermFlowError(
                    "computer_not_found",
                    404,
                    "The Computer does not exist.",
                ) from exc
            retired.append(instance.id)

        deleted = await repositories.installations.delete(installation_id)
        if not deleted:
            raise TermFlowError("computer_not_found", 404, "The Computer does not exist.")
    finally:
        for instance_id in retired:
            await registry.cancel_retirement(instance_id)

    await repositories.audit.record("computer.delete", None, None, None, "ok", None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
