"""Native public-client authorization, consent, and token lifecycle."""

from __future__ import annotations

import hmac
import json
import math
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlencode
from uuid import UUID

from pydantic import SecretStr
from termflow_protocol import (
    NativeClientResponse,
    OAuthAuthorizationDecisionRequest,
    OAuthAuthorizationDecisionResponse,
    OAuthAuthorizationPreviewResponse,
    OAuthAuthorizationRequest,
    OAuthDeviceCodeRequest,
    OAuthDeviceCodeResponse,
    OAuthMetadataResponse,
    OAuthPublicJwk,
    OAuthRevokeResponse,
    OAuthScope,
    OAuthTokenRequest,
    OAuthTokenResponse,
)
from termflow_protocol.http import OAUTH_SCOPES

from termflow_control_plane.config import Settings
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import NativeClient, OAuthAuthorization
from termflow_control_plane.persistence.repositories import (
    CreatedDeviceAuthorization,
    DeviceAuthorizationRequest,
    RepositoryBundle,
    decode_scopes,
)

from .dpop import DpopInvalid, DpopNonceRequired, DpopVerifier, VerifiedDpop, jwk_thumbprint
from .pkce import create_s256_challenge

type FreshTotpVerifier = Callable[[str], Awaitable[bool]]


def _canonical_issuer(settings: Settings) -> str:
    return str(settings.public_base_url).rstrip("/")


def _public_jwk_json(jwk: OAuthPublicJwk) -> str:
    return json.dumps(jwk.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)


def _protocol_scopes(encoded: str) -> list[OAuthScope]:
    return cast(list[OAuthScope], list(decode_scopes(encoded)))


def _callback_uri(authorization: OAuthAuthorization) -> str:
    query = urlencode(
        {
            "state": authorization.request_state,
            "transaction_id": str(authorization.id),
        }
    )
    redirect_uri = authorization.redirect_uri or "termflow://auth/callback"
    return f"{redirect_uri}?{query}"


