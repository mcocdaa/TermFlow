"""HTTP request and response DTOs for TermFlow V1."""

import base64
import binascii
import ipaddress
import re
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .messages import TerminalAction, TerminalBinding, TerminalCloseReason, validate_plain_text
from .topology import TopologySnapshot


class HttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


type OAuthScope = Literal[
    "terminal.read",
    "terminal.write",
    "computers.read",
    "computers.write",
]

OAUTH_SCOPES: tuple[OAuthScope, ...] = (
    "terminal.read",
    "terminal.write",
    "computers.read",
    "computers.write",
)
_ASCII_TOTP = re.compile(r"[0-9]{6}\Z", flags=re.ASCII)
_PKCE_VALUE = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z", flags=re.ASCII)
_BASE64URL_256 = re.compile(r"[A-Za-z0-9_-]{43}\Z", flags=re.ASCII)
_OPAQUE_STATE = re.compile(r"[A-Za-z0-9._~-]{16,256}\Z", flags=re.ASCII)


def validate_editable_name(value: str) -> str:
    if not 1 <= len(value) <= 128:
        raise ValueError("name must contain between 1 and 128 characters")
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
        raise ValueError("name contains unsupported control characters")
    return value


def validate_client_name(value: str) -> str:
    validate_editable_name(value)
    if value != value.strip() or not value.strip():
        raise ValueError("client name must not have leading or trailing whitespace")
    return value


def validate_totp_code(value: object) -> str | SecretStr:
    if isinstance(value, SecretStr):
        raw_value = value.get_secret_value()
    elif isinstance(value, str):
        raw_value = value
    else:
        raise ValueError("TOTP code must be a string")
    if _ASCII_TOTP.fullmatch(raw_value) is None:
        raise ValueError("TOTP code must contain exactly six ASCII digits")
    return value


def validate_pkce_value(value: object) -> str | SecretStr:
    if isinstance(value, SecretStr):
        raw_value = value.get_secret_value()
    elif isinstance(value, str):
        raw_value = value
    else:
        raise ValueError("PKCE value must be a string")
    if _PKCE_VALUE.fullmatch(raw_value) is None:
        raise ValueError("PKCE value must be 43-128 unreserved ASCII characters")
    return value


def validate_oauth_state(value: str) -> str:
    if _OPAQUE_STATE.fullmatch(value) is None:
        raise ValueError("state must be 16-256 unreserved ASCII characters")
    return value


def validate_base64url_256(value: str) -> str:
    if _BASE64URL_256.fullmatch(value) is None:
        raise ValueError("value must be unpadded base64url for 32 bytes")
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("value must be unpadded base64url for 32 bytes") from exc
    if len(decoded) != 32:
        raise ValueError("value must be unpadded base64url for 32 bytes")
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value:
        raise ValueError("value must use canonical unpadded base64url encoding")
    return value


def validate_scopes(value: list[OAuthScope]) -> list[OAuthScope]:
    if not value:
        raise ValueError("at least one scope is required")
    if len(value) != len(set(value)):
        raise ValueError("scopes must be unique")
    return value


def validate_redirect_uri(value: str) -> str:
    if value == "termflow://auth/callback":
        return value
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("redirect URI contains unsupported characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("redirect URI is malformed") from exc
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("redirect URI must not contain credentials, a query, or a fragment")
    if parsed.scheme == "https" and parsed.hostname:
        return value
    if parsed.scheme != "http" or not parsed.hostname or port is None:
        raise ValueError("redirect URI must use the approved native callback forms")
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError as exc:
        raise ValueError("HTTP redirect URI must use a loopback IP literal") from exc
    if not is_loopback or not 49_152 <= port <= 65_535:
        raise ValueError("HTTP redirect URI must use an explicit ephemeral loopback port")
    return value


def validate_callback_uri(value: str) -> str:
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) != {"state", "transaction_id"} or any(
        len(values) != 1 for values in query.values()
    ):
        raise ValueError("callback URI must contain only state and transaction_id")
    state = query["state"][0]
    validate_oauth_state(state)
    try:
        UUID(query["transaction_id"][0])
    except ValueError as exc:
        raise ValueError("callback transaction_id is malformed") from exc
    base_uri = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))
    validate_redirect_uri(base_uri)
    return value


