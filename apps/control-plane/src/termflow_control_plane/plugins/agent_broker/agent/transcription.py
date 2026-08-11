"""Transcription provider port (plan §14, task M7.1).

B owns voice/STT as an input adapter, not part of the Agent Backend
contract: C records bounded audio, B hands it to a
:class:`TranscriptionProvider`, stores a short-lived
:class:`~termflow_control_plane.persistence.models.TranscriptDraft`, and
only a confirmed draft may later become a normal ``UserMessage``.

This module defines the port and a ``Null`` implementation so text chat
remains fully functional when STT is absent (plan §14).  An optional
separately deployed STT container may implement the port; B still owns
upload limits, raw-audio deletion, draft confirmation, provider disclosure,
and no-auto-submit.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class TranscriptionResult(BaseModel):
    """Bounded, provider-produced transcript with disclosure metadata.

    The transcript text is bounded here (plan §14: transcript is shown for
    confirmation before it can cause an Agent turn).  The decoded
    ``duration_seconds`` is re-checked by the upload API against the same
    duration cap it applies before decoding (plan §14: bounded before and
    after decoding).
    """

    model_config = ConfigDict(extra="forbid")

    transcript: str = Field(min_length=1, max_length=4096)
    provider: str = Field(min_length=1, max_length=64)
    region: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=16)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    model: str | None = Field(default=None, max_length=128)


class TranscriptionProviderError(RuntimeError):
    """The STT provider rejected or failed to transcribe the audio."""


class TranscriptionProvider(Protocol):
    """Port from bounded audio bytes to bounded transcript text.

    Implementations must not log, persist, or retain the raw audio; B
    deletes raw audio after transcription by default and never logs it.
    """

    def available(self) -> bool:
        """Describe capability: whether an STT deployment is reachable."""
        ...

    async def transcribe(self, audio: bytes, *, mime_type: str) -> TranscriptionResult:
        """Transcribe bounded ``audio`` bytes, returning bounded text.

        Providers raise :class:`TranscriptionProviderError` on failure.
        """
        ...


class NullTranscriptionProvider:
    """Unavailable provider used when no STT deployment is configured.

    Keeps text chat fully functional: the upload API answers 503
    ``speech_to_text_unavailable`` instead of failing in other ways.
    """

    def available(self) -> bool:
        return False

    async def transcribe(self, audio: bytes, *, mime_type: str) -> TranscriptionResult:
        raise TranscriptionProviderError("no transcription provider is configured")
