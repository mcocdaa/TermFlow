"""Correlated pane capture pending plumbing across registry and bridge."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from termflow_control_plane.connections.registry import (
    ConnectionBackpressure,
    InstanceOffline,
    LiveInstanceRegistry,
)
from termflow_protocol import (
    BridgeHelloPayload,
    CaptureKind,
    MessageType,
    PaneCaptureErrorPayload,
    PaneCaptureRequestPayload,
    PaneCaptureResultPayload,
    WireMessage,
)


def _capture_request(instance_id: UUID) -> PaneCaptureRequestPayload:
    return PaneCaptureRequestPayload(
        instance_id=instance_id,
        pane_id="%0",
        capture_kind=CaptureKind.HISTORY,
        request_id=uuid4(),
        max_bytes=8192,
        pane_incarnation=1,
    )


def _capture_result(
    request: PaneCaptureRequestPayload,
) -> PaneCaptureResultPayload:
    return PaneCaptureResultPayload(
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
    )


def _capture_error(
    request: PaneCaptureRequestPayload,
    error_code: str = "pane_not_found",
) -> PaneCaptureErrorPayload:
    return PaneCaptureErrorPayload(
        request_id=request.request_id,
        instance_id=request.instance_id,
        pane_id=request.pane_id,
        error_code=error_code,
        message="bounded rejection",
    )


async def _register_pending_capture(
    connection,
    request_id: UUID,
) -> asyncio.Future[PaneCaptureResultPayload | PaneCaptureErrorPayload]:
    """White-box stand-in for the future ``submit_capture`` registers."""
    future: asyncio.Future[PaneCaptureResultPayload | PaneCaptureErrorPayload] = (
        asyncio.get_running_loop().create_future()
    )
    connection.pending_captures[request_id] = future
    return future


async def _has_pending_capture(connection, request_id: UUID) -> bool:
    return request_id in connection.pending_captures


@pytest.mark.asyncio
async def test_submit_capture_enqueues_request_and_resolves_with_result() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    connection = await registry.register(uuid4())
    request = _capture_request(connection.instance_id)
    submit = asyncio.create_task(connection.submit_capture(request))

    await asyncio.sleep(0)
    assert request.request_id in connection.pending_captures

    forwarded = await connection.outbound.get()
    assert forwarded.type is MessageType.PANE_CAPTURE_REQUEST
    assert forwarded.instance_id == connection.instance_id
    assert forwarded.payload == request.model_dump(mode="json")

    result = _capture_result(request)
    assert connection.resolve_capture(request.request_id, result)
    assert await submit is result
    assert request.request_id not in connection.pending_captures


@pytest.mark.asyncio
async def test_submit_capture_resolves_with_error_payload() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    connection = await registry.register(uuid4())
    request = _capture_request(connection.instance_id)
    submit = asyncio.create_task(connection.submit_capture(request))

    await asyncio.sleep(0)
    error = _capture_error(request)
    assert connection.resolve_capture(request.request_id, error)
    assert await submit is error
    assert request.request_id not in connection.pending_captures


@pytest.mark.asyncio
async def test_submit_capture_times_out_with_bounded_error() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    connection = await registry.register(uuid4())
    request = _capture_request(connection.instance_id)
    submit = asyncio.create_task(connection.submit_capture(request, timeout_seconds=0.05))

    outcome = await submit
    assert isinstance(outcome, PaneCaptureErrorPayload)
    assert outcome.request_id == request.request_id
    assert outcome.error_code == "capture_timeout"
    assert request.request_id not in connection.pending_captures


@pytest.mark.asyncio
async def test_resolve_capture_ignores_unknown_request_id() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    connection = await registry.register(uuid4())
    result = _capture_result(_capture_request(connection.instance_id))
    assert not connection.resolve_capture(uuid4(), result)
    assert not connection.pending_captures


@pytest.mark.asyncio
async def test_submit_capture_backpressure_cleans_pending_entry() -> None:
    registry = LiveInstanceRegistry(queue_size=1)
    connection = await registry.register(uuid4())
    heartbeat = WireMessage(
        type=MessageType.BRIDGE_HEARTBEAT,
        instance_id=connection.instance_id,
        payload={},
    )
    await registry.enqueue(connection.instance_id, heartbeat)
    request = _capture_request(connection.instance_id)

    with pytest.raises(ConnectionBackpressure):
        await connection.submit_capture(request)
    assert request.request_id not in connection.pending_captures


@pytest.mark.asyncio
async def test_submit_capture_rejects_foreign_instance_request() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    connection = await registry.register(uuid4())
    request = _capture_request(uuid4())

    with pytest.raises(ValueError):
        await connection.submit_capture(request)


@pytest.mark.asyncio
async def test_unregister_fails_pending_captures() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    connection = await registry.register(uuid4())
    request = _capture_request(connection.instance_id)
    submit = asyncio.create_task(connection.submit_capture(request))
    await asyncio.sleep(0)

    assert await registry.unregister(connection)
    with pytest.raises(InstanceOffline):
        await submit
    assert not connection.pending_captures


@pytest.mark.asyncio
async def test_expire_before_fails_pending_captures() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    connection = await registry.register(uuid4())
    request = _capture_request(connection.instance_id)
    submit = asyncio.create_task(connection.submit_capture(request))
    await asyncio.sleep(0)
    connection.last_heartbeat = datetime.now(UTC) - timedelta(minutes=1)

    expired = await registry.expire_before(datetime.now(UTC) - timedelta(seconds=30))
    assert expired == [connection]
    with pytest.raises(InstanceOffline):
        await submit
    assert not connection.pending_captures


def test_bridge_capture_round_trip_resolves_pending_future(
    client,
    provision_term,
) -> None:
    term = provision_term(name="capture")
    instance_id, instance_token = term.instance_id, term.instance_token
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        bridge.send_text(
            WireMessage(
                type=MessageType.BRIDGE_HELLO,
                instance_id=instance_id,
                payload=BridgeHelloPayload(name="capture").model_dump(mode="json"),
            ).model_dump_json()
        )
        registry = client.app.state.registry
        connection = _wait_for_connection(client, registry, instance_id)

        request = _capture_request(instance_id)
        submit_future = client.portal.start_task_soon(connection.submit_capture, request)

        forwarded = bridge.receive_json()
        assert forwarded["type"] == "pane.capture_request"
        assert forwarded["instance_id"] == str(instance_id)
        assert forwarded["payload"]["request_id"] == str(request.request_id)

        result = _capture_result(request)
        bridge.send_text(
            WireMessage(
                type=MessageType.PANE_CAPTURE_RESULT,
                instance_id=instance_id,
                payload=result.model_dump(mode="json"),
            ).model_dump_json()
        )
        outcome = submit_future.result(timeout=5)
        assert outcome == result
        assert (
            client.portal.call(
                _has_pending_capture, connection, request.request_id
            )
            is False
        )


def test_bridge_capture_error_resolves_pending_future(
    client,
    provision_term,
) -> None:
    term = provision_term(name="capture")
    instance_id, instance_token = term.instance_id, term.instance_token
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        bridge.send_text(
            WireMessage(
                type=MessageType.BRIDGE_HELLO,
                instance_id=instance_id,
                payload=BridgeHelloPayload(name="capture").model_dump(mode="json"),
            ).model_dump_json()
        )
        registry = client.app.state.registry
        connection = _wait_for_connection(client, registry, instance_id)

        request = _capture_request(instance_id)
        future = client.portal.call(
            _register_pending_capture, connection, request.request_id
        )
        error = _capture_error(request)
        bridge.send_text(
            WireMessage(
                type=MessageType.PANE_CAPTURE_ERROR,
                instance_id=instance_id,
                payload=error.model_dump(mode="json"),
            ).model_dump_json()
        )
        _wait_for_done(future)
        assert future.result() == error


def test_bridge_rejects_capture_result_from_foreign_instance(
    client,
    provision_term,
) -> None:
    term = provision_term(name="capture")
    instance_id, instance_token = term.instance_id, term.instance_token
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        bridge.send_text(
            WireMessage(
                type=MessageType.BRIDGE_HELLO,
                instance_id=instance_id,
                payload=BridgeHelloPayload(name="capture").model_dump(mode="json"),
            ).model_dump_json()
        )
        registry = client.app.state.registry
        connection = _wait_for_connection(client, registry, instance_id)

        request = _capture_request(instance_id)
        future = client.portal.call(
            _register_pending_capture, connection, request.request_id
        )
        bridge.send_text(
            WireMessage(
                type=MessageType.PANE_CAPTURE_RESULT,
                instance_id=uuid4(),
                payload=_capture_result(request).model_dump(mode="json"),
            ).model_dump_json()
        )
        with pytest.raises(WebSocketDisconnect) as caught:
            bridge.receive_text()
    assert caught.value.code == 4403
    # The mismatched envelope was never applied: teardown fails the pending
    # future with InstanceOffline instead of resolving it with the result.
    _wait_for_done(future)
    with pytest.raises(InstanceOffline):
        future.result()


def test_bridge_ignores_capture_result_without_pending_request(
    client,
    provision_term,
) -> None:
    term = provision_term(name="capture")
    instance_id, instance_token = term.instance_id, term.instance_token
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        bridge.send_text(
            WireMessage(
                type=MessageType.BRIDGE_HELLO,
                instance_id=instance_id,
                payload=BridgeHelloPayload(name="capture").model_dump(mode="json"),
            ).model_dump_json()
        )
        registry = client.app.state.registry
        connection = _wait_for_connection(client, registry, instance_id)

        result = _capture_result(_capture_request(instance_id))
        bridge.send_text(
            WireMessage(
                type=MessageType.PANE_CAPTURE_RESULT,
                instance_id=instance_id,
                payload=result.model_dump(mode="json"),
            ).model_dump_json()
        )
        time.sleep(0.05)
        assert client.portal.call(registry.maybe_get, instance_id) is connection


def test_bridge_rejects_capture_request_from_instance(
    client,
    provision_term,
) -> None:
    term = provision_term(name="capture")
    instance_id, instance_token = term.instance_id, term.instance_token
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {instance_token}"},
    ) as bridge:
        bridge.send_text(
            WireMessage(
                type=MessageType.BRIDGE_HELLO,
                instance_id=instance_id,
                payload=BridgeHelloPayload(name="capture").model_dump(mode="json"),
            ).model_dump_json()
        )
        registry = client.app.state.registry
        connection = _wait_for_connection(client, registry, instance_id)

        request = _capture_request(instance_id)
        bridge.send_text(
            WireMessage(
                type=MessageType.PANE_CAPTURE_REQUEST,
                instance_id=instance_id,
                payload=request.model_dump(mode="json"),
            ).model_dump_json()
        )
        rejection = bridge.receive_json()
        assert rejection["type"] == "pane.capture_error"
        assert rejection["payload"]["request_id"] == str(request.request_id)
        assert rejection["payload"]["error_code"] == "invalid_request"
        assert client.portal.call(registry.maybe_get, instance_id) is connection


def _wait_for_connection(client, registry, instance_id: UUID):
    for _ in range(100):
        connection = client.portal.call(registry.maybe_get, instance_id)
        if connection is not None:
            return connection
        time.sleep(0.01)
    raise AssertionError("bridge connection never registered")


def _wait_for_done(future) -> None:
    for _ in range(100):
        if future.done():
            return
        time.sleep(0.01)
    raise AssertionError("pending future never resolved")