class PaneInputRequest(HttpModel):
    text: str
    submit: bool

    @field_validator("text")
    @classmethod
    def plain_text_only(cls, value: str) -> str:
        return validate_plain_text(value)

    @model_validator(mode="after")
    def has_effect(self) -> "PaneInputRequest":
        if not self.text and not self.submit:
            raise ValueError("text must be non-empty unless submit is true")
        return self


class ErrorDetail(HttpModel):
    code: str
    message: str
    request_id: UUID


class ErrorEnvelope(HttpModel):
    error: ErrorDetail


class EnrollmentCreateRequest(HttpModel):
    display_name: str | None = None

    @field_validator("display_name")
    @classmethod
    def safe_display_name(cls, value: str | None) -> str | None:
        return validate_editable_name(value) if value is not None else None


class EnrollmentCreateResponse(HttpModel):
    token: str = Field(repr=False, min_length=32)
    expires_at: datetime


class EnrollmentMetadataResponse(HttpModel):
    token: str = Field(repr=False, min_length=32)
    expires_at: datetime
    login_command: str = Field(repr=False, min_length=1)


class InstallationEnrollRequest(HttpModel):
    enrollment_token: SecretStr
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    platform: str | None = Field(default=None, min_length=1, max_length=128)
    client_version: str | None = Field(default=None, min_length=1, max_length=64)


class InstallationEnrollResponse(HttpModel):
    installation_id: UUID
    installation_token: str = Field(repr=False, min_length=32)


class InstanceRegisterRequest(HttpModel):
    instance_id: UUID
    name: str = Field(min_length=1, max_length=128)


class InstanceRegisterResponse(HttpModel):
    instance_id: UUID
    instance_token: str = Field(repr=False, min_length=32)


class InstanceResponse(HttpModel):
    instance_id: UUID
    name: str
    installation_id: UUID
    created_at: datetime
    online: bool


class InstanceListResponse(HttpModel):
    instances: list[InstanceResponse]


class TopologyResponse(HttpModel):
    instance_id: UUID
    topology: TopologySnapshot


class CommandResponse(HttpModel):
    command_id: UUID
    idempotency_key: UUID
    ok: bool


class HealthResponse(HttpModel):
    status: str = "ok"


class BrowserSessionCreateRequest(HttpModel):
    admin_token: SecretStr


class BrowserSessionResponse(HttpModel):
    authenticated: bool = True
    expires_at: datetime


class BrowserSessionDeleteResponse(HttpModel):
    ok: bool = True


class BrowserSessionChallengeResponse(HttpModel):
    status: Literal["totp_required"] = "totp_required"
    challenge_id: UUID
    expires_at: datetime


class BrowserSessionTotpRequest(HttpModel):
    code: SecretStr

    @field_validator("code", mode="before")
    @classmethod
    def valid_totp_code(cls, value: object) -> str | SecretStr:
        return validate_totp_code(value)


class TotpStatusResponse(HttpModel):
    enabled: bool
    available: bool


class TotpSetupRequest(HttpModel):
    admin_token: SecretStr
    totp_code: SecretStr | None = None

    @field_validator("totp_code", mode="before")
    @classmethod
    def valid_totp_code(cls, value: object) -> str | SecretStr | None:
        return validate_totp_code(value) if value is not None else None


