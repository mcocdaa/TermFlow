"""Observation Service and terminal capability port tests (plan §9/§10, task M2.3).

The Observation Service maps bounded tool-level ``PaneReadParams`` onto the
A/B ``PaneCaptureRequestPayload`` wire contract through
``LiveConnection.submit_capture``, enforces byte bounds, rechecks pane
incarnation, and surfaces A-side stream gaps and structured errors.

The service runs against the real ``LiveInstanceRegistry``/``LiveConnection``
with capture replies faked exactly like ``test_capture_pending.py`` does.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from termflow_control_plane.connections.registry import LiveInstanceRegistry
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    ObservationService,
    PaneCursorStore,
    TermFlowToolError,
    UnsupportedCommandService,
)
from termflow_protocol.mcp import (
    MAX_PANE_READ_BYTES,
    PaneCursor,
    PaneReadParams,
    PaneReadView,
    PaneSummary,
    TermFlowErrorCode,
)
from termflow_protocol.messages import (
    CaptureKind,
    PaneCaptureErrorPayload,
    PaneCaptureRequestPayload,
    PaneCaptureResultPayload,
)
from termflow_protocol.topology import PaneSnapshot, TopologySnapshot, WindowSnapshot


def _topology() -> TopologySnapshot:
    return TopologySnapshot(
        session_id="$0",
        session_name="test",
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="win0",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id="%0",
                        window_id="@0",
                        index=0,
                        title="shell",
                        width=80,
                        height=24,
                        active=True,
                        dead=False,
                    ),
                    PaneSnapshot(
                        pane_id="%1",
                        window_id="@0",
                        index=1,
                        title="/etc",
                        width=80,
                        height=24,
                        active=False,
                        dead=False,
                    ),
                ],
            ),
            WindowSnapshot(
                window_id="@1",
                index=1,
                name="win1",
                active=False,
                panes=[
                    PaneSnapshot(
                        pane_id="%2",
                        window_id="@1",
                        index=0,
                        title="editor",
                        width=80,
                        height=24,
                        active=False,
                        dead=True,
                    ),
                ],
            ),
        ],
    )


def _pane_read_params(**overrides) -> PaneReadParams:
    values = dict(pane_id="%0", view=PaneReadView.VIEWPORT)
    values.update(overrides)
    return PaneReadParams(**values)


def _capture_result(
    request: PaneCaptureRequestPayload, **overrides
) -> PaneCaptureResultPayload:
    values = dict(
        request_id=request.request_id,
        instance_id=request.instance_id,
        pane_id=request.pane_id,
        pane_incarnation=request.pane_incarnation,
        content="bounded output",
        stream_id=str(uuid4()),
        from_seq=1,
        to_seq=1,
        capture_kind=request.capture_kind,
        truncated=False,
        gap_reason=None,
        result_code="ok",
        stream_gap=False,
    )
    values.update(overrides)
    return PaneCaptureResultPayload(**values)


async def _register_topology() -> tuple[LiveInstanceRegistry, UUID, object]:
    registry = LiveInstanceRegistry(queue_size=4)
    instance_id = uuid4()
    connection = await registry.register(instance_id)
    # M2.4: A negotiates bounded capture in its bridge hello; without this the
    # connection fails closed and never enqueues capture requests.
    connection.bounded_capture = True
    connection.topology = _topology()
    return registry, instance_id, connection


class FakeCursorStore:
    """In-memory PaneCursorStore for incarnation resolution tests."""

    def __init__(self, cursors: dict[tuple[UUID, str], PaneCursor]) -> None:
        self._cursors = cursors

    async def get_cursor(self, instance_id: UUID, pane_id: str) -> PaneCursor | None:
        return self._cursors.get((instance_id, pane_id))


def _cursor(
    instance_id: UUID, pane_id: str, *, incarnation: int, seq: int = 0
) -> PaneCursor:
    return PaneCursor(
        instance_id=instance_id,
        pane_id=pane_id,
        pane_incarnation=incarnation,
        stream_id=uuid4(),
        seq=seq,
    )


@pytest.mark.asyncio
async def test_list_panes_from_topology() -> None:
    registry, instance_id, _ = await _register_topology()
    service = ObservationService(registry)

    panes = await service.list_panes(instance_id)

    assert [pane.pane_id for pane in panes] == ["%0", "%1", "%2"]
    assert all(isinstance(pane, PaneSummary) for pane in panes)
    assert panes[0].index == 0
    assert panes[0].active is True
    assert panes[0].dead is False
    assert panes[2].dead is True


@pytest.mark.asyncio
async def test_list_panes_for_offline_instance_fails_closed() -> None:
    service = ObservationService(LiveInstanceRegistry(queue_size=4))

    with pytest.raises(TermFlowToolError) as caught:
        await service.list_panes(uuid4())

    assert caught.value.error_code is TermFlowErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_read_viewport_submits_capture_and_maps_result() -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)
    params = _pane_read_params(pane_id="%0")

    submit = asyncio.create_task(service.read_pane(instance_id, params))
    await asyncio.sleep(0)

    request = PaneCaptureRequestPayload.model_validate(
        (await connection.outbound.get()).payload
    )
    assert request.pane_id == "%0"
    assert request.capture_kind is CaptureKind.VIEWPORT
    assert request.max_bytes == params.max_bytes
    assert request.pane_incarnation == 1
    assert request.join_wrapped is False
    assert request.start_line is None
    assert request.stream_id is None

    result = _capture_result(request, stream_id=str(uuid4()))
    assert connection.resolve_capture(request.request_id, result)

    outcome = await submit
    assert outcome.instance_id == instance_id
    assert outcome.pane_id == "%0"
    assert outcome.view is PaneReadView.VIEWPORT
    assert outcome.content == "bounded output"
    assert outcome.stream_id == UUID(result.stream_id)
    assert outcome.from_seq == result.from_seq
    assert outcome.to_seq == result.to_seq
    assert outcome.truncated is False
    assert outcome.stream_gap is None


@pytest.mark.asyncio
async def test_read_history_applies_line_range_tail_and_join_wrapped() -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)
    params = _pane_read_params(
        pane_id="%1",
        view=PaneReadView.HISTORY,
        start_line=3,
        end_line=7,
        join_wrapped=True,
        max_bytes=4096,
    )

    submit = asyncio.create_task(service.read_pane(instance_id, params))
    await asyncio.sleep(0)

    request = PaneCaptureRequestPayload.model_validate(
        (await connection.outbound.get()).payload
    )
    assert request.capture_kind is CaptureKind.HISTORY
    assert request.start_line == 3
    assert request.end_line == 7
    assert request.tail_lines is None
    assert request.join_wrapped is True
    assert request.max_bytes == 4096
    assert request.stream_id is None
    assert request.seq is None

    result = _capture_result(
        request,
        capture_range={"start": 3, "end": 7},
        stream_id=str(uuid4()),
    )
    assert connection.resolve_capture(request.request_id, result)
    outcome = await submit
    assert outcome.captured_start_line == 3
    assert outcome.captured_end_line == 7


@pytest.mark.asyncio
async def test_read_since_uses_cursor_stream_seq_and_incarnation() -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)
    cursor = _cursor(instance_id, "%0", incarnation=2, seq=41)

    submit = asyncio.create_task(
        service.read_pane(
            instance_id, _pane_read_params(view=PaneReadView.SINCE, cursor=cursor)
        )
    )
    await asyncio.sleep(0)

    request = PaneCaptureRequestPayload.model_validate(
        (await connection.outbound.get()).payload
    )
    assert request.capture_kind is CaptureKind.HISTORY
    assert request.stream_id == str(cursor.stream_id)
    assert request.seq == 41
    assert request.pane_incarnation == 2
    assert request.start_line is None
    assert request.tail_lines is None

    result = _capture_result(request, stream_id=str(cursor.stream_id), from_seq=41, to_seq=50)
    assert connection.resolve_capture(request.request_id, result)
    outcome = await submit
    assert outcome.stream_id == cursor.stream_id
    assert outcome.from_seq == 41
    assert outcome.to_seq == 50


@pytest.mark.asyncio
async def test_read_since_surfaces_stream_gap() -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)
    cursor = _cursor(instance_id, "%0", incarnation=2, seq=41)

    submit = asyncio.create_task(
        service.read_pane(
            instance_id, _pane_read_params(view=PaneReadView.SINCE, cursor=cursor)
        )
    )
    await asyncio.sleep(0)
    request = PaneCaptureRequestPayload.model_validate(
        (await connection.outbound.get()).payload
    )
    result = _capture_result(
        request,
        content="",
        stream_id=str(cursor.stream_id),
        from_seq=41,
        to_seq=41,
        stream_gap=True,
        gap_reason="overwritten",
        result_code="stream_gap",
    )
    assert connection.resolve_capture(request.request_id, result)

    outcome = await submit
    assert outcome.content == ""
    assert outcome.stream_gap is not None
    assert outcome.stream_gap.previous_stream_id == cursor.stream_id
    assert outcome.stream_gap.reason == "overwritten"


@pytest.mark.asyncio
async def test_read_rejects_max_bytes_above_protocol_bound() -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)

    with pytest.raises(TermFlowToolError) as caught:
        await service.read_pane(
            instance_id,
            _pane_read_params(max_bytes=MAX_PANE_READ_BYTES + 1),
        )

    assert caught.value.error_code is TermFlowErrorCode.QUOTA_EXCEEDED
    assert connection.outbound.empty()


@pytest.mark.asyncio
async def test_read_defensively_truncates_oversized_capture() -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)
    params = _pane_read_params(max_bytes=1024)

    submit = asyncio.create_task(service.read_pane(instance_id, params))
    await asyncio.sleep(0)
    request = PaneCaptureRequestPayload.model_validate(
        (await connection.outbound.get()).payload
    )
    oversized = _capture_result(request, content="x" * 4096, truncated=False)
    assert connection.resolve_capture(request.request_id, oversized)

    outcome = await submit
    assert outcome.truncated is True
    assert len(outcome.content.encode("utf-8")) <= 1024


@pytest.mark.asyncio
async def test_read_surfaces_malformed_stream_id_as_internal_error() -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)

    submit = asyncio.create_task(
        service.read_pane(instance_id, _pane_read_params(pane_id="%0"))
    )
    await asyncio.sleep(0)
    request = PaneCaptureRequestPayload.model_validate(
        (await connection.outbound.get()).payload
    )
    malformed = _capture_result(request, stream_id="not-a-uuid")
    assert connection.resolve_capture(request.request_id, malformed)

    with pytest.raises(TermFlowToolError) as caught:
        await submit
    assert caught.value.error_code is TermFlowErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_read_pane_missing_from_topology_is_not_found() -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)

    with pytest.raises(TermFlowToolError) as caught:
        await service.read_pane(instance_id, _pane_read_params(pane_id="%99"))

    assert caught.value.error_code is TermFlowErrorCode.PANE_NOT_FOUND
    assert connection.outbound.empty()


@pytest.mark.asyncio
async def test_read_rejects_stale_cursor_incarnation_before_submit() -> None:
    registry, instance_id, connection = await _register_topology()
    store = FakeCursorStore(
        {(instance_id, "%0"): _cursor(instance_id, "%0", incarnation=5)}
    )
    service = ObservationService(registry, cursor_store=store)
    stale = _cursor(instance_id, "%0", incarnation=4, seq=41)

    with pytest.raises(TermFlowToolError) as caught:
        await service.read_pane(
            instance_id,
            _pane_read_params(view=PaneReadView.SINCE, cursor=stale),
        )

    assert caught.value.error_code is TermFlowErrorCode.INCARNATION_CHANGED
    assert connection.outbound.empty()


@pytest.mark.asyncio
async def test_read_uses_resolved_incarnation_for_cursorless_views() -> None:
    registry, instance_id, connection = await _register_topology()
    store = FakeCursorStore(
        {(instance_id, "%0"): _cursor(instance_id, "%0", incarnation=5)}
    )
    service = ObservationService(registry, cursor_store=store)

    submit = asyncio.create_task(
        service.read_pane(instance_id, _pane_read_params(pane_id="%0"))
    )
    await asyncio.sleep(0)

    request = PaneCaptureRequestPayload.model_validate(
        (await connection.outbound.get()).payload
    )
    assert request.pane_incarnation == 5

    result = _capture_result(request, stream_id=str(uuid4()))
    assert connection.resolve_capture(request.request_id, result)
    outcome = await submit
    assert outcome.pane_id == "%0"


@pytest.mark.parametrize(
    ("wire_code", "expected_code"),
    [
        ("pane_not_found", TermFlowErrorCode.PANE_NOT_FOUND),
        ("incarnation_changed", TermFlowErrorCode.INCARNATION_CHANGED),
        ("quota_exceeded", TermFlowErrorCode.QUOTA_EXCEEDED),
        ("stream_gap", TermFlowErrorCode.STREAM_GAP),
        ("invalid_request", TermFlowErrorCode.INVALID_REQUEST),
        ("capture_timeout", TermFlowErrorCode.INTERNAL_ERROR),
        ("some_future_code", TermFlowErrorCode.INTERNAL_ERROR),
    ],
)
@pytest.mark.asyncio
async def test_read_maps_structured_capture_errors(
    wire_code: str, expected_code: TermFlowErrorCode
) -> None:
    registry, instance_id, connection = await _register_topology()
    service = ObservationService(registry)

    submit = asyncio.create_task(
        service.read_pane(instance_id, _pane_read_params(pane_id="%0"))
    )
    await asyncio.sleep(0)
    request = PaneCaptureRequestPayload.model_validate(
        (await connection.outbound.get()).payload
    )
    error = PaneCaptureErrorPayload(
        request_id=request.request_id,
        instance_id=instance_id,
        pane_id="%0",
        error_code=wire_code,
        message="bounded rejection",
    )
    assert connection.resolve_capture(request.request_id, error)

    with pytest.raises(TermFlowToolError) as caught:
        await submit
    assert caught.value.error_code is expected_code


@pytest.mark.asyncio
async def test_read_pane_fails_closed_for_old_a_without_capture_capability() -> None:
    # An old A never negotiates bounded_capture in its bridge hello (M2.4), so
    # the request fails closed with a structured policy error instead of
    # hanging on a capture that will never be enqueued.
    registry = LiveInstanceRegistry(queue_size=4)
    instance_id = uuid4()
    connection = await registry.register(instance_id)
    connection.topology = _topology()
    service = ObservationService(registry)

    with pytest.raises(TermFlowToolError) as caught:
        await service.read_pane(instance_id, _pane_read_params(pane_id="%0"))

    assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
    assert connection.outbound.empty()


@pytest.mark.asyncio
async def test_resolve_cursor_delegates_to_cursor_store() -> None:
    registry, instance_id, _ = await _register_topology()
    cursor = _cursor(instance_id, "%0", incarnation=3, seq=9)
    store = FakeCursorStore({(instance_id, "%0"): cursor})
    service = ObservationService(registry, cursor_store=store)

    assert await service.resolve_cursor(instance_id, "%0") is cursor
    assert await service.resolve_cursor(instance_id, "%1") is None

    without_store = ObservationService(registry)
    assert await without_store.resolve_cursor(instance_id, "%0") is None


@pytest.mark.asyncio
async def test_command_port_is_declared_but_writes_not_implemented() -> None:
    service = UnsupportedCommandService()

    with pytest.raises(NotImplementedError, match="M5"):
        await service.send_text(uuid4(), None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="M5"):
        await service.send_keys(uuid4(), None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_observation_service_satisfies_cursor_store_protocol() -> None:
    assert isinstance(FakeCursorStore({}), PaneCursorStore)
