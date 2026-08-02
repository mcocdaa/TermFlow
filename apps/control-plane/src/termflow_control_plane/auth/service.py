"""Authentication orchestration for browser sessions and optional TOTP."""

from __future__ import annotations

import base64
import secrets
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

from termflow_control_plane.auth.secret_box import AesGcmSecretBox, EncryptedSecret
from termflow_control_plane.auth.tokens import issue_token, secret_text_matches
from termflow_control_plane.auth.totp import match_totp_counter
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.models import AuthenticationState
from termflow_control_plane.persistence.repositories import (
    AuthenticationStateChanged,
    RepositoryBundle,
)

_SETUP_PURPOSE = "totp-setup"
_AUTHENTICATOR_PURPOSE = "totp-authenticator"
_WEB_LOGIN_PURPOSE = "web-login-challenge"
_WEB_LOGIN_KIND = "web_session_totp"
_SECRET_BYTES = 20


class AuthenticationRejected(Exception):
    """A deliberately detail-free authentication or challenge rejection."""


class TotpUnavailable(Exception):
    """The independent TOTP encryption key is not configured."""


@dataclass(frozen=True, slots=True)
class WebLoginChallenge:
    challenge_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TotpSetupMaterial:
    setup_id: UUID
    expires_at: datetime
    setup_key: str = field(repr=False)
    provisioning_uri: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class IssuedCliToken:
    access_token: str = field(repr=False)
    expires_at: datetime
    expires_in: int
    scopes: tuple[str, ...]