class OAuthService:
    """Coordinate repositories without ever returning the server-side code."""

    def __init__(
        self,
        repositories: RepositoryBundle,
        settings: Settings,
        dpop: DpopVerifier,
        *,
        totp_verifier: FreshTotpVerifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repositories = repositories
        self._settings = settings
        self._dpop = dpop
        self._totp_verifier = totp_verifier
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def issuer(self) -> str:
        return _canonical_issuer(self._settings)

    def metadata(self) -> OAuthMetadataResponse:
        return OAuthMetadataResponse(
            issuer=self.issuer,
            authorization_endpoint=f"{self.issuer}/api/v1/oauth/authorize",
            token_endpoint=f"{self.issuer}/api/v1/oauth/token",
            revocation_endpoint=f"{self.issuer}/api/v1/oauth/revoke",
            device_authorization_endpoint=f"{self.issuer}/api/v1/oauth/device/code",
            device_verification_uri=f"{self.issuer}/device",
            response_types_supported=["code"],
            grant_types_supported=[
                "authorization_code",
                "refresh_token",
                "urn:ietf:params:oauth:grant-type:device_code",
            ],
            code_challenge_methods_supported=["S256"],
            dpop_signing_alg_values_supported=["ES256"],
            scopes_supported=list(OAUTH_SCOPES),
        )

    async def begin(self, request: OAuthAuthorizationRequest) -> UUID:
        jwk = request.public_jwk.model_dump(mode="json")
        calculated_jkt = jwk_thumbprint(jwk)
        if not hmac.compare_digest(calculated_jkt, request.dpop_jkt):
            raise TermFlowError("invalid_request", 400, "The authorization request is invalid.")
        state = await self._repositories.auth_state.get()
        client = await self._repositories.native_clients.get_or_create(
            display_name=request.client_name,
            public_jwk=_public_jwk_json(request.public_jwk),
            key_thumbprint=calculated_jkt,
            platform=request.platform,
            client_version=request.client_version,
            scopes=tuple(request.scopes),
        )
        if client.revoked_at is not None:
            raise TermFlowError("client_revoked", 403, "The native client is revoked.")
        transaction_secret = secrets.token_urlsafe(32)
        return await self._repositories.oauth_authorizations.create(
            transaction_secret=transaction_secret,
            client_id=client.id,
            redirect_uri=request.redirect_uri,
            request_state=request.state,
            scopes=tuple(request.scopes),
            pkce_challenge=request.code_challenge,
            expires_at=self._clock()
            + timedelta(seconds=self._settings.oauth_authorization_ttl_seconds),
            epoch=state.epoch,
        )

    async def begin_device(self, request: OAuthDeviceCodeRequest) -> OAuthDeviceCodeResponse:
        """Create the shared authorization transaction for the device grant."""

        jwk = request.public_jwk.model_dump(mode="json")
        calculated_jkt = jwk_thumbprint(jwk)
        if not hmac.compare_digest(calculated_jkt, request.dpop_jkt):
            raise TermFlowError("invalid_request", 400, "The authorization request is invalid.")
        state = await self._repositories.auth_state.get()
        client = await self._repositories.native_clients.get_or_create(
            display_name=request.client_name,
            public_jwk=_public_jwk_json(request.public_jwk),
            key_thumbprint=calculated_jkt,
            platform=request.platform,
            client_version=request.client_version,
            scopes=tuple(request.scopes),
        )
        if client.revoked_at is not None:
            raise TermFlowError("client_revoked", 403, "The native client is revoked.")
        now = self._clock()
        ttl_seconds = self._settings.oauth_device_authorization_ttl_seconds
        interval = self._settings.oauth_device_poll_interval_seconds
        if ttl_seconds <= 0 or interval <= 0:
            raise TermFlowError("invalid_request", 400, "The authorization request is invalid.")
        created: CreatedDeviceAuthorization = (
            await self._repositories.oauth_authorizations.create_device_authorization(
                DeviceAuthorizationRequest(
                    client_id=client.id,
                    scopes=tuple(request.scopes),
                    pkce_challenge=request.code_challenge,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                    epoch=state.epoch,
                    interval=interval,
                    # Keep the existing Web C decision response contract for the
                    # shared transaction; the device client never follows it.
                    redirect_uri="termflow://auth/callback",
                    request_state=secrets.token_urlsafe(32),
                ),
                now=now,
            )
        )
        verification_uri = f"{self.issuer}/device"
        complete = f"{verification_uri}?{urlencode({'code': created.user_code})}"
        expires_in = max(1, math.ceil((created.expires_at - now).total_seconds()))
        return OAuthDeviceCodeResponse(
            device_code=created.device_code,
            user_code=created.user_code,
            verification_uri=verification_uri,
            verification_uri_complete=complete,
            expires_in=expires_in,
            interval=created.interval,
        )

    async def preview(self, transaction_id: UUID) -> OAuthAuthorizationPreviewResponse:
        state = await self._repositories.auth_state.get()
        authorization = await self._repositories.oauth_authorizations.get_active_id(
            transaction_id,
            epoch=state.epoch,
            now=self._clock(),
        )
        if authorization is None:
            raise TermFlowError("authorization_expired", 404, "Authorization is unavailable.")
        return await self._preview_authorization(authorization, state.totp_enabled_at is not None)

    async def preview_user_code(self, user_code: str) -> OAuthAuthorizationPreviewResponse:
        state = await self._repositories.auth_state.get()
        authorization = await self._repositories.oauth_authorizations.find_by_user_code(
            user_code,
            epoch=state.epoch,
            now=self._clock(),
        )
        if authorization is None:
            raise TermFlowError("authorization_expired", 404, "Authorization is unavailable.")
        return await self._preview_authorization(authorization, state.totp_enabled_at is not None)

    async def _preview_authorization(
        self,
        authorization: OAuthAuthorization,
        totp_required: bool,
    ) -> OAuthAuthorizationPreviewResponse:
        client = await self._repositories.native_clients.get(authorization.client_id)
        if client is None or client.revoked_at is not None:
            raise TermFlowError("authorization_expired", 404, "Authorization is unavailable.")
        return OAuthAuthorizationPreviewResponse(
            transaction_id=authorization.id,
            issuer=self.issuer,
            client_name=client.display_name,
            platform=client.platform or "unknown",
            client_version=client.client_version,
            key_fingerprint=client.key_thumbprint,
            scopes=_protocol_scopes(authorization.scopes),
            redirect_uri=authorization.redirect_uri or "termflow://auth/callback",
            totp_required=totp_required,
            expires_at=authorization.expires_at,
        )

    async def decide(
        self,
        request: OAuthAuthorizationDecisionRequest,
        *,
        session_authenticated: bool = False,
    ) -> OAuthAuthorizationDecisionResponse:
        state = await self._repositories.auth_state.get()
        supplied = (
            request.admin_token.get_secret_value() if request.admin_token is not None else None
        )
        expected = self._settings.admin_token.get_secret_value()
        authenticated = (
            session_authenticated
            if supplied is None
            else hmac.compare_digest(supplied, expected)
        )
        if not authenticated:
            await self._record_decision_failure(request.transaction_id, state.epoch)
            raise TermFlowError("authentication_failed", 401, "Authentication failed.")
        authorization = await self._repositories.oauth_authorizations.get_active_id(
            request.transaction_id,
            epoch=state.epoch,
            now=self._clock(),
        )
        if authorization is None:
            raise TermFlowError("authorization_expired", 400, "Authorization is unavailable.")
        if state.totp_enabled_at is not None:
            code = request.totp_code
            if code is None or self._totp_verifier is None:
                await self._record_decision_failure(request.transaction_id, state.epoch)
                raise TermFlowError("authentication_failed", 401, "Authentication failed.")
            try:
                accepted = await self._totp_verifier(code.get_secret_value())
            except Exception:
                accepted = False
            if not accepted:
                await self._record_decision_failure(request.transaction_id, state.epoch)
                raise TermFlowError("authentication_failed", 401, "Authentication failed.")
        if authorization.device_code_digest is not None:
            if request.decision == "deny":
                denied = await self._repositories.oauth_authorizations.deny_device_authorization(
                    request.transaction_id,
                    epoch=state.epoch,
                    now=self._clock(),
                )
                if denied is None:
                    raise TermFlowError(
                        "authorization_expired", 400, "Authorization is unavailable."
                    )
                return OAuthAuthorizationDecisionResponse(
                    status="denied",
                    callback_uri=_callback_uri(denied),
                )
            approved = await self._repositories.oauth_authorizations.mark_approved(
                request.transaction_id,
                epoch=state.epoch,
                now=self._clock(),
            )
            if approved is None:
                raise TermFlowError("authorization_expired", 400, "Authorization is unavailable.")
            if not await self._repositories.native_clients.update_scopes(
                authorization.client_id,
                decode_scopes(authorization.scopes),
            ):
                raise TermFlowError("authorization_expired", 400, "Authorization is unavailable.")
            return OAuthAuthorizationDecisionResponse(
                status="approved",
                callback_uri=_callback_uri(approved),
            )
        if request.decision == "deny":
            denied = await self._repositories.oauth_authorizations.deny(
                request.transaction_id,
                epoch=state.epoch,
                now=self._clock(),
            )
            if denied is None:
                raise TermFlowError("authorization_expired", 400, "Authorization is unavailable.")
            return OAuthAuthorizationDecisionResponse(
                status="denied",
                callback_uri=_callback_uri(denied),
            )
        internal_code = secrets.token_urlsafe(32)
        issued = await self._repositories.oauth_authorizations.issue_code(
            authorization.id,
            internal_code,
            epoch=state.epoch,
            code_ttl_seconds=self._settings.oauth_authorization_code_ttl_seconds,
        )
        if not issued:
            raise TermFlowError("authorization_expired", 400, "Authorization is unavailable.")
        if not await self._repositories.native_clients.update_scopes(
            authorization.client_id,
            decode_scopes(authorization.scopes),
        ):
            raise TermFlowError("authorization_expired", 400, "Authorization is unavailable.")
        approved = await self._repositories.oauth_authorizations.get_active_id(
            authorization.id,
            epoch=state.epoch,
            now=self._clock(),
        )
        if approved is None:
            raise TermFlowError("authorization_expired", 400, "Authorization is unavailable.")
        return OAuthAuthorizationDecisionResponse(
            status="approved",
            callback_uri=_callback_uri(approved),
        )

    async def _record_decision_failure(self, transaction_id: UUID, epoch: int) -> None:
        await self._repositories.oauth_authorizations.record_failure(
            transaction_id,
            epoch=epoch,
            maximum=self._settings.auth_max_challenge_attempts,
            now=self._clock(),
        )

    def verify_token_proof(
        self,
        proof: str,
        public_jwk: OAuthPublicJwk,
    ) -> VerifiedDpop:
        expected_jkt = jwk_thumbprint(public_jwk.model_dump(mode="json"))
        return self._dpop.verify(
            proof,
            method="POST",
            htu=f"{self.issuer}/api/v1/oauth/token",
            expected_jkt=expected_jkt,
        )

    async def exchange(
        self,
        request: OAuthTokenRequest,
        verified: VerifiedDpop,
    ) -> OAuthTokenResponse:
        state = await self._repositories.auth_state.get()
        now = self._clock()
        if request.grant_type == "urn:ietf:params:oauth:grant-type:device_code":
            assert request.device_code is not None and request.code_verifier is not None
            return await self._exchange_device_code(request, verified, state.epoch, now)
        raw_access = secrets.token_urlsafe(32)
        raw_refresh = secrets.token_urlsafe(48)
        access_expires_at = now + timedelta(seconds=self._settings.auth_access_token_ttl_seconds)
        refresh_expires_at = now + timedelta(seconds=self._settings.auth_refresh_token_ttl_seconds)
        if request.grant_type == "authorization_code":
            assert request.transaction_id is not None and request.code_verifier is not None
            verifier = request.code_verifier.get_secret_value()
            try:
                challenge = create_s256_challenge(verifier)
            except ValueError as exc:
                raise TermFlowError("invalid_grant", 400, "The grant is invalid.") from exc
            exchanged = await self._repositories.oauth_authorizations.exchange_transaction(
                request.transaction_id,
                epoch=state.epoch,
                raw_access_token=raw_access,
                raw_refresh_token=raw_refresh,
                key_thumbprint=verified.jkt,
                pkce_challenge=challenge,
                access_expires_at=access_expires_at,
                refresh_expires_at=refresh_expires_at,
                now=now,
            )
            if exchanged is None:
                raise TermFlowError("invalid_grant", 400, "The grant is invalid.")
            scopes = decode_scopes(exchanged.authorization.scopes)
        else:
            assert request.refresh_token is not None
            rotated = await self._repositories.auth_tokens.rotate_refresh_pair(
                request.refresh_token.get_secret_value(),
                raw_refresh,
                raw_access,
                access_expires_at=access_expires_at,
                refresh_expires_at=refresh_expires_at,
                epoch=state.epoch,
                key_thumbprint=verified.jkt,
                now=now,
            )
            if rotated is None:
                raise TermFlowError("invalid_grant", 400, "The grant is invalid.")
            scopes = decode_scopes(rotated.access_token.scopes)
        return OAuthTokenResponse(
            access_token=raw_access,
            expires_in=self._settings.auth_access_token_ttl_seconds,
            refresh_token=raw_refresh,
            scopes=cast(list[OAuthScope], list(scopes)),
        )

    async def _exchange_device_code(
        self,
        request: OAuthTokenRequest,
        verified: VerifiedDpop,
        epoch: int,
        now: datetime,
    ) -> OAuthTokenResponse:
        assert request.device_code is not None and request.code_verifier is not None
        raw_device_code = request.device_code.get_secret_value()
        authorization = await self._repositories.oauth_authorizations.find_by_device_code(
            raw_device_code,
            epoch=epoch,
            now=now,
        )
        if authorization is None:
            terminal = await self._repositories.oauth_authorizations.get_device_authorization(
                raw_device_code,
                epoch=epoch,
            )
            if terminal is not None and terminal.device_status == "denied":
                raise TermFlowError("access_denied", 400, "The authorization was denied.")
            raise TermFlowError("expired_token", 400, "The device authorization has expired.")
        client = await self._repositories.native_clients.get(authorization.client_id)
        if client is None or client.revoked_at is not None:
            raise TermFlowError("expired_token", 400, "The device authorization has expired.")
        if not hmac.compare_digest(client.key_thumbprint, verified.jkt):
            raise TermFlowError("invalid_grant", 400, "The grant is invalid.")
        interval = (
            authorization.device_interval or self._settings.oauth_device_poll_interval_seconds
        )
        retry_after = await self._repositories.oauth_authorizations.record_device_poll(
            raw_device_code,
            epoch=epoch,
            interval=interval,
            now=now,
        )
        if retry_after is not None:
            raise TermFlowError(
                "slow_down",
                400,
                "Polling too frequently.",
                retry_after=retry_after,
            )
        if authorization.device_status == "pending":
            raise TermFlowError(
                "authorization_pending",
                400,
                "The authorization is pending.",
                retry_after=interval,
            )
        raw_access = secrets.token_urlsafe(32)
        raw_refresh = secrets.token_urlsafe(48)
        access_expires_at = now + timedelta(seconds=self._settings.auth_access_token_ttl_seconds)
        refresh_expires_at = now + timedelta(seconds=self._settings.auth_refresh_token_ttl_seconds)
        exchanged = await self._repositories.oauth_authorizations.exchange_device_code_with_tokens(
            raw_device_code,
            request.code_verifier.get_secret_value(),
            epoch=epoch,
            now=now,
            raw_access_token=raw_access,
            raw_refresh_token=raw_refresh,
            key_thumbprint=verified.jkt,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
        )
        if exchanged is None:
            terminal = await self._repositories.oauth_authorizations.get_device_authorization(
                raw_device_code,
                epoch=epoch,
            )
            if terminal is not None and terminal.device_status == "denied":
                raise TermFlowError("access_denied", 400, "The authorization was denied.")
            raise TermFlowError("invalid_grant", 400, "The grant is invalid.")
        scopes = decode_scopes(exchanged.authorization.scopes)
        return OAuthTokenResponse(
            access_token=raw_access,
            expires_in=self._settings.auth_access_token_ttl_seconds,
            refresh_token=raw_refresh,
            scopes=cast(list[OAuthScope], list(scopes)),
        )

    async def revoke(
        self,
        raw_token: SecretStr,
        *,
        client_id: UUID,
        key_thumbprint: str,
    ) -> OAuthRevokeResponse:
        await self._repositories.auth_tokens.revoke_owned_family(
            raw_token.get_secret_value(),
            client_id=client_id,
            key_thumbprint=key_thumbprint,
        )
        return OAuthRevokeResponse()


def client_response(client: NativeClient) -> NativeClientResponse:
    return NativeClientResponse(
        client_id=client.id,
        display_name=client.display_name,
        platform=client.platform or "unknown",
        client_version=client.client_version,
        key_thumbprint=client.key_thumbprint,
        scopes=_protocol_scopes(client.scopes),
        created_at=client.created_at,
        last_used_at=client.last_used_at,
        revoked_at=client.revoked_at,
    )


__all__ = [
    "DpopInvalid",
    "DpopNonceRequired",
    "FreshTotpVerifier",
    "OAuthService",
    "client_response",
]
