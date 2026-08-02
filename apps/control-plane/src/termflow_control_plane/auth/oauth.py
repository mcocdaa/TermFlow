"""Native public-client authorization, consent, and token lifecycle."""

from __future__ import annotations

import hmac
import json
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
    return f"{authorization.redirect_uri}?{query}"


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
            response_types_supported=["code"],
            grant_types_supported=["authorization_code", "refresh_token"],
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
        client = await self._repositories.native_clients.get_active_by_thumbprint(calculated_jkt)
        if client is None:
            client = await self._repositories.native_clients.create(
                display_name=request.client_name,
                public_jwk=_public_jwk_json(request.public_jwk),
                key_thumbprint=calculated_jkt,
                platform=request.platform,
                client_version=request.client_version,
                scopes=tuple(request.scopes),
            )
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

    async def preview(self, transaction_id: UUID) -> OAuthAuthorizationPreviewResponse:
        state = await self._repositories.auth_state.get()
        authorization = await self._repositories.oauth_authorizations.get_active_id(
            transaction_id,
            epoch=state.epoch,
            now=self._clock(),
        )
        if authorization is None:
            raise TermFlowError("authorization_expired", 404, "Authorization is unavailable.")
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
            redirect_uri=authorization.redirect_uri,
            totp_required=state.totp_enabled_at is not None,
            expires_at=authorization.expires_at,
        )

    async def decide(
        self,
        request: OAuthAuthorizationDecisionRequest,
    ) -> OAuthAuthorizationDecisionResponse:
        supplied = request.admin_token.get_secret_value()
        expected = self._settings.admin_token.get_secret_value()
        if not hmac.compare_digest(supplied, expected):
            raise TermFlowError("authentication_failed", 401, "Authentication failed.")
        state = await self._repositories.auth_state.get()
        if state.totp_enabled_at is not None:
            code = request.totp_code
            if code is None or self._totp_verifier is None:
                raise TermFlowError("authentication_failed", 401, "Authentication failed.")
            if not await self._totp_verifier(code.get_secret_value()):
                raise TermFlowError("authentication_failed", 401, "Authentication failed.")
        authorization = await self._repositories.oauth_authorizations.get_active_id(
            request.transaction_id,
            epoch=state.epoch,
            now=self._clock(),
        )
        if authorization is None:
            raise TermFlowError("authorization_expired", 400, "Authorization is unavailable.")
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

    async def revoke(self, raw_token: SecretStr) -> OAuthRevokeResponse:
        await self._repositories.auth_tokens.revoke(raw_token.get_secret_value())
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
