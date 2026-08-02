"""Instance enrollment endpoints used by a local TermFlow installation."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from termflow_protocol import (
    CommandResponse,
    InstanceListResponse,
    InstanceRegisterRequest,
    InstanceRegisterResponse,
    InstanceResponse,
    PaneInputRequest,
    TopologyResponse,
)

from termflow_control_plane.api.dependencies import (
    get_command_router,
    get_registry,
    get_repositories,
    require_admin,
    require_installation,
)
from termflow_control_plane.auth.tokens import hash_token, issue_token
from termflow_control_plane.connections.registry import InstanceOffline, LiveInstanceRegistry
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import Installation, Instance
from termflow_control_plane.persistence.repositories import (
    InstanceOwnershipError,
    RepositoryBundle,
)
from termflow_control_plane.routing.router import CommandRouter

router = APIRouter(prefix="/api/v1/instances", tags=["instances"])


@router.post(
    "/register",
    response_model=InstanceRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_instance(
    request: InstanceRegisterRequest,
    installation: Annotated[Installation, Depends(require_installation)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> InstanceRegisterResponse:
    """Create an Instance credential, or rotate it for the owning installation."""

    raw_token = issue_token()
    try:
        instance = await repositories.instances.register_or_rotate(
            instance_id=request.instance_id,
            installation_id=installation.id,
            name=request.name,
            token_hash=hash_token(raw_token),
        )
    except InstanceOwnershipError as exc:
        raise TermFlowError(
            "instance_owned_by_another_installation",
            status.HTTP_403_FORBIDDEN,
            "The Instance belongs to another installation.",
        ) from exc
    await registry.reactivate(instance.id)
    return InstanceRegisterResponse(instance_id=instance.id, instance_token=raw_token)


def _instance_response(instance: Instance, online: bool) -> InstanceResponse:
    return InstanceResponse(
        instance_id=instance.id,
        name=instance.name,
        installation_id=instance.installation_id,
        created_at=instance.created_at,
        online=online,
    )


async def _require_instance(instance_id: UUID, repositories: RepositoryBundle) -> Instance:
    instance = await repositories.instances.get(instance_id)
    if instance is None:
        raise TermFlowError("instance_not_found", 404, "The Instance does not exist.")
    return instance


@router.get(
    "",
    response_model=InstanceListResponse,
    dependencies=[Depends(require_admin)],
)
async def list_instances(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> InstanceListResponse:
    instances = await repositories.instances.list_all()
    online = await registry.online_ids()
    return InstanceListResponse(
        instances=[_instance_response(instance, instance.id in online) for instance in instances]
    )


@router.get(
    "/{instance_id}",
    response_model=InstanceResponse,
    dependencies=[Depends(require_admin)],
)
async def get_instance(
    instance_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> InstanceResponse:
    instance = await _require_instance(instance_id, repositories)
    online = await registry.maybe_get(instance_id) is not None
    return _instance_response(instance, online)


@router.get(
    "/{instance_id}/topology",
    response_model=TopologyResponse,
    dependencies=[Depends(require_admin)],
)
async def get_topology(
    instance_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> TopologyResponse:
    await _require_instance(instance_id, repositories)
    try:
        connection = await registry.get(instance_id)
    except InstanceOffline as exc:
        raise TermFlowError(
            "instance_offline",
            409,
            "The Instance is not connected.",
        ) from exc
    if connection.topology is None:
        raise TermFlowError(
            "topology_unavailable",
            409,
            "The Instance has not reported its topology.",
        )
    return TopologyResponse(instance_id=instance_id, topology=connection.topology)


@router.post(
    "/{instance_id}/panes/{pane_id}/input",
    response_model=CommandResponse,
    dependencies=[Depends(require_admin)],
)
async def send_pane_input(
    instance_id: UUID,
    pane_id: str,
    request: PaneInputRequest,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    command_router: Annotated[CommandRouter, Depends(get_command_router)],
) -> CommandResponse:
    await _require_instance(instance_id, repositories)
    result = await command_router.send_input(
        instance_id,
        pane_id,
        request.text,
        request.submit,
        idempotency_key,
    )
    return CommandResponse(
        command_id=result.command_id,
        idempotency_key=result.idempotency_key,
        ok=result.ok,
    )
