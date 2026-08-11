"""Bounded voice transcription draft API tests (plan §14, task M7.1).

Covers the ``/api/v1/agent/transcription`` router: bounded multipart audio
upload that stores a short-lived ``TranscriptDraft`` (hash + metadata only,
raw audio never persisted nor logged), same-user one-time confirm/cancel,
owner-only reads, provider absence (503), and guaranteed temp-file cleanup
on every error path.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from termflow_control_plane.persistence.models import TranscriptDraft
from termflow_control_plane.persistence.repositories import RepositoryBundle
from termflow_control_plane.plugins.agent_broker.agent.transcription import (
    TranscriptionProviderError,
    TranscriptionResult,
)

from .oauth_helpers import (
    approve_authorization,
    begin_authorization,
    exchange_authorization,
    key_and_jwk,
    proof,
)

_TRANSCRIPT = "hello from voice"


def _wav_bytes(
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    duration_seconds: float = 1.0,
) -> bytes:
    block_align = channels * 2
    byte_rate = sample_rate * block_align
    data_size = int(byte_rate * duration_seconds)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        16,
        b"data",
        data_size,
    )
    return header + b"\x00" * data_size


def _webm_bytes(size: int = 4096) -> bytes:
    return b"\x1a\x45\xdf\xa3" + b"\x00" * max(0, size - 4)


class FakeTranscriptionProvider:
    def __init__(
        self,
        *,
        available: bool = True,
        error: Exception | None = None,
        delay: float = 0.0,
        result: TranscriptionResult | None = None,
    ) -> None:
        self.available_value = available
        self.error = error
        self.delay = delay
        self.result = result
        self.calls: list[tuple[bytes, str]] = []

    def available(self) -> bool:
        return self.available_value

    async def transcribe(self, audio: bytes, *, mime_type: str) -> TranscriptionResult:
        self.calls.append((audio, mime_type))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return TranscriptionResult(
            transcript=_TRANSCRIPT,
            provider="fake",
            region="test-region",
            language="en",
            duration_seconds=1.0,
        )


def _create_profile(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    display_name: str = "opencode",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/profiles",
        headers=admin_headers,
        json={
            "display_name": display_name,
            "backend_kind": "opencode",
            "config": '{"model": "default"}',
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    profile_id: UUID,
    term_id: UUID,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/admin/bindings",
        headers=admin_headers,
        json={"profile_id": str(profile_id), "term_id": str(term_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_conversation(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    binding_id: UUID,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/agent/conversations",
        headers=admin_headers,
        json={"binding_id": str(binding_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_binding(
    client: TestClient,
    admin_headers: dict[str, str],
    provision_term,
) -> UUID:
    profile = _create_profile(
        client,
        admin_headers,
        display_name=f"opencode-{uuid4().hex[:8]}",
    )
    term = provision_term(name="voice-term")
    binding = _create_binding(
        client,
        admin_headers,
        profile_id=UUID(str(profile["profile_id"])),
        term_id=term.instance_id,
    )
    return UUID(str(binding["binding_id"]))


def _upload(
    client: TestClient,
    *,
    binding_id: UUID,
    conversation_id: UUID,
    audio: bytes,
    mime: str = "audio/wav",
    filename: str = "speech.wav",
    headers: dict[str, str] | None = None,
):
    return client.post(
        "/api/v1/agent/transcription/drafts",
        headers=headers,
        files={"audio": (filename, audio, mime)},
        data={
            "binding_id": str(binding_id),
            "target_conversation_id": str(conversation_id),
        },
    )


def _swap_provider(client: TestClient, provider: FakeTranscriptionProvider) -> None:
    client.app.state.transcription_provider = provider


def _stage_in(client: TestClient, directory: str) -> None:
    client.app.state.transcription_staging_dir = directory


async def _get_draft(repositories: RepositoryBundle, draft_id: UUID) -> TranscriptDraft | None:
    return await repositories.transcript_drafts.get_by_id(draft_id)


async def _expire_draft(
    repositories: RepositoryBundle,
    session_factory: Any,
    draft_id: UUID,
) -> None:
    async with session_factory() as session:
        draft = await session.get(TranscriptDraft, draft_id)
        draft.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


def _register_native_client(client: TestClient) -> tuple[Any, dict[str, str], str, str]:
    key, jwk = key_and_jwk()
    transaction_id, verifier = begin_authorization(client, jwk)
    approve_authorization(client, transaction_id)
    tokens, nonce = exchange_authorization(client, key, jwk, transaction_id, verifier)
    return key, jwk, str(tokens["access_token"]), nonce


def _native_request(
    client: TestClient,
    key: Any,
    jwk: dict[str, str],
    access_token: str,
    nonce: str,
    method: str,
    path: str,
    **kwargs: Any,
) -> tuple[Any, str]:
    htu = f"http://127.0.0.1:8000{path}"
    for _ in range(2):
        response = client.request(
            method,
            path,
            headers={
                "Authorization": f"Bearer {access_token}",
                "DPoP": proof(
                    key,
                    jwk,
                    method=method,
                    htu=htu,
                    nonce=nonce,
                    access_token=access_token,
                ),
            },
            **kwargs,
        )
        if (
            response.status_code == 401
            and response.json().get("error", {}).get("code") == "use_dpop_nonce"
        ):
            nonce = response.headers["dpop-nonce"]
            continue
        break
    return response, nonce


class TestUploads:
    def test_upload_happy_path_persists_metadata_only(
        self,
        client,
        admin_headers,
        provision_term,
        tmp_path,
        caplog,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        provider = FakeTranscriptionProvider()
        _swap_provider(client, provider)
        _stage_in(client, str(tmp_path))
        audio = _wav_bytes()

        with caplog.at_level("DEBUG"):
            response = _upload(
                client,
                binding_id=binding_id,
                conversation_id=conversation_id,
                audio=audio,
                headers=admin_headers,
            )

        assert response.status_code == 201, response.text
        body = response.json()
        draft_id = UUID(str(body["draft_id"]))
        assert body["state"] == "draft"
        assert body["transcript"] == _TRANSCRIPT
        assert body["provider"] == "fake"
        assert body["region"] == "test-region"
        assert body["language"] == "en"
        assert body["duration_seconds"] == 1.0
        expires_at = datetime.fromisoformat(str(body["expires_at"]))
        assert expires_at > datetime.now(UTC) + timedelta(minutes=50)

        # The provider received the raw bounded audio once, with the sniffed MIME.
        assert len(provider.calls) == 1
        assert provider.calls[0][0] == audio
        assert provider.calls[0][1] == "audio/wav"

        # The draft row holds only hash + metadata: no audio bytes anywhere on
        # disk (database, WAL, or staged temp file) and nothing in the logs.
        draft = client.portal.call(_get_draft, client.app.state.repositories, draft_id)
        assert draft is not None
        assert draft.state == "draft"
        assert draft.owner_actor_id == "admin"
        assert draft.provider == "fake"
        assert draft.region == "test-region"
        assert draft.transcript_hash == hashlib.sha256(_TRANSCRIPT.encode()).hexdigest()
        assert draft.target_conversation_id == conversation_id
        assert draft.binding_id == binding_id
        assert timedelta(minutes=59) <= draft.expires_at - draft.created_at <= timedelta(hours=1)
        for path in tmp_path.iterdir():
            assert audio not in path.read_bytes(), f"audio leaked into {path}"
        assert repr(audio) not in caplog.text

    def test_upload_requires_auth_binding_and_conversation(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        _swap_provider(client, FakeTranscriptionProvider())
        audio = _wav_bytes()

        anonymous = _upload(
            client,
            binding_id=binding_id,
            conversation_id=conversation_id,
            audio=audio,
        )
        assert anonymous.status_code == 401

        missing_binding = _upload(
            client,
            binding_id=uuid4(),
            conversation_id=conversation_id,
            audio=audio,
            headers=admin_headers,
        )
        assert missing_binding.status_code == 404
        assert missing_binding.json()["error"]["code"] == "binding_not_found"

        missing_conversation = _upload(
            client,
            binding_id=binding_id,
            conversation_id=uuid4(),
            audio=audio,
            headers=admin_headers,
        )
        assert missing_conversation.status_code == 404
        assert missing_conversation.json()["error"]["code"] == "conversation_not_found"

        other_binding = _seed_binding(client, admin_headers, provision_term)
        mismatched = _upload(
            client,
            binding_id=other_binding,
            conversation_id=conversation_id,
            audio=audio,
            headers=admin_headers,
        )
        assert mismatched.status_code == 422
        assert mismatched.json()["error"]["code"] == "binding_conversation_mismatch"

    def test_upload_rejects_oversized_audio(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        _swap_provider(client, FakeTranscriptionProvider())
        oversized = _webm_bytes(size=10 * 1024 * 1024 + 1)

        response = _upload(
            client,
            binding_id=binding_id,
            conversation_id=UUID(str(conversation["conversation_id"])),
            audio=oversized,
            mime="audio/webm",
            filename="speech.webm",
            headers=admin_headers,
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "audio_too_large"

    def test_upload_rejects_unsupported_mime(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        _swap_provider(client, FakeTranscriptionProvider())

        response = _upload(
            client,
            binding_id=binding_id,
            conversation_id=UUID(str(conversation["conversation_id"])),
            audio=b"GIF89a-not-an-audio-file",
            mime="audio/wav",
            filename="speech.wav",
            headers=admin_headers,
        )
        assert response.status_code == 415
        assert response.json()["error"]["code"] == "unsupported_audio_format"

    def test_upload_rejects_wav_out_of_bounds(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        conversation_id = UUID(str(conversation["conversation_id"]))
        _swap_provider(client, FakeTranscriptionProvider())

        fast = _upload(
            client,
            binding_id=binding_id,
            conversation_id=conversation_id,
            audio=_wav_bytes(sample_rate=192000),
            headers=admin_headers,
        )
        assert fast.status_code == 422
        assert fast.json()["error"]["code"] == "audio_sample_rate_out_of_bounds"

        long = _upload(
            client,
            binding_id=binding_id,
            conversation_id=conversation_id,
            audio=_wav_bytes(sample_rate=8000, duration_seconds=601),
            headers=admin_headers,
        )
        assert long.status_code == 422
        assert long.json()["error"]["code"] == "audio_too_long"

    def test_upload_rejects_provider_duration_out_of_bounds(
        self,
        client,
        admin_headers,
        provision_term,
        tmp_path,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        _swap_provider(
            client,
            FakeTranscriptionProvider(
                result=TranscriptionResult(
                    transcript=_TRANSCRIPT,
                    provider="fake",
                    region="test-region",
                    duration_seconds=700.0,
                )
            ),
        )
        _stage_in(client, str(tmp_path))

        response = _upload(
            client,
            binding_id=binding_id,
            conversation_id=UUID(str(conversation["conversation_id"])),
            audio=_wav_bytes(),
            headers=admin_headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "audio_too_long"
        assert list(tmp_path.glob("termflow-audio-*")) == []

    def test_provider_unavailable_returns_503(self, client, admin_headers, provision_term) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)

        response = _upload(
            client,
            binding_id=binding_id,
            conversation_id=UUID(str(conversation["conversation_id"])),
            audio=_wav_bytes(),
            headers=admin_headers,
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "speech_to_text_unavailable"

    def test_text_chat_unaffected_without_stt(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        assert conversation["status"] == "active"
        listed = client.get(
            f"/api/v1/agent/conversations?binding_id={binding_id}",
            headers=admin_headers,
        )
        assert listed.status_code == 200

    def test_provider_failure_returns_502_and_cleans_temp(
        self,
        client,
        admin_headers,
        provision_term,
        tmp_path,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        _swap_provider(
            client,
            FakeTranscriptionProvider(error=TranscriptionProviderError("backend exploded")),
        )
        _stage_in(client, str(tmp_path))

        response = _upload(
            client,
            binding_id=binding_id,
            conversation_id=UUID(str(conversation["conversation_id"])),
            audio=_wav_bytes(),
            headers=admin_headers,
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "transcription_failed"
        assert list(tmp_path.glob("termflow-audio-*")) == []

    def test_provider_timeout_returns_504_and_cleans_temp(
        self,
        client,
        admin_headers,
        provision_term,
        tmp_path,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        _swap_provider(client, FakeTranscriptionProvider(delay=1.0))
        _stage_in(client, str(tmp_path))
        client.app.state.transcription_timeout_seconds = 0.05

        response = _upload(
            client,
            binding_id=binding_id,
            conversation_id=UUID(str(conversation["conversation_id"])),
            audio=_wav_bytes(),
            headers=admin_headers,
        )
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "transcription_timeout"
        assert list(tmp_path.glob("termflow-audio-*")) == []


class TestDraftActions:
    def _draft(self, client, admin_headers, provision_term) -> UUID:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        _swap_provider(client, FakeTranscriptionProvider())
        response = _upload(
            client,
            binding_id=binding_id,
            conversation_id=UUID(str(conversation["conversation_id"])),
            audio=_wav_bytes(),
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        return UUID(str(response.json()["draft_id"]))

    def test_confirm_once_by_owner_cas(self, client, admin_headers, provision_term) -> None:
        draft_id = self._draft(client, admin_headers, provision_term)

        confirmed = client.post(
            f"/api/v1/agent/transcription/drafts/{draft_id}/confirm",
            headers=admin_headers,
        )
        assert confirmed.status_code == 204

        draft = client.portal.call(_get_draft, client.app.state.repositories, draft_id)
        assert draft is not None
        assert draft.state == "confirmed"

        second = client.post(
            f"/api/v1/agent/transcription/drafts/{draft_id}/confirm",
            headers=admin_headers,
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "draft_not_confirmable"

    def test_confirm_by_different_user_rejected(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        draft_id = self._draft(client, admin_headers, provision_term)
        key, jwk, access_token, nonce = _register_native_client(client)

        response, _ = _native_request(
            client,
            key,
            jwk,
            access_token,
            nonce,
            "POST",
            f"/api/v1/agent/transcription/drafts/{draft_id}/confirm",
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "draft_not_owner"

        draft = client.portal.call(_get_draft, client.app.state.repositories, draft_id)
        assert draft is not None
        assert draft.state == "draft"

    def test_native_client_confirms_own_draft(
        self,
        client,
        admin_headers,
        provision_term,
    ) -> None:
        binding_id = _seed_binding(client, admin_headers, provision_term)
        conversation = _create_conversation(client, admin_headers, binding_id=binding_id)
        key, jwk, access_token, nonce = _register_native_client(client)
        _swap_provider(client, FakeTranscriptionProvider())

        upload, nonce = _native_request(
            client,
            key,
            jwk,
            access_token,
            nonce,
            "POST",
            "/api/v1/agent/transcription/drafts",
            files={"audio": ("speech.wav", _wav_bytes(), "audio/wav")},
            data={
                "binding_id": str(binding_id),
                "target_conversation_id": str(conversation["conversation_id"]),
            },
        )
        assert upload.status_code == 201, upload.text
        draft_id = UUID(str(upload.json()["draft_id"]))

        confirmed, _ = _native_request(
            client,
            key,
            jwk,
            access_token,
            nonce,
            "POST",
            f"/api/v1/agent/transcription/drafts/{draft_id}/confirm",
        )
        assert confirmed.status_code == 204, confirmed.text

    def test_confirm_expired_draft_rejected(self, client, admin_headers, provision_term) -> None:
        draft_id = self._draft(client, admin_headers, provision_term)
        client.portal.call(
            _expire_draft,
            client.app.state.repositories,
            client.app.state.session_factory,
            draft_id,
        )

        response = client.post(
            f"/api/v1/agent/transcription/drafts/{draft_id}/confirm",
            headers=admin_headers,
        )
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "draft_expired"

    def test_cancel_draft(self, client, admin_headers, provision_term) -> None:
        draft_id = self._draft(client, admin_headers, provision_term)

        cancelled = client.post(
            f"/api/v1/agent/transcription/drafts/{draft_id}/cancel",
            headers=admin_headers,
        )
        assert cancelled.status_code == 204

        draft = client.portal.call(_get_draft, client.app.state.repositories, draft_id)
        assert draft is not None
        assert draft.state == "cancelled"

        second = client.post(
            f"/api/v1/agent/transcription/drafts/{draft_id}/cancel",
            headers=admin_headers,
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "draft_not_cancellable"

        confirm = client.post(
            f"/api/v1/agent/transcription/drafts/{draft_id}/confirm",
            headers=admin_headers,
        )
        assert confirm.status_code == 409
        assert confirm.json()["error"]["code"] == "draft_not_confirmable"

    def test_get_draft_is_owner_only(self, client, admin_headers, provision_term) -> None:
        draft_id = self._draft(client, admin_headers, provision_term)

        detail = client.get(
            f"/api/v1/agent/transcription/drafts/{draft_id}",
            headers=admin_headers,
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["draft_id"] == str(draft_id)
        assert body["state"] == "draft"
        assert body["provider"] == "fake"
        assert body["region"] == "test-region"
        assert body["transcript_hash"] == hashlib.sha256(_TRANSCRIPT.encode()).hexdigest()

        key, jwk, access_token, nonce = _register_native_client(client)
        foreign, _ = _native_request(
            client,
            key,
            jwk,
            access_token,
            nonce,
            "GET",
            f"/api/v1/agent/transcription/drafts/{draft_id}",
        )
        assert foreign.status_code == 403
        assert foreign.json()["error"]["code"] == "draft_not_owner"

        missing = client.get(
            f"/api/v1/agent/transcription/drafts/{uuid4()}",
            headers=admin_headers,
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "draft_not_found"