class TotpSetupResponse(HttpModel):
    setup_id: UUID
    provisioning_uri: str = Field(repr=False, min_length=1, max_length=2048)
    setup_key: str = Field(repr=False, min_length=16, max_length=128)
    expires_at: datetime

    @field_validator("provisioning_uri")
    @classmethod
    def valid_provisioning_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        expected_parameters = {"secret", "issuer", "algorithm", "digits", "period"}
        if (
            parsed.scheme != "otpauth"
            or parsed.netloc != "totp"
            or not parsed.path
            or parsed.fragment
            or set(query) != expected_parameters
            or any(len(values) != 1 for values in query.values())
            or not query["secret"][0]
            or not query["issuer"][0]
            or query["algorithm"][0] != "SHA1"
            or query["digits"][0] != "6"
            or query["period"][0] != "30"
        ):
            raise ValueError("provisioning URI must use the fixed TermFlow V1 TOTP parameters")
        return value

    @model_validator(mode="after")
    def provisioning_secret_matches_setup_key(self) -> "TotpSetupResponse":
        secret = parse_qs(urlsplit(self.provisioning_uri).query)["secret"][0]
        if secret != self.setup_key:
            raise ValueError("provisioning URI secret must match setup_key")
        return self


class TotpConfirmRequest(HttpModel):
    code: SecretStr

    @field_validator("code", mode="before")
    @classmethod
    def valid_totp_code(cls, value: object) -> str | SecretStr:
        return validate_totp_code(value)


class TotpDisableRequest(HttpModel):
    admin_token: SecretStr
    code: SecretStr

    @field_validator("code", mode="before")
    @classmethod
    def valid_totp_code(cls, value: object) -> str | SecretStr:
        return validate_totp_code(value)


class OAuthPublicJwk(HttpModel):
    kty: Literal["EC"] = "EC"
    crv: Literal["P-256"] = "P-256"
    alg: Literal["ES256"] = "ES256"
    x: str
    y: str

    @field_validator("x", "y")
    @classmethod
    def valid_coordinate(cls, value: str) -> str:
        return validate_base64url_256(value)


class OAuthMetadataResponse(HttpModel):
    issuer: str = Field(min_length=1, max_length=2048)
    authorization_endpoint: str = Field(min_length=1, max_length=2048)
    token_endpoint: str = Field(min_length=1, max_length=2048)
    revocation_endpoint: str = Field(min_length=1, max_length=2048)
    response_types_supported: list[Literal["code"]]
    grant_types_supported: list[Literal["authorization_code", "refresh_token"]]
    code_challenge_methods_supported: list[Literal["S256"]]
    dpop_signing_alg_values_supported: list[Literal["ES256"]]
    scopes_supported: list[OAuthScope]

    @field_validator("scopes_supported")
    @classmethod
    def nonempty_unique_scopes(cls, value: list[OAuthScope]) -> list[OAuthScope]:
        return validate_scopes(value)


class OAuthAuthorizationRequest(HttpModel):
    response_type: Literal["code"] = "code"
    client_name: str
    platform: str = Field(min_length=1, max_length=128)
    client_version: str | None = Field(default=None, min_length=1, max_length=64)
    redirect_uri: str
    state: str
    code_challenge: str
    code_challenge_method: Literal["S256"] = "S256"
    dpop_jkt: str
    public_jwk: OAuthPublicJwk
    scopes: list[OAuthScope]

    @field_validator("client_name")
    @classmethod
    def valid_client_name(cls, value: str) -> str:
        return validate_client_name(value)

    @field_validator("redirect_uri")
    @classmethod
    def safe_redirect_uri(cls, value: str) -> str:
        return validate_redirect_uri(value)

    @field_validator("state")
    @classmethod
    def valid_state(cls, value: str) -> str:
        return validate_oauth_state(value)

    @field_validator("code_challenge")
    @classmethod
    def valid_code_challenge(cls, value: str) -> str:
        validated = validate_pkce_value(value)
        assert isinstance(validated, str)
        return validated

    @field_validator("dpop_jkt")
    @classmethod
    def valid_dpop_thumbprint(cls, value: str) -> str:
        return validate_base64url_256(value)

    @field_validator("scopes")
    @classmethod
    def nonempty_unique_scopes(cls, value: list[OAuthScope]) -> list[OAuthScope]:
        return validate_scopes(value)


