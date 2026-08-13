"""Speaches (OpenAI-compatible Whisper/faster-whisper) STT provider (M7a).

Implements the :class:`TranscriptionProvider` port against the pinned
speaches container contract (``tests/fixtures/speaches/speaches-pin.md``):
``POST /v1/audio/transcriptions`` with a multipart ``file``/``model``/
``response_format=json`` body and optional Bearer auth, returning the
default ``{"text": ...}`` JSON shape.

**Privacy contract (port §14):** raw audio never appears in logs, is never
persisted or retained by this provider — the request body is only ever sent
to the configured STT endpoint, and every failure is reduced to a stable
category (unreachable / timed out / HTTP status / invalid response) before
logging.

**httpx loading (house rule):** httpx is a dev-group dependency, so it is
never imported at module level; tests inject a client (``httpx.MockTransport``
precedent, as in ``agent/opencode.py``) and the provider lazily builds an
owned client only when none is injected. No new runtime dependency.

Timeout layering (spec §5.1): the provider HTTP timeout (default 55s) is
strictly below B's 60s wall clock in ``api/transcription.py``, so a slow or
stalled provider maps to 502 (provider error) before the B-side 504 race
can invert.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from .transcription import (
    TranscriptionProviderError,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)

#: Pinned OpenAI-compatible endpoint on the speaches container (pin fixture).
_TRANSCRIPTIONS_PATH = "/v1/audio/transcriptions"

#: B's authoritative upload bound (api/transcription.py MAX_AUDIO_BYTES),
#: re-checked here as port-boundary defense in depth (spec §5.1).
DEFAULT_MAX_AUDIO_BYTES = 10 * 1024 * 1024

#: Response body cap: a transcription JSON ({"text": ...} up to the port's
#: 4096-char transcript bound) is far below this; anything larger is a
#: malformed or malicious provider response and is rejected before parsing.
MAX_RESPONSE_BYTES = 64 * 1024

#: Stable safe filenames per sniffed mime; the port never trusts callers.
_MIME_FILENAMES = {
    "audio/wav": "speech.wav",
    "audio/webm": "speech.webm",
    "audio/mpeg": "speech.mp3",
}

_PROVIDER_NAME = "speaches"


class SpeachesTranscriptionProvider:
    """OpenAI-compatible transcription provider for the pinned speaches image.

    ``client`` may be any async HTTP client exposing ``post`` (for example
    ``httpx.AsyncClient``) and is accepted so tests inject an
    ``httpx.MockTransport``; httpx is a dev-group dependency here, so it is
    never imported at module level.  When no client is injected the provider
    creates and owns an ``httpx.AsyncClient`` with the provider timeout.

    ``available()`` is configuration-derived (spec §5.3): construction
    succeeded, so the deployment is declared available; there is no live
    ``/health`` probe per request.  Container liveness is exposed by the
    compose healthcheck instead.
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        token: str | None = None,
        timeout_seconds: float = 55.0,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
        client: object | None = None,
    ) -> None:
        if not base_url or not model:
            raise ValueError("base_url and model must be non-empty")
        if max_audio_bytes <= 0:
            raise ValueError("max_audio_bytes must be positive")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_audio_bytes = max_audio_bytes
        self._owns_client = client is None
        if client is None:
            try:
                import httpx  # dev-group dependency: lazy import, never module-level
            except ImportError as exc:  # pragma: no cover - dev env always has httpx
                raise ImportError(
                    "httpx is required to create an owned HTTP client; "
                    "inject an async client instead"
                ) from exc
            client = httpx.AsyncClient(timeout=timeout_seconds)
        self._client: Any = client

    def available(self) -> bool:
        """Describe capability: configuration-derived, no live probing (§5.3)."""
        return True

    async def close(self) -> None:
        """Close the HTTP client only when this provider owns it.

        The composition root installs the provider as a process-lifetime
        singleton and calls this during application shutdown; an injected
        (test) client belongs to its injector and is left open (the
        ``agent/opencode.py`` precedent).
        """
        if self._owns_client:
            await self._client.aclose()

    async def transcribe(self, audio: bytes, *, mime_type: str) -> TranscriptionResult:
        """Transcribe bounded ``audio`` bytes against the pinned endpoint.

        Every failure is raised as :class:`TranscriptionProviderError` with a
        stable category message; raw audio, request bodies, and response
        bodies are never logged or embedded in errors.
        """
        if not (0 < len(audio) <= self.max_audio_bytes):
            raise TranscriptionProviderError(
                "speaches audio must be non-empty and within the size bound"
            )
        filename = _MIME_FILENAMES.get(mime_type)
        if filename is None:
            raise TranscriptionProviderError(
                f"speaches does not support mime type {mime_type}"
            )
        headers = {}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        try:
            response = await self._client.post(
                f"{self.base_url}{_TRANSCRIPTIONS_PATH}",
                headers=headers,
                data={"model": self.model, "response_format": "json"},
                files={"file": (filename, audio, mime_type)},
            )
        except Exception as exc:
            category = _classify_connection_error(exc)
            logger.error("speaches transcription failed: %s", category)
            raise TranscriptionProviderError(category) from exc
        if not 200 <= response.status_code < 300:
            logger.error(
                "speaches transcription failed: HTTP %s", response.status_code
            )
            raise TranscriptionProviderError(
                f"speaches returned HTTP {response.status_code}"
            )
        # Bound the response before parsing (spec §4.3 invalid-response
        # family): a declared Content-Length over the cap is rejected up
        # front, and the materialized body is re-checked for chunked/absent
        # headers before any JSON parsing cost is paid.
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = -1
            if declared_bytes > MAX_RESPONSE_BYTES:
                logger.error("speaches transcription failed: invalid response")
                raise TranscriptionProviderError("invalid transcription response")
        try:
            if len(response.content) > MAX_RESPONSE_BYTES:
                logger.error("speaches transcription failed: invalid response")
                raise TranscriptionProviderError("invalid transcription response")
            payload = response.json()
        except TranscriptionProviderError:
            raise
        except Exception as exc:
            logger.error("speaches transcription failed: invalid response")
            raise TranscriptionProviderError(
                "invalid transcription response"
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text:
            logger.error("speaches transcription failed: invalid response")
            raise TranscriptionProviderError("invalid transcription response")
        try:
            return TranscriptionResult(
                transcript=text,
                provider=_PROVIDER_NAME,
                model=self.model,
            )
        except ValidationError as exc:
            # Bounds enforced here as the port boundary (transcript 1..4096);
            # pydantic failures surface as a provider error, never as a
            # leaked validation exception.
            logger.error("speaches transcription failed: invalid response")
            raise TranscriptionProviderError(
                "invalid transcription response"
            ) from exc


def _classify_connection_error(exc: Exception) -> str:
    """Map a client-level failure to a stable category (spec §4.3).

    The lazy httpx import can only fail when a caller injected a non-httpx
    client that raised a non-httpx exception, which still reduces to a stable
    category.  ``ConnectTimeout`` subclasses both ``ConnectError`` and
    ``TimeoutException``, so timeouts are classified first.
    """
    try:
        import httpx  # dev-group dependency: lazy import, never module-level
    except ImportError:
        return "speaches failed"
    if isinstance(exc, httpx.TimeoutException):
        return "speaches timed out"
    if isinstance(exc, httpx.ConnectError):
        return "speaches unreachable"
    return "speaches failed"
