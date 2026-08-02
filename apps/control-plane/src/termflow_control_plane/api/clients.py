"""Same-origin Web administration for authorized native clients."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from termflow_protocol import (
    NativeClientDeleteResponse,
    NativeClientListResponse,
    NativeClientResponse,
    NativeClientUpdateRequest,
)

from termflow_control_plane.auth.oauth import client_response
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle

from .dependencies import get_repositories, require_web_admin

router = APIRouter(
    prefix="/api/v1/admin/clients",
    tags=["clients"],
    dependencies=[Depends(require_web_admin)],
)


@router.get("", response_model=NativeClientListResponse)
async def list_native_clients(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> NativeClientListResponse:
    clients = await repositories.native_clients.list_authorized()
    return NativeClientListResponse(clients=[client_response(client) for client in clients])


@router.patch("/{client_id}", response_model=NativeClientResponse)
async def rename_native_client(
    client_id: UUID,
    body: NativeClientUpdateRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> NativeClientResponse:
    client = await repositories.native_clients.rename(client_id, body.display_name)
    if client is None:
        raise TermFlowError("client_not_found", 404, "The client was not found.")
    return client_response(client)


@router.delete("/{client_id}", response_model=NativeClientDeleteResponse)
async def delete_native_client(
    client_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> NativeClientDeleteResponse:
    if not await repositories.native_clients.revoke(client_id):
        raise TermFlowError("client_not_found", 404, "The client was not found.")
    return NativeClientDeleteResponse()
