import base64
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError
from termflow_protocol import (
    BrowserSessionChallengeResponse,
    BrowserSessionCreateRequest,
    BrowserSessionDeleteResponse,
    BrowserSessionResponse,
    BrowserSessionTotpRequest,
    CliTokenRequest,
    CliTokenResponse,
    ComputerListResponse,
    ComputerRenameRequest,
    DashboardMetrics,
    DashboardResponse,
    EnrollmentCreateRequest,
    EnrollmentCreateResponse,
    EnrollmentMetadataResponse,
    InstallationEnrollResponse,
    NativeClientDeleteResponse,
    NativeClientListResponse,
    NativeClientResponse,
    NativeClientUpdateRequest,
    OAuthAuthorizationDecisionRequest,
    OAuthAuthorizationDecisionResponse,
    OAuthAuthorizationPreviewResponse,
    OAuthAuthorizationRequest,
    OAuthMetadataResponse,
    OAuthPublicJwk,
    OAuthRevokeRequest,
    OAuthRevokeResponse,
    OAuthTokenRequest,
    OAuthTokenResponse,
    PaneInputRequest,
    TerminalBindingSnapshotFrame,
    TerminalClosedFrame,
    TerminalErrorFrame,
    TerminalReadyFrame,
    TerminalSizeFrame,
    TermRenameRequest,
    TotpConfirmRequest,
    TotpDisableRequest,
    TotpProtectionRequest,
    TotpSetupRequest,
    TotpSetupResponse,
    TotpStatusResponse,
)


def base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


VALID_JWK = {
    "kty": "EC",
    "crv": "P-256",
    "alg": "ES256",
    "x": base64url(bytes(32)),
    "y": base64url(bytes([1]) * 32),
}
VALID_SCOPES = ["terminal.read", "terminal.write"]
VALID_SETUP_KEY = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
VALID_THUMBPRINT = base64url(bytes([2]) * 32)


def valid_authorization_request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "response_type": "code",
        "client_name": "TermFlow Desktop",
        "platform": "linux",
        "client_version": "0.2.0",
        "redirect_uri": "termflow://auth/callback",
        "state": "state-" + "s" * 38,
        "code_challenge": "c" * 43,
        "code_challenge_method": "S256",
        "dpop_jkt": VALID_THUMBPRINT,
        "public_jwk": VALID_JWK,
        "scopes": VALID_SCOPES,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("value", ["hello\x03", "\x1b[31m", "nul\x00", "delete\x7f"])
def test_plain_input_rejects_control_characters(value: str) -> None:
    with pytest.raises(ValidationError):
        PaneInputRequest(text=value, submit=False)


def test_plain_input_requires_text_or_submit() -> None:
    with pytest.raises(ValidationError):
        PaneInputRequest(text="", submit=False)


def test_plain_input_allows_empty_text_when_submitting_enter() -> None:
    request = PaneInputRequest(text="", submit=True)
    assert request.submit is True


def test_plain_input_limit_is_measured_in_utf8_bytes() -> None:
    with pytest.raises(ValidationError):
        PaneInputRequest(text="界" * 6000, submit=False)


def test_plain_unicode_input_is_accepted() -> None:
    request = PaneInputRequest(text="继续处理这个任务", submit=True)
    assert request.text == "继续处理这个任务"


def test_credential_response_serializes_real_token_without_repr_leak() -> None:
    raw_token = "raw-" + "x" * 40
    response = InstallationEnrollResponse(
        installation_id=uuid4(),
        installation_token=raw_token,
    )
    assert response.model_dump(mode="json")["installation_token"] == raw_token
    assert raw_token not in repr(response)


