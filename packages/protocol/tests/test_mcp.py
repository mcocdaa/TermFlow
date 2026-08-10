from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import (
    PaneReadParams,
    PaneReadResult,
    PaneReadView,
    PaneSendTextResult,
    PaneWriteResult,
    StreamGapInfo,
    TermFlowErrorCode,
    TermFlowToolName,
    WatchCancelResult,
    WatchCondition,
    WatchConditionKind,
    WatchCreateParams,
    WatchGetParams,
    WatchListParams,
    WatchStatus,
)

_CURSOR: dict[str, object] = {
    "instance_id": uuid4(),
    "pane_id": "%1",
    "pane_incarnation": 0,
    "stream_id": uuid4(),
    "seq": 12,
}


def _pane_read_params(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "pane_id": "%1",
        "view": "viewport",
        "max_bytes": 4096,
        "join_wrapped": True,
    }
    data.update(overrides)
    return data


def test_pane_read_max_bytes_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        PaneReadParams.model_validate(_pane_read_params(max_bytes=0))
    with pytest.raises(ValidationError):
        PaneReadParams.model_validate(_pane_read_params(max_bytes=-1))


def test_pane_read_view_is_a_closed_enum() -> None:
    with pytest.raises(ValidationError):
        PaneReadParams.model_validate(_pane_read_params(view="scrollback"))
    parsed = PaneReadParams.model_validate(_pane_read_params(view="since", cursor=_CURSOR))
    assert parsed.view is PaneReadView.SINCE
    assert parsed.cursor is not None


@pytest.mark.parametrize("view", ["viewport", "history", "since"])
def test_pane_read_view_values_validate(view: str) -> None:
    kwargs: dict[str, object] = {}
    if view == "since":
        kwargs["cursor"] = _CURSOR
    parsed = PaneReadParams.model_validate(_pane_read_params(view=view, **kwargs))
    assert parsed.view.value == view


def test_since_view_requires_cursor() -> None:
    with pytest.raises(ValidationError, match="cursor"):
        PaneReadParams.model_validate(_pane_read_params(view="since"))


def test_cursor_only_valid_in_since_view() -> None:
    with pytest.raises(ValidationError, match="cursor"):
        PaneReadParams.model_validate(_pane_read_params(view="viewport", cursor=_CURSOR))


def test_start_line_and_end_line_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="start_line"):
        PaneReadParams.model_validate(
            _pane_read_params(view="history", start_line=20, end_line=10)
        )


def test_tail_lines_conflicts_with_line_range() -> None:
    with pytest.raises(ValidationError, match="tail_lines"):
        PaneReadParams.model_validate(
            _pane_read_params(view="history", start_line=1, tail_lines=10)
        )


def test_error_code_enum_values_are_stable() -> None:
    assert {member.value for member in TermFlowErrorCode} == {
        "pane_not_found",
        "incarnation_changed",
        "policy_denied",
        "stream_gap",
        "quota_exceeded",
        "invalid_request",
        "watch_not_found",
        "approval_required",
        "approval_denied",
        "outcome_unknown",
        "internal_error",
    }


def test_tool_name_enum_matches_contract() -> None:
    assert {member.value for member in TermFlowToolName} == {
        "termflow_list_panes",
        "termflow_pane_read",
        "termflow_pane_send_text",
        "termflow_pane_send_keys",
        "termflow_watch_create",
        "termflow_watch_list",
        "termflow_watch_get",
        "termflow_watch_cancel",
    }


def test_send_text_result_rejects_unknown_error_code() -> None:
    with pytest.raises(ValidationError):
        PaneSendTextResult(
            request_key="req-1",
            ok=False,
            outcome="failed",
            error_code="not_a_code",
        )


def test_send_text_result_rejects_conflicting_outcome() -> None:
    with pytest.raises(ValidationError, match="outcome"):
        PaneSendTextResult(
            request_key="req-1",
            ok=True,
            outcome="failed",
        )


def test_send_text_result_rejects_missing_error_code() -> None:
    with pytest.raises(ValidationError, match="error_code"):
        PaneSendTextResult(
            request_key="req-1",
            ok=False,
            outcome="failed",
        )