class AuthenticationService:
    """Keep primary, TOTP, replay, and encrypted-transaction rules together."""

    def __init__(
        self,
        repositories: RepositoryBundle,
        settings: Settings,
        *,
        secret_box: AesGcmSecretBox | None,
        clock: Callable[[], datetime] | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._repositories = repositories
        self._settings = settings
        self._secret_box = secret_box
        self._clock = clock or (lambda: datetime.now(UTC))
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return (
            "AuthenticationService("
            f"totp_available={self._secret_box is not None}, credentials=<redacted>)"
        )

    def primary_token_matches(self, supplied: str) -> bool:
        return secret_text_matches(
            supplied,
            self._settings.admin_token.get_secret_value(),
        )

    async def totp_status(self) -> tuple[bool, bool]:
        state = await self._repositories.auth_state.get()
        return state.totp_enabled_at is not None, self._secret_box is not None

    async def begin_web_login(self, admin_token: str) -> WebLoginChallenge | None:
        state = await self._repositories.auth_state.get()
        primary_matches = self.primary_token_matches(admin_token)
        if state.totp_enabled_at is None:
            if not primary_matches:
                raise AuthenticationRejected
            return None
        box = self._required_box(authentication=True)
        if self._enabled_secret(state, box) is None:
            raise AuthenticationRejected
        if not primary_matches:
            raise AuthenticationRejected
        expires_at = self._clock() + timedelta(
            seconds=self._settings.auth_challenge_ttl_seconds
        )
        context = box.encrypt(
            struct.pack(">Q", state.totp_generation),
            purpose=_WEB_LOGIN_PURPOSE,
        )
        challenge_id = await self._repositories.auth_challenges.create(
            _WEB_LOGIN_KIND,
            context,
            expires_at=expires_at,
            epoch=state.epoch,
        )
        return WebLoginChallenge(challenge_id=challenge_id, expires_at=expires_at)

    async def complete_web_login(self, challenge_id: UUID, code: str) -> bool:
        state = await self._repositories.auth_state.get()
        box = self._required_box(authentication=True)
        encrypted_context = await self._repositories.auth_challenges.get_active(
            challenge_id,
            _WEB_LOGIN_KIND,
            epoch=state.epoch,
            now=self._clock(),
        )
        secret = self._enabled_secret(state, box)
        if encrypted_context is None or secret is None:
            return False
        try:
            context = box.decrypt(encrypted_context, purpose=_WEB_LOGIN_PURPOSE)
            (generation,) = struct.unpack(">Q", context)
        except (ValueError, struct.error):
            return False
        if generation != state.totp_generation:
            return False
        counter = match_totp_counter(secret, code, self._clock())
        if counter is None:
            await self._repositories.auth_challenges.fail_attempt(
                challenge_id,
                maximum=self._settings.auth_max_challenge_attempts,
                now=self._clock(),
            )
            return False
        accepted = await self._repositories.auth_state.accept_totp_counter(
            counter,
            epoch=state.epoch,
            generation=state.totp_generation,
        )
        if not accepted:
            await self._repositories.auth_challenges.fail_attempt(
                challenge_id,
                maximum=self._settings.auth_max_challenge_attempts,
                now=self._clock(),
            )
            return False
        consumed = await self._repositories.auth_challenges.consume(
            challenge_id,
            _WEB_LOGIN_KIND,
            epoch=state.epoch,
            now=self._clock(),
        )
        return consumed is not None

    async def verify_fresh_totp(self, code: str) -> bool:
        state = await self._repositories.auth_state.get()
        return await self._verify_fresh_totp_for_state(state, code)

    async def issue_cli_token(
        self,
        admin_token: str,
        totp_code: str | None,
        scopes: tuple[str, ...],
    ) -> IssuedCliToken:
        """Exchange root credentials for one epoch-bound, short-lived CLI token."""

        state = await self._repositories.auth_state.get()
        if not self.primary_token_matches(admin_token):
            raise AuthenticationRejected
        if state.totp_enabled_at is not None and (
            totp_code is None
            or not await self._verify_fresh_totp_for_state(state, totp_code)
        ):
            raise AuthenticationRejected
        if not scopes or len(scopes) != len(set(scopes)):
            raise AuthenticationRejected
        raw_token = issue_token()
        expires_at = self._clock() + timedelta(
            seconds=self._settings.auth_cli_token_ttl_seconds
        )
        try:
            await self._repositories.auth_tokens.issue(
                raw_token,
                kind="cli",
                scopes=scopes,
                key_thumbprint=None,
                expires_at=expires_at,
                epoch=state.epoch,
            )
        except AuthenticationStateChanged as exc:
            raise AuthenticationRejected from exc
        return IssuedCliToken(
            access_token=raw_token,
            expires_at=expires_at,
            expires_in=self._settings.auth_cli_token_ttl_seconds,
            scopes=scopes,
        )

    async def _verify_fresh_totp_for_state(
        self,
        state: AuthenticationState,
        code: str,
    ) -> bool:
        box = self._required_box(authentication=True)
        secret = self._enabled_secret(state, box)
        if secret is None:
            return False
        counter = match_totp_counter(secret, code, self._clock())
        if counter is None:
            return False
        return await self._repositories.auth_state.accept_totp_counter(
            counter,
            epoch=state.epoch,
            generation=state.totp_generation,
        )

    async def begin_totp_setup(
        self,
        admin_token: str,
        current_code: str | None,
    ) -> TotpSetupMaterial:
        if not self.primary_token_matches(admin_token):
            raise AuthenticationRejected
        box = self._required_box(authentication=False)
        state = await self._repositories.auth_state.get()
        if state.totp_enabled_at is not None:
            if current_code is None or not await self.verify_fresh_totp(current_code):
                raise AuthenticationRejected
            state = await self._repositories.auth_state.get()

        secret = self._random_bytes(_SECRET_BYTES)
        if len(secret) != _SECRET_BYTES:
            raise RuntimeError("the TOTP random source returned the wrong number of bytes")
        encrypted = box.encrypt(
            struct.pack(">Q", state.totp_generation) + secret,
            purpose=_SETUP_PURPOSE,
        )
        expires_at = self._clock() + timedelta(seconds=self._settings.totp_setup_ttl_seconds)
        setup_id = await self._repositories.totp_setups.create(
            encrypted,
            expires_at=expires_at,
            epoch=state.epoch,
        )
        setup_key = base64.b32encode(secret).decode("ascii").rstrip("=")
        issuer_host = urlsplit(str(self._settings.public_base_url)).hostname
        if issuer_host is None:
            raise RuntimeError("public_base_url has no hostname")
        label = quote(f"TermFlow:{issuer_host}", safe="")
        query = urlencode(
            {
                "secret": setup_key,
                "issuer": "TermFlow",
                "algorithm": "SHA1",
                "digits": "6",
                "period": "30",
            }
        )
        return TotpSetupMaterial(
            setup_id=setup_id,
            expires_at=expires_at,
            setup_key=setup_key,
            provisioning_uri=f"otpauth://totp/{label}?{query}",
        )

    async def confirm_totp_setup(self, setup_id: UUID, code: str) -> bool:
        box = self._required_box(authentication=False)
        state = await self._repositories.auth_state.get()
        encrypted = await self._repositories.totp_setups.get_active(
            setup_id,
            epoch=state.epoch,
            now=self._clock(),
        )
        if encrypted is None:
            return False
        unpacked = self._decrypt_setup(box, encrypted)
        if unpacked is None:
            return False
        generation, secret = unpacked
        if generation != state.totp_generation:
            return False
        counter = match_totp_counter(secret, code, self._clock())
        if counter is None:
            return False
        consumed = await self._repositories.totp_setups.consume(
            setup_id,
            epoch=state.epoch,
            now=self._clock(),
        )
        if consumed is None:
            return False
        authenticator = box.encrypt(secret, purpose=_AUTHENTICATOR_PURPOSE)
        return await self._repositories.auth_state.enable_totp(
            authenticator,
            counter,
            expected_epoch=state.epoch,
            expected_generation=state.totp_generation,
        )

    async def disable_totp(self, admin_token: str, code: str) -> bool:
        if not self.primary_token_matches(admin_token):
            raise AuthenticationRejected
        state = await self._repositories.auth_state.get()
        if state.totp_enabled_at is None or not await self.verify_fresh_totp(code):
            raise AuthenticationRejected
        return await self._repositories.auth_state.disable_totp(
            expected_epoch=state.epoch,
            expected_generation=state.totp_generation,
        )

    def _required_box(self, *, authentication: bool) -> AesGcmSecretBox:
        if self._secret_box is not None:
            return self._secret_box
        if authentication:
            raise AuthenticationRejected
        raise TotpUnavailable

    @staticmethod
    def _enabled_secret(
        state: AuthenticationState,
        box: AesGcmSecretBox,
    ) -> bytes | None:
        fields = (
            state.totp_ciphertext,
            state.totp_nonce,
            state.totp_key_version,
            state.totp_aad_version,
        )
        if state.totp_enabled_at is None or any(value is None for value in fields):
            return None
        encrypted = EncryptedSecret(
            ciphertext=state.totp_ciphertext,  # type: ignore[arg-type]
            nonce=state.totp_nonce,  # type: ignore[arg-type]
            key_version=state.totp_key_version,  # type: ignore[arg-type]
            aad_version=state.totp_aad_version,  # type: ignore[arg-type]
        )
        try:
            return box.decrypt(encrypted, purpose=_AUTHENTICATOR_PURPOSE)
        except ValueError:
            return None

    @staticmethod
    def _decrypt_setup(
        box: AesGcmSecretBox,
        encrypted: EncryptedSecret,
    ) -> tuple[int, bytes] | None:
        try:
            payload = box.decrypt(encrypted, purpose=_SETUP_PURPOSE)
        except ValueError:
            return None
        if len(payload) != 8 + _SECRET_BYTES:
            return None
        (generation,) = struct.unpack(">Q", payload[:8])
        return generation, payload[8:]
