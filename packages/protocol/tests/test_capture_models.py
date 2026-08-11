"""Bounded pane capture wire protocol models (Task M2.1, part 1)."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import (
    CaptureKind,
    PaneCaptureErrorPayload,
    PaneCaptureRequestPayload,
    PaneCaptureResultPayload,
    WireMessage,
    parse_payload,
)


def make_request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "instance_id": uuid4(),
        "pane_id": "%1",
        "capture_kind": "history",
        "request_id": uuid4(),
        "max_bytes": 65_536,
        "join_wrapped": True,
        "pane_incarnation": 3,
    }
    payload.update(overrides)
    return payload


def make_result(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": uuid4(),
        "instance_id": uuid4(),
        "pane_id": "%1",
        "pane_incarnation": 3,
        "content": "first line\nsecond line",
        "encoding": "utf-8",
        "stream_id": "s-9f2c",
        "from_seq": 10,
        "to_seq": 20,
        "capture_kind": "history",
        "capture_range": {"start": 1, "end": 2},
        "viewport_geometry": {"rows": 24, "cols": 80},
        "truncated": False,
        "stream_gap": False,
        "gap_reason": None,
        "result_code": "ok",
    }
    payload.update(overrides)
    return payload


def make_error(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": uuid4(),
        "instance_id": uuid4(),
        "pane_id": "%1",
        "error_code": "pane_not_found",
        "message": "pane %1 disappeared before capture",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("capture_kind", ["viewport", "history", "snapshot", "gap_snapshot"])
def test_capture_request_round_trips_through_python_and_json_modes(capture_kind: str) -> None:
    payload = PaneCaptureRequestPayload.model_validate(
        make_request(capture_kind=capture_kind)
    )

    assert payload.model_dump() == PaneCaptureRequestPayload.model_validate(
        payload.model_dump()
    ).model_dump()
    assert payload.model_dump() == PaneCaptureRequestPayload.model_validate(
        payload.model_dump(mode="json")
    ).model_dump()


def test_capture_result_round_trips_through_python_and_json_modes() -> None:
    payload = PaneCaptureResultPayload.model_validate(make_result())

    assert payload.model_dump() == PaneCaptureResultPayload.model_validate(
        payload.model_dump()
    ).model_dump()
    assert payload.model_dump() == PaneCaptureResultPayload.model_validate(
        payload.model_dump(mode="json")
    ).model_dump()


def test_capture_error_round_trips_through_python_and_json_modes() -> None:
    payload = PaneCaptureErrorPayload.model_validate(make_error())

    assert payload.model_dump() == PaneCaptureErrorPayload.model_validate(
        payload.model_dump()
    ).model_dump()
    assert payload.model_dump() == PaneCaptureErrorPayload.model_validate(
        payload.model_dump(mode="json")
    ).model_dump()


@pytest.mark.parametrize("max_bytes", [0, -1])
def test_max_bytes_must_be_positive(max_bytes: int) -> None:
    with pytest.raises(ValidationError):
        PaneCaptureRequestPayload.model_validate(make_request(max_bytes=max_bytes))


@pytest.mark.parametrize("field", ["start_line", "end_line", "tail_lines"])
@pytest.mark.parametrize("value", [0, -1])
def test_line_bounds_must_be_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        PaneCaptureRequestPayload.model_validate(make_request(**{field: value}))


def test_request_id_is_echoed_in_result_and_error() -> None:
    request_id = uuid4()
    request = PaneCaptureRequestPayload.model_validate(
        make_request(request_id=request_id)
    )
    result = PaneCaptureResultPayload.model_validate(
        make_result(request_id=request_id)
    )
    error = PaneCaptureErrorPayload.model_validate(
        make_error(request_id=request_id)
    )

    assert request.request_id == request_id
    assert result.request_id == request_id
    assert error.request_id == request_id


@pytest.mark.parametrize(
    "payload_type",
    [PaneCaptureRequestPayload, PaneCaptureResultPayload],
)
def test_unknown_capture_kind_is_rejected(payload_type: type[object]) -> None:
    factory = make_request if payload_type is PaneCaptureRequestPayload else make_result
    with pytest.raises(ValidationError):
        payload_type.model_validate(factory(capture_kind="bogus"))  # type: ignore[operator]


@pytest.mark.parametrize(
    ("payload_type", "factory"),
    [
        (PaneCaptureRequestPayload, make_request),
        (PaneCaptureResultPayload, make_result),
        (PaneCaptureErrorPayload, make_error),
    ],
)
def test_extra_fields_are_rejected(
    payload_type: type[object], factory: object
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        payload_type.model_validate(factory(extra_field="nope"))  # type: ignore[operator]


@pytest.mark.parametrize(
    ("message_type", "payload_factory", "expected_type"),
    [
        ("pane.capture_request", make_request, PaneCaptureRequestPayload),
        ("pane.capture_result", make_result, PaneCaptureResultPayload),
        ("pane.capture_error", make_error, PaneCaptureErrorPayload),
    ],
)
def test_wire_message_dispatches_each_capture_type(
    message_type: str,
    payload_factory: object,
    expected_type: type[object],
) -> None:
    payload = payload_factory()  # type: ignore[operator]
    message = WireMessage(
        type=message_type,
        instance_id=payload["instance_id"],
        payload=payload,
    )
    parsed = parse_payload(message.type.value, message.payload)
    assert isinstance(parsed, expected_type)


def test_parse_payload_rejects_unknown_type_like_existing_behavior() -> None:
    with pytest.raises(ValueError, match="unsupported message type"):
        parse_payload("pane.capture", {})


def test_capture_kind_members_are_stable() -> None:
    assert [member.value for member in CaptureKind] == [
        "viewport",
        "history",
        "snapshot",
        "gap_snapshot",
    ]


def test_error_message_is_bounded_to_512_characters() -> None:
    PaneCaptureErrorPayload.model_validate(make_error(message="x" * 512))
    with pytest.raises(ValidationError):
        PaneCaptureErrorPayload.model_validate(make_error(message="x" * 513))


def test_result_requires_explicit_gap_reason() -> None:
    payload = make_result()
    del payload["gap_reason"]
    with pytest.raises(ValidationError, match="gap_reason"):
        PaneCaptureResultPayload.model_validate(payload)
