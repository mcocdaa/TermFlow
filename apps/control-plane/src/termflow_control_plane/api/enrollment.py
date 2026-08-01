"""Single-use enrollment and Installation bootstrap endpoints."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from termflow_protocol import (
    EnrollmentCreateResponse,
    InstallationEnrollRequest,
    InstallationEnrollResponse,
)

from termflow_control_plane.auth.tokens import hash_token, issue_token
from termflow_control_plane.config import Settings
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle

from .dependencies import get_repositories, get_settings, require_admin

router = APIRouter(prefix="/api/v1")


@router.post(
    "/enrollment-tokens",
    response_model=EnrollmentCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_enrollment_token(
    response: Response,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EnrollmentCreateResponse:
    raw_token = issue_token()
    expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.enrollment_token_ttl_seconds
    )
    await repositories.enrollments.create(hash_token(raw_token), expires_at)
    response.headers["Cache-Control"] = "no-store"
    return EnrollmentCreateResponse(token=raw_token, expires_at=expires_at)


@router.post(
    "/installations/enroll",
    response_model=InstallationEnrollResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enroll_installation(
    request: InstallationEnrollRequest,
    response: Response,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> InstallationEnrollResponse:
    enrollment_token = request.enrollment_token.get_secret_value()
    enrollment_id = await repositories.enrollments.consume(hash_token(enrollment_token))
    if enrollment_id is None:
        raise TermFlowError(
            "invalid_enrollment_token",
            401,
            "The enrollment token is invalid, expired, or already used.",
        )
    raw_installation_token = issue_token()
    installation = await repositories.installations.create(
        hash_token(raw_installation_token),
        hostname=request.hostname,
        display_name=request.hostname,
        platform=request.platform,
        client_version=request.client_version,
    )
    response.headers["Cache-Control"] = "no-store"
    return InstallationEnrollResponse(
        installation_id=installation.id,
        installation_token=raw_installation_token,
    )
