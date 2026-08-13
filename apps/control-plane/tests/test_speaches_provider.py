"""SpeachesTranscriptionProvider unit tests (spec §4.3, §7.1, no Docker).

Exercises the provider against an injected ``httpx.AsyncClient`` built on
``httpx.MockTransport`` (the opencode.py adapter precedent): the pinned
OpenAI-compatible multipart contract, the mime-to-filename mapping, optional
Bearer auth, disclosure metadata on ``TranscriptionResult``, and the
fail-closed mapping of every connection/HTTP/response-shape failure into
:class:`TranscriptionProviderError` — with raw audio and response bodies
never appearing in logs.
"""

from __future__ import annotations

from email import policy
from email.parser import BytesParser

import httpx
import pytest
from termflow_control_plane.plugins.agent_broker.agent.speaches import (
    SpeachesTranscriptionProvider,
)
from termflow_control_plane.plugins.agent_broker.agent.transcription import (
    TranscriptionProviderError,
)

MODEL = "Systran/faster-distil-whisper-small.en"
AUDIO = b"\x00\x01\x02\x03" * 256


def _provider(
    handler,
    *,
    token: str | None = "secret-token",
    model: str = MODEL,
    max_audio_bytes: int | None = None,
    requests: list[httpx.Request] | None = None,
) -> SpeachesTranscriptionProvider:
    captured = requests if requests is not None else []

    def record(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    kwargs = {} if max_audio_bytes is None else {"max_audio_bytes": max_audio_bytes}
    return SpeachesTranscriptionProvider(
        "http://stt-speaches:8000",
        model=model,
        token=token,
        timeout_seconds=5.0,
        client=client,
        **kwargs,
    )


def _multipart_fields(request: httpx.Request) -> dict[str, bytes]:
    """Parse the multipart body into a {field name: bytes} map.

    ``BytesParser.parsebytes`` cannot know the multipart boundary without
    the message headers, so the request's Content-Type is prepended.
    """
    body = request.read()
    headers = (
        f"Content-Type: {request.headers['content-type']}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode()
    message = BytesParser(policy=policy.default).parsebytes(headers + body)
    fields: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = _disposition_param(part.get("Content-Disposition", ""), "name")
        if name is not None:
            fields[name] = part.get_payload(decode=True) or b""
    return fields


def _disposition_param(disposition: str, key: str) -> str | None:
    for item in disposition.split(";"):
        item = item.strip()
        if item.startswith(f"{key}="):
            return item.split("=", 1)[1].strip('"')
    return None


class TestSuccessPath:
    async def test_transcribes_wav_with_bearer_and_pinned_fields(self) -> None:
        requests: list[httpx.Request] = []
        provider = _provider(
            lambda request: httpx.Response(200, json={"text": "hello world"}),
            requests=requests,
        )

        result = await provider.transcribe(AUDIO, mime_type="audio/wav")

        assert result.transcript == "hello world"
        assert result.provider == "speaches"
        assert result.model == MODEL
        assert result.region is None
        assert result.language is None
        assert result.duration_seconds is None
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == "http://stt-speaches:8000/v1/audio/transcriptions"
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert request.headers["authorization"] == "Bearer secret-token"
        fields = _multipart_fields(request)
        assert fields["model"] == MODEL.encode()
        assert fields["response_format"] == b"json"
        assert fields["file"] == AUDIO
        assert b'filename="speech.wav"' in request.read()

    @pytest.mark.parametrize(
        ("mime_type", "filename"),
        [
            ("audio/wav", "speech.wav"),
            ("audio/webm", "speech.webm"),
            ("audio/mpeg", "speech.mp3"),
        ],
    )
    async def test_mime_maps_to_a_stable_filename(
        self, mime_type: str, filename: str
    ) -> None:
        requests: list[httpx.Request] = []
        provider = _provider(
            lambda request: httpx.Response(200, json={"text": "hi"}),
            requests=requests,
        )

        await provider.transcribe(AUDIO, mime_type=mime_type)

        assert f'filename="{filename}"'.encode() in requests[0].read()

    async def test_no_authorization_header_without_token(self) -> None:
        requests: list[httpx.Request] = []
        provider = _provider(
            lambda request: httpx.Response(200, json={"text": "hi"}),
            token=None,
            requests=requests,
        )

        await provider.transcribe(AUDIO, mime_type="audio/wav")

        assert requests[0].headers.get("authorization") is None


class TestAvailability:
    def test_available_is_configuration_derived(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, json={"text": "hi"}))
        # M7a spec §5.3: availability is derived from configuration, never a
        # live /health probe.
        assert provider.available() is True


class TestFailureMapping:
    async def test_connection_failure_is_unreachable(self) -> None:
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        provider = _provider(refused)

        with pytest.raises(TranscriptionProviderError, match="speaches unreachable"):
            await provider.transcribe(AUDIO, mime_type="audio/wav")

    async def test_request_timeout_is_timed_out(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        provider = _provider(slow)

        with pytest.raises(TranscriptionProviderError, match="speaches timed out"):
            await provider.transcribe(AUDIO, mime_type="audio/wav")

    @pytest.mark.parametrize("status", [401, 403, 422, 500, 503])
    async def test_http_errors_carry_only_the_status(self, status: int) -> None:
        provider = _provider(
            lambda request: httpx.Response(status, json={"detail": "boom"})
        )

        with pytest.raises(
            TranscriptionProviderError,
            match=f"speaches returned HTTP {status}",
        ):
            await provider.transcribe(AUDIO, mime_type="audio/wav")

    async def test_non_json_response_is_invalid(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, text="<html>nope"))

        with pytest.raises(
            TranscriptionProviderError, match="invalid transcription response"
        ):
            await provider.transcribe(AUDIO, mime_type="audio/wav")

    async def test_missing_text_is_invalid(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, json={"other": 1}))

        with pytest.raises(
            TranscriptionProviderError, match="invalid transcription response"
        ):
            await provider.transcribe(AUDIO, mime_type="audio/wav")

    async def test_empty_text_is_invalid(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, json={"text": ""}))

        with pytest.raises(
            TranscriptionProviderError, match="invalid transcription response"
        ):
            await provider.transcribe(AUDIO, mime_type="audio/wav")

    async def test_oversized_text_is_invalid(self) -> None:
        provider = _provider(
            lambda request: httpx.Response(200, json={"text": "x" * 4097})
        )

        with pytest.raises(
            TranscriptionProviderError, match="invalid transcription response"
        ):
            await provider.transcribe(AUDIO, mime_type="audio/wav")

    async def test_failures_never_log_audio_or_response_bodies(self, caplog) -> None:
        provider = _provider(
            lambda request: httpx.Response(500, json={"detail": "sensitive-body"})
        )

        with caplog.at_level("DEBUG"):
            with pytest.raises(TranscriptionProviderError):
                await provider.transcribe(AUDIO, mime_type="audio/wav")

        assert repr(AUDIO) not in caplog.text
        assert AUDIO.decode("latin-1") not in caplog.text
        assert "sensitive-body" not in caplog.text


class TestDefensiveBounds:
    async def test_empty_audio_rejected(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, json={"text": "hi"}))

        with pytest.raises(TranscriptionProviderError):
            await provider.transcribe(b"", mime_type="audio/wav")

    async def test_oversized_audio_rejected_before_any_request(self) -> None:
        requests: list[httpx.Request] = []
        provider = _provider(
            lambda request: httpx.Response(200, json={"text": "hi"}),
            max_audio_bytes=1024,
            requests=requests,
        )

        with pytest.raises(TranscriptionProviderError):
            await provider.transcribe(b"x" * 1025, mime_type="audio/wav")
        assert requests == []

    async def test_unknown_mime_rejected(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, json={"text": "hi"}))

        with pytest.raises(TranscriptionProviderError):
            await provider.transcribe(AUDIO, mime_type="audio/ogg")