class OAuthAuthorizationPreviewResponse(HttpModel):
    transaction_id: UUID
    issuer: str = Field(min_length=1, max_length=2048)
    client_name: str
    platform: str
    client_version: str | None = None
    key_fingerprint: str
    scopes: list[OAuthScope]
    redirect_uri: str
    totp_required: bool
    expires_at: datetime

    @field_validator("client_name")
    @classmethod
    def valid_client_name(cls, value: str) -> str:
        return validate_client_name(value)

    @field_validator("key_fingerprint")
    @classmethod
    def valid_key_fingerprint(cls, value: str) -> str:
        return validate_base64url_256(value)

    @field_validator("scopes")
    @classmethod
    def nonempty_unique_scopes(cls, value: list[OAuthScope]) -> list[OAuthScope]:
        return validate_scopes(value)

    @field_validator("redirect_uri")
    @classmethod
    def safe_redirect_uri(cls, value: str) -> str:
        return validate_redirect_uri(value)


class OAuthAuthorizationDecisionRequest(HttpModel):
    transaction_id: UUID
    decision: Literal["allow", "deny"]
    admin_token: SecretStr
    totp_code: SecretStr | None = None

    @field_validator("totp_code", mode="before")
    @classmethod
    def valid_totp_code(cls, value: object) -> str | SecretStr | None:
        return validate_totp_code(value) if value is not None else None


class OAuthAuthorizationDecisionResponse(HttpModel):
    status: Literal["approved", "denied"]
    callback_uri: str

    @field_validator("callback_uri")
    @classmethod
    def safe_callback_uri(cls, value: str) -> str:
        return validate_callback_uri(value)


class OAuthTokenRequest(HttpModel):
    grant_type: Literal["authorization_code", "refresh_token"]
    transaction_id: UUID | None = None
    code_verifier: SecretStr | None = None
    refresh_token: SecretStr | None = None
    public_jwk: OAuthPublicJwk

    @field_validator("code_verifier", mode="before")
    @classmethod
    def valid_code_verifier(cls, value: object) -> str | SecretStr | None:
        return validate_pkce_value(value) if value is not None else None

    @model_validator(mode="after")
    def fields_match_grant(self) -> "OAuthTokenRequest":
        if self.grant_type == "authorization_code":
            if self.transaction_id is None or self.code_verifier is None:
                raise ValueError("authorization_code requires transaction_id and code_verifier")
            if self.refresh_token is not None:
                raise ValueError("authorization_code must not include refresh_token")
        elif (
            self.refresh_token is None
            or self.transaction_id is not None
            or self.code_verifier is not None
        ):
            raise ValueError("refresh_token requires only refresh_token credentials")
        return self


class OAuthTokenResponse(HttpModel):
    token_type: Literal["DPoP"] = "DPoP"
    access_token: str = Field(repr=False, min_length=32)
    expires_in: int = Field(gt=0)
    refresh_token: str = Field(repr=False, min_length=32)
    scopes: list[OAuthScope]

    @field_validator("scopes")
    @classmethod
    def nonempty_unique_scopes(cls, value: list[OAuthScope]) -> list[OAuthScope]:
        return validate_scopes(value)


class OAuthRevokeRequest(HttpModel):
    token: SecretStr
    token_type_hint: Literal["access_token", "refresh_token"] | None = None


class OAuthRevokeResponse(HttpModel):
    ok: bool = True


class CliTokenRequest(HttpModel):
    admin_token: SecretStr
    totp_code: SecretStr | None = None
    scopes: list[OAuthScope] = Field(default_factory=lambda: list(OAUTH_SCOPES))

    @field_validator("totp_code", mode="before")
    @classmethod
    def valid_totp_code(cls, value: object) -> str | SecretStr | None:
        return validate_totp_code(value) if value is not None else None

    @field_validator("scopes")
    @classmethod
    def nonempty_unique_scopes(cls, value: list[OAuthScope]) -> list[OAuthScope]:
        return validate_scopes(value)


