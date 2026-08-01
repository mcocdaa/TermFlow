from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import (
    BrowserSessionCreateRequest,
    BrowserSessionDeleteResponse,
    BrowserSessionResponse,
    ComputerListResponse,
    ComputerRenameRequest,
    DashboardMetrics,
    DashboardResponse,
    EnrollmentCreateRequest,
    EnrollmentMetadataResponse,
    InstallationEnrollResponse,
    PaneInputRequest,
    TerminalBindingSnapshotFrame,
    TerminalClosedFrame,
    TerminalErrorFrame,
    TerminalReadyFrame,
    TerminalSizeFrame,
    TermRenameRequest,
)


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
        TerminalReadyFrame(
            terminal_id=uuid4(), stream_id=uuid4(), rows=24, cols=80
        ),
        TerminalSizeFrame(terminal_id=uuid4(), rows=30, cols=100),
        TerminalBindingSnapshotFrame(
            terminal_id=uuid4(), prefix="C-b", prefix2=None, bindings=[]
        ),
        TerminalErrorFrame(
            terminal_id=uuid4(), code="offline", message="Term is offline"
        ),
        TerminalClosedFrame(terminal_id=uuid4(), reason="client_closed"),
    ],
)
def test_browser_terminal_control_frames_have_no_byte_body(frame: object) -> None:
    dumped = frame.model_dump(mode="json")  # type: ignore[attr-defined]
    UUID(dumped["terminal_id"])
    assert "data" not in dumped
    assert "data_base64" not in dumped