def test_browser_session_dtos_hide_admin_token_and_serialize_status() -> None:
    raw_token = "admin-" + "s" * 40
    request = BrowserSessionCreateRequest(admin_token=raw_token)
    expires_at = datetime.now(UTC)
    response = BrowserSessionResponse(authenticated=True, expires_at=expires_at)

    assert raw_token not in repr(request)
    assert response.model_dump(mode="json") == {
        "authenticated": True,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
    assert BrowserSessionDeleteResponse().ok is True


def test_browser_session_totp_challenge_and_status_are_strict() -> None:
    challenge_id = uuid4()
    expires_at = datetime.now(UTC)
    challenge = BrowserSessionChallengeResponse(
        status="totp_required",
        challenge_id=challenge_id,
        expires_at=expires_at,
    )

    assert challenge.model_dump(mode="json") == {
        "status": "totp_required",
        "challenge_id": str(challenge_id),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
    assert TotpStatusResponse(configured=False, enabled=False, available=True).available is True
    with pytest.raises(ValidationError):
        TotpStatusResponse(configured=False, enabled=True, available=True)
    with pytest.raises(ValidationError):
        BrowserSessionChallengeResponse(
            status="totp_required",
            challenge_id=challenge_id,
            expires_at=expires_at,
            admin_token="must-not-be-accepted",
        )


@pytest.mark.parametrize(
    "model, payload",
    [
        (BrowserSessionTotpRequest, {}),
        (TotpConfirmRequest, {}),
        (
            TotpDisableRequest,
            {"admin_token": SecretStr("admin-" + "a" * 32)},
        ),
        (
            TotpProtectionRequest,
            {"admin_token": SecretStr("admin-" + "a" * 32)},
        ),
    ],
)
@pytest.mark.parametrize(
    "code",
    ["12345", "1234567", "12345x", " 123456", "123456 ", "１２３４５６", "١٢٣٤٥٦"],
)
def test_totp_codes_are_exactly_six_ascii_digits(
    model: type[object],
    payload: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(ValidationError):
        model(code=code, **payload)  # type: ignore[operator]


@pytest.mark.parametrize("value", [123456, None, [], {}])
def test_totp_json_types_fail_as_validation_errors(value: object) -> None:
    with pytest.raises(ValidationError):
        BrowserSessionTotpRequest.model_validate_json(json.dumps({"code": value}))


@pytest.mark.parametrize("value", [123456, None, [], {}])
def test_pkce_json_types_fail_as_validation_errors(value: object) -> None:
    payload = {
        "grant_type": "authorization_code",
        "transaction_id": str(uuid4()),
        "code_verifier": value,
        "public_jwk": VALID_JWK,
    }
    with pytest.raises(ValidationError):
        OAuthTokenRequest.model_validate_json(json.dumps(payload))


def test_submitted_totp_and_admin_credentials_do_not_leak_from_repr() -> None:
    admin_token = "admin-" + "a" * 40
    requests = [
        BrowserSessionTotpRequest(code="123456"),
        TotpSetupRequest(admin_token=admin_token, totp_code="123456"),
        TotpConfirmRequest(code="123456"),
        TotpDisableRequest(admin_token=admin_token, code="123456"),
        TotpProtectionRequest(admin_token=admin_token, code="123456"),
        CliTokenRequest(admin_token=admin_token, totp_code="123456"),
    ]

    for request in requests:
        rendered = repr(request)
        assert admin_token not in rendered
        assert "123456" not in rendered


def test_totp_setup_response_exposes_once_only_material_without_repr_leak() -> None:
    setup_key = VALID_SETUP_KEY
    response = TotpSetupResponse(
        setup_id=uuid4(),
        provisioning_uri=(
            f"otpauth://totp/TermFlow%3Aadmin?secret={setup_key}"
            "&issuer=TermFlow&algorithm=SHA1&digits=6&period=30"
        ),
        setup_key=setup_key,
        expires_at=datetime.now(UTC),
    )

    assert response.model_dump()["setup_key"] == setup_key
    assert setup_key not in repr(response)


def test_enrollment_response_carries_env_authoritative_server_command() -> None:
    raw_token = "join-" + "x" * 40
    response = EnrollmentCreateResponse(
        token=raw_token,
        expires_at=datetime.now(UTC),
        server_url="https://relay.example.com",
        login_command=(
            "termflow login --server https://relay.example.com " f"--code {raw_token}"
        ),
    )

    assert response.server_url == "https://relay.example.com"
    assert response.login_command.startswith("termflow login --server")
    assert response.login_command not in repr(response)


def test_totp_status_separates_configuration_from_enforcement() -> None:
    status = TotpStatusResponse(configured=True, enabled=False, available=True)

    assert status.model_dump() == {
        "configured": True,
        "enabled": False,
        "available": True,
    }


@pytest.mark.parametrize(
    "query",
    [
        f"secret={VALID_SETUP_KEY}&secret=OTHER&issuer=TermFlow&algorithm=SHA1&digits=6&period=30",
        f"secret={VALID_SETUP_KEY}&algorithm=SHA1&digits=6&period=30",
        f"secret={VALID_SETUP_KEY}&issuer=TermFlow&algorithm=SHA256&digits=6&period=30",
        f"secret={VALID_SETUP_KEY}&issuer=TermFlow&algorithm=SHA1&digits=8&period=30",
        f"secret={VALID_SETUP_KEY}&issuer=TermFlow&algorithm=SHA1&digits=6&period=60",
        f"secret={VALID_SETUP_KEY}&issuer=TermFlow&algorithm=SHA1&digits=6&period=30&extra=value",
    ],
)
def test_totp_provisioning_uri_requires_unique_fixed_v1_parameters(query: str) -> None:
    with pytest.raises(ValidationError):
        TotpSetupResponse(
            setup_id=uuid4(),
            provisioning_uri=f"otpauth://totp/TermFlow%3Aadmin?{query}",
            setup_key=VALID_SETUP_KEY,
            expires_at=datetime.now(UTC),
        )


def test_totp_provisioning_uri_secret_matches_displayed_setup_key() -> None:
    with pytest.raises(ValidationError):
        TotpSetupResponse(
            setup_id=uuid4(),
            provisioning_uri=(
                "otpauth://totp/TermFlow%3Aadmin?secret=FIRST&issuer=TermFlow"
                "&algorithm=SHA1&digits=6&period=30"
            ),
            setup_key="SECOND-SETUP-KEY",
            expires_at=datetime.now(UTC),
        )


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "termflow://auth/callback",
        "https://mobile.example/oauth/callback",
        "http://127.0.0.1:49152/oauth/callback",
        "http://[::1]:65535/oauth/callback",
    ],
)
def test_native_authorization_accepts_safe_redirect_uris(redirect_uri: str) -> None:
    request = OAuthAuthorizationRequest.model_validate(
        valid_authorization_request(redirect_uri=redirect_uri)
    )
    assert request.redirect_uri == redirect_uri


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "termflow://auth/other",
        "termflow://other/callback",
        "termflow://auth/callback?code=leak",
        "https://mobile.example/oauth/callback?state=not-registered-here",
        "http://127.0.0.1:49152/oauth/callback?state=not-registered-here",
        "http://example.com:49152/callback",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:8080/callback",
        "https://user:pass@example.com/callback",
        "https://example.com/callback#fragment",
        "ftp://example.com/callback",
    ],
)
def test_native_authorization_rejects_unsafe_redirect_uris(redirect_uri: str) -> None:
    with pytest.raises(ValidationError):
        OAuthAuthorizationRequest.model_validate(
            valid_authorization_request(redirect_uri=redirect_uri)
        )


