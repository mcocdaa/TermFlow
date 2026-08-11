"""Bounded voice transcription draft API (plan §14, task M7.1).

Endpoints under ``/api/v1/agent/transcription`` turn bounded audio uploads
into short-lived :class:`~termflow_control_plane.persistence.models.TranscriptDraft`
rows that only the same authenticated user may confirm once, before the
confirmed transcript may later become a normal ``UserMessage`` (that
submission lands with the agent pipeline milestone; nothing is auto-submitted
to a backend here).

Documented bounds (plan §14):

- upload size: 10 MiB, enforced on the ``Content-Length`` header when
  present and, authoritatively, while streaming the body into a temporary
  file (never accumulated unbounded in memory);
- formats: ``audio/webm``, ``audio/wav``, ``audio/mpeg`` sniffed by magic
  bytes — the declared client Content-Type is never trusted;
- ``audio/wav`` gets a cheap decode-aware check (sample rate 8-96 kHz and
  duration <= 10 minutes parsed from the header); webm/mpeg duration is
  validated by the provider after decoding and re-checked against the same
  duration cap;
- concurrency: at most 4 in-flight transcriptions per process;
- per-call timeout: 60 seconds;
- draft TTL: 1 hour (§16.1), enforced at confirm time;
- raw audio is deleted after transcription by default (temporary file
  unlinked in ``finally`` on every path) and is never logged or persisted —
  the draft row holds only hash + metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, ValidationError

from termflow_control_plane.api.dependencies import get_repositories, require_admin
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import TranscriptDraft
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.transcription import (
    NullTranscriptionProvider,
    TranscriptionProvider,
    TranscriptionProviderError,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/agent/transcription",
    tags=["agent"],
    dependencies=[Depends(require_admin)],
)

#: Documented upload/decode bounds (plan §14).
MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_AUDIO_DURATION_SECONDS = 600.0  # 10 minutes, before and after decoding
MIN_SAMPLE_RATE_HZ = 8_000
MAX_SAMPLE_RATE_HZ = 96_000
TRANSCRIPTION_TIMEOUT_SECONDS = 60.0
MAX_CONCURRENT_TRANSCRIPTIONS = 4
DRAFT_TTL = timedelta(hours=1)  # §16.1 transcript draft retention
_CHUNK_SIZE = 64 * 1024


class TranscriptionDraftResponse(BaseModel):
    """Upload result: the draft id plus the transcript shown for confirmation."""

    model_config = ConfigDict(extra="forbid")

    draft_id: UUID
    state: str
    transcript: str
    provider: str
    region: str
    language: str | None
    duration_seconds: float | None
    expires_at: datetime


class TranscriptionDraftDetailResponse(BaseModel):
    """Owner-only draft metadata read.

    The transcript text itself is never persisted (only its hash), so it is
    returned to the client once, at upload time, for confirmation.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: UUID
    binding_id: UUID
    target_conversation_id: UUID
    state: str
    provider: str
    region: str
    transcript_hash: str
    expires_at: datetime
    created_at: datetime


def get_transcription_provider(request: Request) -> TranscriptionProvider:
    provider = getattr(request.app.state, "transcription_provider", None)
    if provider is None:
        return NullTranscriptionProvider()
    return cast(TranscriptionProvider, provider)


def _actor_id(request: Request) -> str:
    """Stable identity of the authenticated actor who owns drafts.

    Native device clients authenticate as their ``native_client_id``; the
    admin token, CLI tokens, and Web admin sessions share the single
    ``admin`` actor.
    """
    client_id = getattr(request.state, "native_client_id", None)
    if client_id is not None:
        return f"native:{client_id}"
    return "admin"