def test_pane_write_result_round_trips() -> None:
    result = PaneWriteResult(
        request_key="req-1",
        ok=True,
        outcome="confirmed",
    )
    dumped = result.model_dump()
    reparsed = PaneWriteResult.model_validate(dumped)
    assert reparsed == result


def test_pane_read_result_truncated_flag_round_trips() -> None:
    result = PaneReadResult(
        instance_id=uuid4(),
        pane_id="%1",
        view="history",
        content="bounded content",
        truncated=True,
    )
    dumped = result.model_dump()
    reparsed = PaneReadResult.model_validate(dumped)
    assert reparsed.truncated is True
    assert reparsed.pane_id == "%1"
    assert reparsed.instance_id == result.instance_id


def test_pane_read_result_stream_gap_is_explicit() -> None:
    result = PaneReadResult.model_validate(
        {
            "instance_id": uuid4(),
            "pane_id": "%1",
            "view": "since",
            "content": "",
            "truncated": False,
            "stream_gap": {
                "pane_id": "%1",
                "previous_stream_id": uuid4(),
                "reason": "backpressure",
                "recovered_cursor": _CURSOR,
            },
        }
    )
    assert result.stream_gap is not None
    assert isinstance(result.stream_gap, StreamGapInfo)
    assert result.stream_gap.reason == "backpressure"
    assert result.stream_gap.recovered_cursor is not None
    assert result.stream_gap.recovered_cursor.seq == 12


def test_watch_condition_kind_is_a_closed_set() -> None:
    with pytest.raises(ValidationError):
        WatchCondition(kind="regex_match", match="x")


def test_output_contains_condition_requires_match() -> None:
    with pytest.raises(ValidationError, match="match"):
        WatchCondition(kind="output_contains")


def test_output_idle_condition_requires_idle_duration() -> None:
    with pytest.raises(ValidationError, match="idle_after_seconds"):
        WatchCondition(kind="output_idle")


def test_pane_exited_condition_accepts_no_match() -> None:
    with pytest.raises(ValidationError):
        WatchCondition(kind="pane_exited", match="ready")


def test_watch_create_params_round_trip() -> None:
    params = WatchCreateParams(
        pane_id="%1",
        instance_id=uuid4(),
        conversation_id=uuid4(),
        condition=WatchCondition(kind="output_contains", match="ready"),
        start_cursor=_CURSOR,
        expires_at=datetime.now(UTC),
        one_shot=True,
        intent="wait for the ready prompt",
    )
    dumped = params.model_dump()
    reparsed = WatchCreateParams.model_validate(dumped)
    assert reparsed == params
    assert reparsed.condition.kind is WatchConditionKind.OUTPUT_CONTAINS


def test_rearmable_watch_requires_max_rearms() -> None:
    with pytest.raises(ValidationError, match="max_rearms"):
        WatchCreateParams(
            pane_id="%1",
            conversation_id=uuid4(),
            condition=WatchCondition(kind="output_contains", match="ready"),
            one_shot=False,
        )


def test_watch_list_and_get_params_validate() -> None:
    watch_id = uuid4()
    listed = WatchListParams.model_validate(
        {"conversation_id": uuid4(), "status": "active"}
    )
    assert listed.status is WatchStatus.ACTIVE
    got = WatchGetParams.model_validate({"watch_id": watch_id})
    assert got.watch_id == watch_id
    with pytest.raises(ValidationError):
        WatchListParams.model_validate({"status": "stale"})


def test_watch_cancel_result_is_consistent() -> None:
    ok = WatchCancelResult.model_validate({"watch_id": uuid4(), "ok": True})
    assert ok.error_code is None
    failed = WatchCancelResult.model_validate(
        {"watch_id": uuid4(), "ok": False, "error_code": "watch_not_found"}
    )
    assert failed.error_code is TermFlowErrorCode.WATCH_NOT_FOUND
    with pytest.raises(ValidationError):
        WatchCancelResult.model_validate(
            {"watch_id": uuid4(), "ok": True, "error_code": "pane_not_found"}
        )