@pytest.mark.parametrize(
    "field, value",
    [
        ("code_challenge", "a" * 42),
        ("code_challenge", "a" * 129),
        ("code_challenge", "a" * 42 + "%"),
        ("code_challenge_method", "plain"),
    ],
)
def test_native_authorization_requires_pkce_s256(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        OAuthAuthorizationRequest.model_validate(valid_authorization_request(**{field: value}))


@pytest.mark.parametrize("state", ["!" + "s" * 43, "s" * 43 + "!", "s" * 43 + "\n"])
def test_native_authorization_state_requires_a_full_ascii_match(state: str) -> None:
    with pytest.raises(ValidationError):
        OAuthAuthorizationRequest.model_validate(valid_authorization_request(state=state))


@pytest.mark.parametrize(
    "jwk",
    [
        {**VALID_JWK, "d": "private-key-material"},
        {**VALID_JWK, "kty": "RSA"},
        {**VALID_JWK, "crv": "P-384"},
        {**VALID_JWK, "alg": "ES384"},
        {**VALID_JWK, "x": "A" * 42},
        {**VALID_JWK, "y": "not/base64url"},
        {**VALID_JWK, "x": VALID_JWK["x"][:-1] + "B"},
    ],
)
def test_native_authorization_accepts_only_public_es256_p256_jwk(
    jwk: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        OAuthAuthorizationRequest.model_validate(valid_authorization_request(public_jwk=jwk))


@pytest.mark.parametrize(
    "scopes",
    [[], ["admin"], ["terminal.read", "terminal.read"]],
)
def test_native_authorization_requires_nonempty_minimal_unique_scopes(
    scopes: list[str],
) -> None:
    with pytest.raises(ValidationError):
        OAuthAuthorizationRequest.model_validate(valid_authorization_request(scopes=scopes))


@pytest.mark.parametrize("client_name", ["", "   ", " bad", "bad ", "bad\x00name", "x" * 129])
def test_native_authorization_rejects_invalid_client_names(client_name: str) -> None:
    with pytest.raises(ValidationError):
        OAuthAuthorizationRequest.model_validate(
            valid_authorization_request(client_name=client_name)
        )


def test_oauth_metadata_and_browser_preview_are_public_contracts() -> None:
    now = datetime.now(UTC)
    transaction_id = uuid4()
    metadata = OAuthMetadataResponse(
        issuer="https://termflow.example",
        authorization_endpoint="https://termflow.example/api/v1/oauth/authorize",
        token_endpoint="https://termflow.example/api/v1/oauth/token",
        revocation_endpoint="https://termflow.example/api/v1/oauth/revoke",
        device_authorization_endpoint="https://termflow.example/api/v1/oauth/device/code",
        device_verification_uri="https://termflow.example/device",
        response_types_supported=["code"],
        grant_types_supported=["authorization_code", "refresh_token"],
        code_challenge_methods_supported=["S256"],
        dpop_signing_alg_values_supported=["ES256"],
        scopes_supported=[
            "terminal.read",
            "terminal.write",
            "computers.read",
            "computers.write",
        ],
    )
    preview = OAuthAuthorizationPreviewResponse(
        transaction_id=transaction_id,
        issuer=metadata.issuer,
        client_name="TermFlow Desktop",
        platform="linux",
        client_version="0.2.0",
        key_fingerprint=VALID_THUMBPRINT,
        scopes=VALID_SCOPES,
        redirect_uri="termflow://auth/callback",
        totp_required=True,
        expires_at=now,
    )

    assert metadata.code_challenge_methods_supported == ["S256"]
    assert preview.transaction_id == transaction_id
    assert "admin_token" not in preview.model_dump()


def test_authorization_decision_credentials_are_secret_and_response_is_public() -> None:
    admin_token = "admin-" + "a" * 40
    request = OAuthAuthorizationDecisionRequest(
        transaction_id=uuid4(),
        decision="allow",
        admin_token=admin_token,
        totp_code="123456",
    )
    response = OAuthAuthorizationDecisionResponse(
        status="approved",
        callback_uri=(
            "termflow://auth/callback?state=state-ssssssssssssssssssssssssssssssssssssss"
            f"&transaction_id={uuid4()}"
        ),
    )

    assert admin_token not in repr(request)
    assert "123456" not in repr(request)
    assert response.status == "approved"


@pytest.mark.parametrize(
    "callback_uri",
    [
        "termflow://auth/callback",
        "termflow://auth/callback?code=secret&state=valid-state-value",
        "termflow://auth/callback?access_token=secret&state=valid-state-value",
        "termflow://auth/callback?state=valid-state-value",
    ],
)
def test_authorization_callback_contains_only_state_and_public_transaction(
    callback_uri: str,
) -> None:
    with pytest.raises(ValidationError):
        OAuthAuthorizationDecisionResponse(status="approved", callback_uri=callback_uri)


def test_authorization_code_token_exchange_requires_pkce_and_public_key() -> None:
    verifier = "v" * 43
    request = OAuthTokenRequest(
        grant_type="authorization_code",
        transaction_id=uuid4(),
        code_verifier=verifier,
        public_jwk=OAuthPublicJwk(**VALID_JWK),
    )

    assert verifier not in repr(request)
    with pytest.raises(ValidationError):
        OAuthTokenRequest(
            grant_type="authorization_code",
            transaction_id=uuid4(),
            public_jwk=OAuthPublicJwk(**VALID_JWK),
        )


def test_refresh_token_exchange_rejects_mixed_grant_credentials() -> None:
    refresh_token = "refresh-" + "r" * 32
    request = OAuthTokenRequest(
        grant_type="refresh_token",
        refresh_token=refresh_token,
        public_jwk=OAuthPublicJwk(**VALID_JWK),
    )

    assert refresh_token not in repr(request)
    with pytest.raises(ValidationError):
        OAuthTokenRequest(
            grant_type="refresh_token",
            refresh_token=refresh_token,
            transaction_id=uuid4(),
            code_verifier="v" * 43,
            public_jwk=OAuthPublicJwk(**VALID_JWK),
        )


def test_token_and_revoke_contracts_hide_returned_and_submitted_tokens() -> None:
    access_token = "access-" + "a" * 40
    refresh_token = "refresh-" + "r" * 40
    response = OAuthTokenResponse(
        token_type="DPoP",
        access_token=access_token,
        expires_in=600,
        refresh_token=refresh_token,
        scopes=VALID_SCOPES,
    )
    revoke = OAuthRevokeRequest(token=refresh_token, token_type_hint="refresh_token")

    assert response.model_dump()["access_token"] == access_token
    assert response.model_dump()["refresh_token"] == refresh_token
    assert access_token not in repr(response)
    assert refresh_token not in repr(response)
    assert refresh_token not in repr(revoke)
    assert OAuthRevokeResponse().ok is True


def test_cli_token_contract_has_scoped_defaults_and_hides_token_values() -> None:
    access_token = "cli-" + "c" * 40
    request = CliTokenRequest(admin_token="admin-" + "a" * 40, totp_code=None)
    response = CliTokenResponse(
        token_type="Bearer",
        access_token=access_token,
        expires_in=600,
        scopes=request.scopes,
    )

    assert request.scopes
    assert access_token not in repr(response)
    assert response.model_dump()["access_token"] == access_token


def test_native_client_list_update_and_delete_contracts_are_strict() -> None:
    now = datetime.now(UTC)
    client = NativeClientResponse(
        client_id=uuid4(),
        display_name="TermFlow Phone",
        platform="ios",
        client_version="0.2.0",
        key_thumbprint=VALID_THUMBPRINT,
        scopes=["terminal.read"],
        created_at=now,
        last_used_at=None,
        revoked_at=None,
    )
    clients = NativeClientListResponse(clients=[client])
    update = NativeClientUpdateRequest(display_name="Personal Phone")

    assert clients.clients[0] == client
    assert update.display_name == "Personal Phone"
    assert NativeClientDeleteResponse().ok is True
    with pytest.raises(ValidationError):
        NativeClientUpdateRequest(display_name="Phone", unknown=True)


@pytest.mark.parametrize(
    "model, field",
    [
        (ComputerRenameRequest, "display_name"),
        (TermRenameRequest, "name"),
    ],
)
@pytest.mark.parametrize("value", ["", "x" * 129, "bad\x00name", "bad\x85name"])
def test_editable_names_have_one_shared_safe_contract(
    model: type[object],
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        model(**{field: value})  # type: ignore[operator]


def test_enrollment_creation_accepts_an_optional_validated_display_name() -> None:
    assert EnrollmentCreateRequest().display_name is None
    assert EnrollmentCreateRequest(display_name="跑步工作站").display_name == "跑步工作站"


@pytest.mark.parametrize("value", ["", "x" * 129, "bad\x00name", "bad\x85name"])
def test_enrollment_creation_rejects_unsafe_display_names(value: str) -> None:
    with pytest.raises(ValidationError):
        EnrollmentCreateRequest(display_name=value)


def test_dashboard_and_computer_dtos_group_terms_without_terminal_content() -> None:
    installation_id = uuid4()
    instance_id = uuid4()
    now = datetime.now(UTC)
    payload = {
        "metrics": {
            "online_terms": 1,
            "total_terms": 1,
            "active_panes": 2,
            "interactions_24h": 3,
            "computers": 1,
        },
        "computers": [
            {
                "installation_id": installation_id,
                "hostname": "devbox",
                "display_name": "Desk",
                "platform": "Linux",
                "client_version": "0.2.0",
                "last_seen_at": now,
                "online": True,
                "terms": [
                    {
                        "instance_id": instance_id,
                        "name": "backend",
                        "online": True,
                        "window_count": 2,
                        "pane_count": 3,
                        "active_pane_count": 1,
                        "current_command": "python",
                        "last_seen_at": now,
                    }
                ],
            }
        ],
    }

    response = DashboardResponse.model_validate(payload)
    computers = ComputerListResponse(computers=response.computers)
    assert response.metrics == DashboardMetrics(
        online_terms=1,
        total_terms=1,
        active_panes=2,
        interactions_24h=3,
        computers=1,
    )
    assert computers.computers[0].terms[0].current_command == "python"
    assert "data" not in repr(response).lower()


def test_enrollment_metadata_exposes_once_only_command_without_repr_token_leak() -> None:
    token = "enroll-" + "x" * 40
    metadata = EnrollmentMetadataResponse(
        token=token,
        expires_at=datetime.now(UTC),
        login_command=f"termflow login https://termflow.example {token}",
    )
    assert metadata.model_dump()["token"] == token
    assert token not in repr(metadata)


@pytest.mark.parametrize(
    "frame",
    [
        TerminalReadyFrame(terminal_id=uuid4(), stream_id=uuid4(), rows=24, cols=80),
        TerminalSizeFrame(terminal_id=uuid4(), rows=30, cols=100),
        TerminalBindingSnapshotFrame(terminal_id=uuid4(), prefix="C-b", prefix2=None, bindings=[]),
        TerminalErrorFrame(terminal_id=uuid4(), code="offline", message="Term is offline"),
        TerminalClosedFrame(terminal_id=uuid4(), reason="client_closed"),
    ],
)
def test_browser_terminal_control_frames_have_no_byte_body(frame: object) -> None:
    dumped = frame.model_dump(mode="json")  # type: ignore[attr-defined]
    UUID(dumped["terminal_id"])
    assert "data" not in dumped
    assert "data_base64" not in dumped