def _as_utc(value: datetime) -> datetime:
    """Normalize a persisted timestamp to an aware UTC value.

    ``DateTime(timezone=True)`` columns carry the offset on Postgres, but
    SQLite returns naive values; treat naive values as UTC (the codebase
    always writes ``utc_now``), matching the ``_as_utc`` helper in the
    dashboard API.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sniff_mime(audio: bytes) -> str | None:
    """Sniff the magic MIME type from the audio head (never trust the client)."""
    if audio.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    if len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        return "audio/wav"
    if audio.startswith(b"ID3") or (
        len(audio) >= 2 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0
    ):
        return "audio/mpeg"
    return None


def _wav_sample_rate_duration(audio: bytes) -> tuple[int, float]:
    """Cheap decode-aware bounds from a WAV header (PCM only).

    Returns ``(sample_rate_hz, duration_seconds)``; raises ``ValueError``
    for malformed or unsupported WAV data.
    """
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    cursor = 12
    sample_rate = 0
    byte_rate = 0
    data_available = 0
    while cursor + 8 <= len(audio):
        chunk_id = audio[cursor : cursor + 4]
        (chunk_size,) = struct.unpack_from("<I", audio, cursor + 4)
        body_start = cursor + 8
        if chunk_id == b"fmt ":
            if body_start + 16 > len(audio):
                raise ValueError("truncated fmt chunk")
            audio_format, channels, sample_rate, byte_rate = struct.unpack_from(
                "<HHII", audio, body_start
            )
            if audio_format != 1:
                raise ValueError("unsupported WAV encoding")
            if channels < 1 or byte_rate <= 0:
                raise ValueError("malformed WAV parameters")
        elif chunk_id == b"data":
            data_available = min(chunk_size, len(audio) - body_start)
            break
        cursor = body_start + chunk_size + (chunk_size % 2)
    else:
        raise ValueError("missing data chunk")
    if byte_rate <= 0:
        raise ValueError("missing fmt chunk")
    return sample_rate, data_available / byte_rate


def _timeout_seconds(request: Request) -> float:
    value = getattr(request.app.state, "transcription_timeout_seconds", None)
    return float(value) if value is not None else TRANSCRIPTION_TIMEOUT_SECONDS


@asynccontextmanager
async def _staged_audio(
    request: Request,
    audio: UploadFile,
) -> AsyncIterator[str]:
    """Stream the upload into a temporary file, bounded, always unlinked.

    The size cap is enforced while streaming so the body is never
    accumulated unbounded in memory or on disk; the temporary file is
    unlinked in ``finally`` on every path (plan §14: temporary files and
    error paths are cleaned up).
    """
    staging_dir = getattr(request.app.state, "transcription_staging_dir", None)
    staged: Any = await asyncio.to_thread(
        tempfile.NamedTemporaryFile,
        prefix="termflow-audio-",
        suffix=".bin",
        dir=staging_dir,
        delete=False,
    )
    path = staged.name
    closed = False
    try:
        total = 0
        while True:
            chunk = await audio.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_AUDIO_BYTES:
                raise TermFlowError(
                    "audio_too_large",
                    413,
                    "The audio file exceeds the 10 MiB upload limit.",
                )
            await asyncio.to_thread(staged.write, chunk)
        await asyncio.to_thread(staged.close)
        closed = True
        yield path
    finally:
        if not closed:
            await asyncio.to_thread(staged.close)
        with suppress(OSError):
            await asyncio.to_thread(os.unlink, path)


async def _transcribe_with_bounds(
    request: Request,
    provider: TranscriptionProvider,
    audio: bytes,
    mime_type: str,
) -> TranscriptionResult:
    """Call the provider under the concurrency and timeout bounds."""
    semaphore = getattr(request.app.state, "transcription_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRANSCRIPTIONS)
    try:
        raw = await asyncio.wait_for(
            _transcribe(semaphore, provider, audio, mime_type),
            timeout=_timeout_seconds(request),
        )
        result = TranscriptionResult.model_validate(raw)
    except TimeoutError as exc:
        raise TermFlowError(
            "transcription_timeout",
            504,
            "The transcription provider did not respond in time.",
        ) from exc
    except TranscriptionProviderError as exc:
        raise TermFlowError(
            "transcription_failed",
            502,
            "The transcription provider could not process the audio.",
        ) from exc
    except ValidationError as exc:
        raise TermFlowError(
            "transcription_failed",
            502,
            "The transcription provider returned an invalid transcript.",
        ) from exc
    if (
        result.duration_seconds is not None
        and result.duration_seconds > MAX_AUDIO_DURATION_SECONDS
    ):
        raise TermFlowError(
            "audio_too_long",
            422,
            "The decoded audio exceeds the 10 minute duration limit.",
        )
    return result


async def _transcribe(
    semaphore: asyncio.Semaphore,
    provider: TranscriptionProvider,
    audio: bytes,
    mime_type: str,
) -> TranscriptionResult:
    async with semaphore:
        return await provider.transcribe(audio, mime_type=mime_type)


async def _require_owned_draft(
    draft_id: UUID,
    request: Request,
    repositories: RepositoryBundle,
) -> TranscriptDraft:
    draft = await repositories.transcript_drafts.get_by_id(draft_id)
    if draft is None:
        raise TermFlowError(
            "draft_not_found",
            404,
            "The transcript draft does not exist.",
        )
    if draft.owner_actor_id != _actor_id(request):
        raise TermFlowError(
            "draft_not_owner",
            403,
            "The transcript draft belongs to another user.",
        )
    return draft


@router.post(
    "/drafts",
    response_model=TranscriptionDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_transcript_draft(
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    provider: Annotated[TranscriptionProvider, Depends(get_transcription_provider)],
    audio: Annotated[UploadFile, File()],
    binding_id: Annotated[UUID, Form()],
    target_conversation_id: Annotated[UUID, Form()],
) -> TranscriptionDraftResponse:
    if not provider.available():
        raise TermFlowError(
            "speech_to_text_unavailable",
            503,
            "Speech-to-text is not available on this deployment.",
        )
    binding = await repositories.agent_bindings.get_by_id(binding_id)
    if binding is None:
        raise TermFlowError("binding_not_found", 404, "The Agent Binding does not exist.")
    conversation = await repositories.agent_conversations.get_by_id(target_conversation_id)
    if conversation is None:
        raise TermFlowError(
            "conversation_not_found",
            404,
            "The Agent Conversation does not exist.",
        )
    if conversation.binding_id != binding_id:
        raise TermFlowError(
            "binding_conversation_mismatch",
            422,
            "The conversation does not belong to the given binding.",
        )
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_AUDIO_BYTES:
                raise TermFlowError(
                    "audio_too_large",
                    413,
                    "The audio file exceeds the 10 MiB upload limit.",
                )
        except ValueError:
            pass

    async with _staged_audio(request, audio) as path:
        audio_bytes = await asyncio.to_thread(_read_staged, path)
        mime_type = _sniff_mime(audio_bytes)
        if mime_type is None:
            raise TermFlowError(
                "unsupported_audio_format",
                415,
                "The audio format is not supported (webm, wav, and mpeg only).",
            )
        if mime_type == "audio/wav":
            try:
                sample_rate, duration = _wav_sample_rate_duration(audio_bytes)
            except ValueError as exc:
                raise TermFlowError(
                    "invalid_audio",
                    422,
                    "The WAV audio is malformed or unsupported.",
                ) from exc
            if not MIN_SAMPLE_RATE_HZ <= sample_rate <= MAX_SAMPLE_RATE_HZ:
                raise TermFlowError(
                    "audio_sample_rate_out_of_bounds",
                    422,
                    "The audio sample rate is outside the supported range.",
                )
            if duration > MAX_AUDIO_DURATION_SECONDS:
                raise TermFlowError(
                    "audio_too_long",
                    422,
                    "The audio exceeds the 10 minute duration limit.",
                )

        result = await _transcribe_with_bounds(request, provider, audio_bytes, mime_type)

    draft = await repositories.transcript_drafts.create(
        binding_id=binding_id,
        target_conversation_id=target_conversation_id,
        owner_actor_id=_actor_id(request),
        transcript_hash=hashlib.sha256(result.transcript.encode("utf-8")).hexdigest(),
        provider=result.provider,
        region=result.region or "",
        expires_at=datetime.now(UTC) + DRAFT_TTL,
        state="draft",
    )
    return TranscriptionDraftResponse(
        draft_id=draft.id,
        state=draft.state,
        transcript=result.transcript,
        provider=draft.provider,
        region=draft.region,
        language=result.language,
        duration_seconds=result.duration_seconds,
        expires_at=draft.expires_at,
    )


def _read_staged(path: str) -> bytes:
    with open(path, "rb") as staged:
        return staged.read()


@router.post("/drafts/{draft_id}/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_transcript_draft(
    draft_id: UUID,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> Response:
    draft = await _require_owned_draft(draft_id, request, repositories)
    if _as_utc(draft.expires_at) <= datetime.now(UTC):
        raise TermFlowError(
            "draft_expired",
            410,
            "The transcript draft has expired.",
        )
    if (
        await repositories.transcript_drafts.set_state(
            draft_id,
            "confirmed",
            expected_state="draft",
        )
        is None
    ):
        raise TermFlowError(
            "draft_not_confirmable",
            409,
            "The transcript draft cannot be confirmed.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/drafts/{draft_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_transcript_draft(
    draft_id: UUID,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> Response:
    await _require_owned_draft(draft_id, request, repositories)
    if (
        await repositories.transcript_drafts.set_state(
            draft_id,
            "cancelled",
            expected_state="draft",
        )
        is None
    ):
        raise TermFlowError(
            "draft_not_cancellable",
            409,
            "The transcript draft cannot be cancelled.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/drafts/{draft_id}", response_model=TranscriptionDraftDetailResponse)
async def get_transcript_draft(
    draft_id: UUID,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> TranscriptionDraftDetailResponse:
    draft = await _require_owned_draft(draft_id, request, repositories)
    return TranscriptionDraftDetailResponse(
        draft_id=draft.id,
        binding_id=draft.binding_id,
        target_conversation_id=draft.target_conversation_id,
        state=draft.state,
        provider=draft.provider,
        region=draft.region,
        transcript_hash=draft.transcript_hash,
        expires_at=draft.expires_at,
        created_at=draft.created_at,
    )
