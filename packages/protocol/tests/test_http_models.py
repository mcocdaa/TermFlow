from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import InstallationEnrollResponse, PaneInputRequest


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
