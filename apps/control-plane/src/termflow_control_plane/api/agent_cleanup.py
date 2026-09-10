"""Safe cleanup-manifest status and pending-deletion response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from termflow_control_plane.api.dependencies import (
    get_repositories,
    require_admin,
    require_cleanup_helper,
)
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle

router = APIRouter(
    prefix="/api/v1/agent/admin",
    tags=["agent"],
)


class CleanupPendingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cleanup_job_id: UUID
    state: Literal["deletion_pending"]
    status_url: str


class CleanupReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: UUID
    artifact_kind: str
    state: Literal["pending", "confirmed", "not_applicable", "dead_letter"]
    attempt_count: int
    next_attempt_at: datetime | None = None
    reason_code: str | None = None


class CleanupJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cleanup_job_id: UUID
    target_kind: str
    state: Literal["pending", "completed", "dead_letter"]
    receipts: list[CleanupReceiptResponse]


def pending_response(request: Request, job_id: UUID) -> JSONResponse:
    """Build the only public body allowed for an incomplete deletion."""

    status_url = str(request.url_for("get_cleanup_job", job_id=str(job_id)))
    payload = CleanupPendingResponse(
        cleanup_job_id=job_id,
        state="deletion_pending",
        status_url=status_url,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=payload.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


async def existing_deletion_response(
    request: Request, repositories: RepositoryBundle, target_kind: str, target_id: UUID
) -> Response | None:
    job = await repositories.cleanup_jobs.get_by_target(
        target_kind=target_kind, target_ref=str(target_id)
    )
    if job is None:
        return None
    refreshed = await repositories.cleanup_jobs.refresh_state(job.id)
    if refreshed is not None and refreshed.state == "completed":
        return Response(status_code=204)
    return pending_response(request, job.id)


def _safe_reason(receipt: object) -> str | None:
    state = str(getattr(receipt, "state", ""))
    if state == "dead_letter":
        return "cleanup_dead_letter"
    if state == "pending" and getattr(receipt, "last_error", None):
        return "cleanup_retry_scheduled"
    if state == "not_applicable":
        return "policy_not_applicable"
    return None


@router.get(
    "/cleanup-jobs/{job_id}",
    response_model=CleanupJobResponse,
    name="get_cleanup_job",
    dependencies=[Depends(require_admin)],
)
async def get_cleanup_job(
    job_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> CleanupJobResponse:
    job = await repositories.cleanup_jobs.refresh_state(job_id)
    if job is None:
        raise TermFlowError("cleanup_job_not_found", 404, "The cleanup job does not exist.")
    receipts = await repositories.cleanup_jobs.list_receipts(job.id)
    return CleanupJobResponse(
        cleanup_job_id=job.id,
        target_kind=job.target_kind,
        state=cast(Literal["pending", "completed", "dead_letter"], job.state),
        receipts=[
            CleanupReceiptResponse(
                receipt_id=receipt.id,
                artifact_kind=receipt.artifact_kind,
                state=cast(
                    Literal["pending", "confirmed", "not_applicable", "dead_letter"], receipt.state
                ),
                attempt_count=receipt.attempt_count,
                next_attempt_at=receipt.next_attempt_at,
                reason_code=_safe_reason(receipt),
            )
            for receipt in receipts
        ],
    )


class CleanupReceiptConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_ref: str = Field(min_length=1, max_length=256)
    result: Literal["confirmed", "dead_letter"]
    evidence_digest: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    idempotency_key: UUID

    @model_validator(mode="after")
    def validate_result(self) -> CleanupReceiptConfirmRequest:
        if self.result == "confirmed" and (
            self.evidence_digest is None or self.reason_code is not None
        ):
            raise ValueError("confirmed requires evidence only")
        if self.result == "dead_letter" and (
            self.reason_code is None or self.evidence_digest is not None
        ):
            raise ValueError("dead_letter requires reason only")
        return self


@router.post(
    "/cleanup-jobs/{job_id}/receipts/{receipt_id}/confirm",
    response_model=CleanupReceiptResponse,
    dependencies=[Depends(require_cleanup_helper)],
)
async def confirm_cleanup_receipt(
    job_id: UUID,
    receipt_id: UUID,
    payload: CleanupReceiptConfirmRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> CleanupReceiptResponse:
    try:
        receipt = await repositories.cleanup_jobs.confirm_helper(
            job_id=job_id,
            receipt_id=receipt_id,
            artifact_ref=payload.artifact_ref,
            result=payload.result,
            evidence_digest=payload.evidence_digest.lower() if payload.evidence_digest else None,
            reason_code=payload.reason_code,
            idempotency_key=payload.idempotency_key,
        )
    except KeyError as exc:
        raise TermFlowError(
            "cleanup_receipt_not_found", 404, "Cleanup receipt does not exist."
        ) from exc
    except ValueError as exc:
        raise TermFlowError(
            "cleanup_receipt_conflict", 409, "Cleanup confirmation conflicts with the receipt."
        ) from exc
    return CleanupReceiptResponse(
        receipt_id=receipt.id,
        artifact_kind=receipt.artifact_kind,
        state=cast(Literal["pending", "confirmed", "not_applicable", "dead_letter"], receipt.state),
        attempt_count=receipt.attempt_count,
        reason_code=_safe_reason(receipt),
    )