class CliTokenResponse(HttpModel):
    token_type: Literal["Bearer"] = "Bearer"
    access_token: str = Field(repr=False, min_length=32)
    expires_in: int = Field(gt=0)
    scopes: list[OAuthScope]

    @field_validator("scopes")
    @classmethod
    def nonempty_unique_scopes(cls, value: list[OAuthScope]) -> list[OAuthScope]:
        return validate_scopes(value)


class NativeClientResponse(HttpModel):
    client_id: UUID
    display_name: str
    platform: str
    client_version: str | None = None
    key_thumbprint: str
    scopes: list[OAuthScope]
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("display_name")
    @classmethod
    def valid_display_name(cls, value: str) -> str:
        return validate_client_name(value)

    @field_validator("key_thumbprint")
    @classmethod
    def valid_key_thumbprint(cls, value: str) -> str:
        return validate_base64url_256(value)

    @field_validator("scopes")
    @classmethod
    def nonempty_unique_scopes(cls, value: list[OAuthScope]) -> list[OAuthScope]:
        return validate_scopes(value)


class NativeClientListResponse(HttpModel):
    clients: list[NativeClientResponse]


class NativeClientUpdateRequest(HttpModel):
    display_name: str

    @field_validator("display_name")
    @classmethod
    def valid_display_name(cls, value: str) -> str:
        return validate_client_name(value)


class NativeClientDeleteResponse(HttpModel):
    ok: bool = True


class ComputerRenameRequest(HttpModel):
    display_name: str

    @field_validator("display_name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return validate_editable_name(value)


class TermRenameRequest(HttpModel):
    name: str

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return validate_editable_name(value)


class TermSummary(HttpModel):
    instance_id: UUID
    name: str
    online: bool
    window_count: int = Field(ge=0)
    pane_count: int = Field(ge=0)
    active_pane_count: int = Field(ge=0)
    current_command: str | None = None
    last_seen_at: datetime | None = None


class ComputerSummary(HttpModel):
    installation_id: UUID
    hostname: str | None = None
    display_name: str
    platform: str | None = None
    client_version: str | None = None
    registered_at: datetime | None = None
    last_seen_at: datetime | None = None
    online: bool
    terms: list[TermSummary]


class ComputerListResponse(HttpModel):
    computers: list[ComputerSummary]


class DashboardMetrics(HttpModel):
    online_terms: int = Field(ge=0)
    total_terms: int = Field(ge=0)
    active_panes: int = Field(ge=0)
    interactions_24h: int = Field(ge=0)
    computers: int = Field(ge=0)


class DashboardResponse(HttpModel):
    metrics: DashboardMetrics
    computers: list[ComputerSummary]


class TerminalReadyFrame(HttpModel):
    type: Literal["terminal.ready"] = "terminal.ready"
    terminal_id: UUID
    stream_id: UUID
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)


class TerminalSizeFrame(HttpModel):
    type: Literal["terminal.size"] = "terminal.size"
    terminal_id: UUID
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)


class TerminalBindingSnapshotFrame(HttpModel):
    type: Literal["terminal.binding_snapshot"] = "terminal.binding_snapshot"
    terminal_id: UUID
    prefix: str
    prefix2: str | None = None
    bindings: list[TerminalBinding]


class TerminalErrorFrame(HttpModel):
    type: Literal["terminal.error"] = "terminal.error"
    terminal_id: UUID
    code: str
    message: str


class TerminalClosedFrame(HttpModel):
    type: Literal["terminal.closed"] = "terminal.closed"
    terminal_id: UUID
    reason: TerminalCloseReason


class TerminalActionResultFrame(HttpModel):
    type: Literal["terminal.action_result"] = "terminal.action_result"
    terminal_id: UUID
    action_id: UUID
    ok: bool
    error_code: str | None = None


class TerminalActionFrame(HttpModel):
    type: Literal["terminal.action"] = "terminal.action"
    action_id: UUID
    action: TerminalAction
    target_pane_id: str | None = None
    confirmed: bool = False


class TerminalCloseFrame(HttpModel):
    type: Literal["terminal.close"] = "terminal.close"
    reason: Literal["client_closed"] = "client_closed"
