"""M2 verification suite: envelope validation, cross-Term identity, old-A
compatibility, backpressure gaps, and pane replacement/cursor recovery
(plan §19 M2 checklist remaining items, task M2.5).

Coverage:

* **Envelope validation before EventHub/persistence** (plan §19 M2 item
  "Reject A envelopes whose ``instance_id``, pane identity, or stream epoch
  does not match the authenticated bridge connection/current topology"): a
  simulated A that streams ``PANE_OUTPUT``/``STREAM_GAP`` for a pane outside
  the current topology, before any topology is known, with duplicate,
  out-of-order, or unannounced stream-epoch changes never reaches EventHub
  subscribers, never moves the connection's accepted stream state, and never
  persists an observation cursor.
* **Cross-Term pane identity**: two Terms (instances) each carrying a pane
  ``%1`` never share state — pane reads through the Observation Service stay
  on their own instance, the observation cursor ledger is keyed by
  ``(instance_id, pane_id)``, and the EventHub isolates per instance.
* **Old-A compatibility**: a bridge hello without the capability fields fails
  closed end to end (``capture_unsupported`` -> ``POLICY_DENIED``, nothing
  enqueued on the wire).
* **Backpressure/drop-to-gap**: an explicit ``STREAM_GAP(reason=backpressure)``
  is forwarded and the fresh stream that follows it is accepted, while a stale
  stream after the gap is rejected (A-side ring behavior is verified in
  ``apps/node/tests/test_bridge_runtime.py``).
* **Pane replacement and cursor recovery**: a stale incarnation cursor is
  rejected before submit, the current incarnation is resolved from the
  persisted cursor store, and a ``since`` read is re-issued from the persisted
  stream/seq after a restart, surfacing A-side ring gaps.

Watch-level duplicate/out-of-order/epoch-rollback rejection is covered by
``test_watch_engine.py``; this module adds the persistence-key isolation and
the bridge ingress checks the watch engine depends on.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect
from termflow_control_plane.connections.event_hub import EventSubscriber
from termflow_control_plane.connections.registry import LiveConnection, LiveInstanceRegistry
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.models import PaneObservationCursor
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    ObservationService,
    TermFlowToolError,
)
from termflow_control_plane.plugins.agent_broker.agent.watches import (
    CursorAcceptance,
    ObservationCursorStore,
)
from termflow_protocol import (
    BridgeHelloPayload,
    MessageType,
    PaneCaptureRequestPayload,
    PaneCaptureResultPayload,
    PaneOutputPayload,
    PaneSnapshot,
    StreamGapPayload,
    TopologySnapshot,
    TopologySnapshotPayload,
    WindowSnapshot,
    WireMessage,
)
from termflow_protocol.mcp import (
    PaneCursor,
    PaneReadParams,
    PaneReadView,
    TermFlowErrorCode,
)


def _topology(*pane_ids: str, revision: int = 1) -> TopologySnapshot:
    return TopologySnapshot(
        session_id="$0",
        session_name="test",
        revision=revision,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="win0",
                active=True,
                panes=[
                    PaneSnapshot(
                        pane_id=pane_id,
                        window_id="@0",
                        index=index,
                        title="shell",
                        width=80,
                        height=24,
                        active=index == 0,
                        dead=False,
                    )
                    for index, pane_id in enumerate(pane_ids)
                ],
            )
        ],
    )


def _hello(term, *, bounded_capture: bool = True, old_a: bool = False) -> str:
    payload: dict[str, object] = (
        {"name": "alpha"}
        if old_a
        else BridgeHelloPayload(name="alpha", bounded_capture=bounded_capture).model_dump(
            mode="json"
        )
    )
    return WireMessage(
        type=MessageType.BRIDGE_HELLO,
        instance_id=term.instance_id,
        payload=payload,
    ).model_dump_json()


def _topology_message(term, topology: TopologySnapshot) -> str:
    return WireMessage(
        type=MessageType.TOPOLOGY_SNAPSHOT,
        instance_id=term.instance_id,
        payload=TopologySnapshotPayload(topology=topology).model_dump(mode="json"),
    ).model_dump_json()


def _output(
    instance_id: UUID,
    pane_id: str,
    stream_id: UUID,
    seq: int,
    data: bytes = b"x",
) -> WireMessage:
    payload = PaneOutputPayload.from_bytes(pane_id, stream_id, seq, data)
    return WireMessage(
        type=MessageType.PANE_OUTPUT,
        instance_id=instance_id,
        payload=payload.model_dump(mode="json"),
    )


def _gap(
    instance_id: UUID,
    pane_id: str,
    previous_stream_id: UUID,
    reason: str = "backpressure",
) -> WireMessage:
    payload = StreamGapPayload(
        pane_id=pane_id,
        previous_stream_id=previous_stream_id,
        reason=reason,
    )
    return WireMessage(
        type=MessageType.STREAM_GAP,
        instance_id=instance_id,
        payload=payload.model_dump(mode="json"),
    )


@contextmanager
def _bridge(client, term, *, topology: TopologySnapshot | None = None, old_a: bool = False):
    """Open an authenticated bridge and (optionally) send hello + topology."""
    with client.websocket_connect(
        "/api/v1/bridge/connect",
        headers={"Authorization": f"Bearer {term.instance_token}"},
    ) as websocket:
        websocket.send_text(_hello(term, old_a=old_a))
        if topology is not None:
            websocket.send_text(_topology_message(term, topology))
        yield websocket


def _wait_for_connection(client, instance_id: UUID) -> LiveConnection:
    registry = client.app.state.registry
    for _ in range(100):
        connection = client.portal.call(registry.maybe_get, instance_id)
        if connection is not None and connection.topology is not None:
            return connection
        time.sleep(0.01)
    raise AssertionError("bridge connection never registered with a topology")


def _subscribe(client, instance_id: UUID) -> EventSubscriber:
    return client.portal.call(client.app.state.event_hub.subscribe, instance_id)


async def _hub_receive(subscriber: EventSubscriber, timeout_seconds: float = 2.0):
    return await asyncio.wait_for(subscriber.queue.get(), timeout=timeout_seconds)


async def _hub_drain(subscriber: EventSubscriber) -> list[WireMessage]:
    drained: list[WireMessage] = []
    while True:
        try:
            drained.append(subscriber.queue.get_nowait())
        except asyncio.QueueEmpty:
            return drained


def _next_pane_event(client, subscriber: EventSubscriber) -> WireMessage:
    """The next PANE_OUTPUT/STREAM_GAP, skipping control/topology messages.

    Because the bridge processes envelopes sequentially, any envelope that was
    rejected by the bridge can never be queued before the accepted one.
    """
    while True:
        message = client.portal.call(_hub_receive, subscriber)
        if message.type in {MessageType.PANE_OUTPUT, MessageType.STREAM_GAP}:
            return message
        assert message.type in {MessageType.TOPOLOGY_SNAPSHOT, MessageType.TOPOLOGY_CHANGED}


def _assert_no_stray_pane_events(client, subscriber: EventSubscriber) -> None:
    drained = client.portal.call(_hub_drain, subscriber)
    assert all(
        message.type in {MessageType.TOPOLOGY_SNAPSHOT, MessageType.TOPOLOGY_CHANGED}
        for message in drained
    ), [message.type for message in drained]


def _cursor(
    instance_id: UUID,
    pane_id: str,
    *,
    incarnation: int = 1,
    seq: int = 5,
    stream_id: UUID | None = None,
) -> PaneCursor:
    return PaneCursor(
        instance_id=instance_id,
        pane_id=pane_id,
        pane_incarnation=incarnation,
        stream_id=stream_id or uuid4(),
        seq=seq,
    )


def _capture_result(request: PaneCaptureRequestPayload, **overrides) -> PaneCaptureResultPayload:
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


async def _register_topology(
    registry: LiveInstanceRegistry,
    instance_id: UUID | None = None,
    *,
    topology: TopologySnapshot | None = None,
) -> tuple[UUID, LiveConnection]:
    instance_id = instance_id or uuid4()
    connection = await registry.register(instance_id)
    connection.bounded_capture = True
    connection.topology = topology or _topology("%0")
    return instance_id, connection


@pytest_asyncio.fixture
async def repositories(tmp_path) -> RepositoryBundle:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'observation-verify.db'}")
    await database.initialize()
    bundle = RepositoryBundle(database.session_factory)
    bundle.session_factory = database.session_factory  # type: ignore[attr-defined]
    try:
        yield bundle
    finally:
        await database.dispose()


async def _register_instance(repositories: RepositoryBundle, instance_id: UUID) -> None:
    installation = await repositories.installations.create(digest_secret(f"computer-{uuid4().hex}"))
    await repositories.instances.register_or_rotate(
        instance_id,
        installation.id,
        f"term-{instance_id.hex[:8]}",
        digest_secret(f"term-{uuid4().hex}"),
    )


# ---------------------------------------------------------------------------
# Envelope validation before EventHub fan-out (bridge ingress)
# ---------------------------------------------------------------------------


def test_bridge_forwards_contiguous_pane_output(client, provision_term) -> None:
    term = provision_term(name="verify")
    stream = uuid4()
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        first = _output(term.instance_id, "%0", stream, 1)
        second = _output(term.instance_id, "%0", stream, 2, b"y")
        bridge.send_text(first.model_dump_json())
        bridge.send_text(second.model_dump_json())
        received = _next_pane_event(client, subscriber)
        assert received.payload == first.payload
        received = _next_pane_event(client, subscriber)
        assert received.payload == second.payload
        _assert_no_stray_pane_events(client, subscriber)
        assert connection.pane_streams["%0"] == (stream, 2)


def test_replay_deliveries_are_forwarded_not_rejected_as_stale(
    client, admin_headers, provision_term
) -> None:
    # A client that missed live output requests a replay from A's ring. A
    # answers by re-sending chunks the bridge already accepted; those
    # re-deliveries must reach the subscriber instead of being rejected as
    # duplicates/out-of-order (plan §9.2/§17 replay semantics).
    term = provision_term(name="verify")
    stream = uuid4()
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        for seq in range(1, 11):
            bridge.send_text(_output(term.instance_id, "%0", stream, seq).model_dump_json())
        replay_query = (
            f"/api/v1/events?instance_id={term.instance_id}"
            f"&pane_id=%0&stream_id={stream}&after_seq=5"
        )
        with client.websocket_connect(replay_query, headers=admin_headers):
            request = WireMessage.model_validate(bridge.receive_json())
            assert request.type is MessageType.PANE_REPLAY_REQUEST
            assert request.payload["after_seq"] == 5
            # A re-sends the requested range from its ring.
            for seq in range(6, 11):
                bridge.send_text(_output(term.instance_id, "%0", stream, seq).model_dump_json())
            received: list[WireMessage] = []
            for _ in range(15):
                message = _next_pane_event(client, subscriber)
                received.append(message)
            assert [message.payload["seq"] for message in received] == list(range(1, 11)) + list(
                range(6, 11)
            )
            _assert_no_stray_pane_events(client, subscriber)
        # The replayed re-deliveries never moved the live ledger; the window
        # stays armed for the requested stream/range until the epoch changes.
        assert connection.pane_streams["%0"] == (stream, 10)
        assert connection.replay_windows[("%0", stream)] == 5


def test_bridge_rejects_pane_output_for_pane_not_in_topology(client, provision_term) -> None:
    term = provision_term(name="verify")
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        # An A envelope names a pane that is not part of the current topology.
        bridge.send_text(_output(term.instance_id, "%1", uuid4(), 1).model_dump_json())
        # A valid envelope for a topology pane still flows.
        expected = _output(term.instance_id, "%0", uuid4(), 1)
        bridge.send_text(expected.model_dump_json())
        received = _next_pane_event(client, subscriber)
        assert received.payload == expected.payload
        _assert_no_stray_pane_events(client, subscriber)
        assert (
            client.portal.call(client.app.state.registry.maybe_get, term.instance_id) is connection
        )
        assert "%1" not in connection.pane_streams


def test_bridge_rejects_pane_output_before_topology_is_known(client, provision_term) -> None:
    term = provision_term(name="verify")
    with _bridge(client, term) as bridge:
        connection = None
        for _ in range(100):
            connection = client.portal.call(client.app.state.registry.maybe_get, term.instance_id)
            if connection is not None and connection.hello_ready.is_set():
                break
            time.sleep(0.01)
        assert connection is not None
        subscriber = _subscribe(client, term.instance_id)
        # No topology yet: the pane identity cannot be verified, so the
        # envelope fails closed instead of being fanned out.
        bridge.send_text(_output(term.instance_id, "%0", uuid4(), 1).model_dump_json())
        bridge.send_text(_topology_message(term, _topology("%0")))
        expected = _output(term.instance_id, "%0", uuid4(), 1)
        bridge.send_text(expected.model_dump_json())
        received = _next_pane_event(client, subscriber)
        assert received.payload == expected.payload
        _assert_no_stray_pane_events(client, subscriber)


def test_bridge_rejects_duplicate_and_out_of_order_pane_output(client, provision_term) -> None:
    term = provision_term(name="verify")
    stream = uuid4()
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        first = _output(term.instance_id, "%0", stream, 1)
        second = _output(term.instance_id, "%0", stream, 2)
        third = _output(term.instance_id, "%0", stream, 3)
        bridge.send_text(first.model_dump_json())
        # Duplicate chunk.
        bridge.send_text(_output(term.instance_id, "%0", stream, 1).model_dump_json())
        # Unannounced seq jump: continuity cannot be proven without a gap.
        bridge.send_text(_output(term.instance_id, "%0", stream, 4).model_dump_json())
        # The ledger stays at 1, so seq 2 is still the contiguous next chunk;
        # seq 3 continues it, and the seq-2 replay after that is out-of-order.
        bridge.send_text(second.model_dump_json())
        bridge.send_text(third.model_dump_json())
        bridge.send_text(_output(term.instance_id, "%0", stream, 2).model_dump_json())
        received = _next_pane_event(client, subscriber)
        assert received.payload == first.payload
        received = _next_pane_event(client, subscriber)
        assert received.payload == second.payload
        received = _next_pane_event(client, subscriber)
        assert received.payload == third.payload
        _assert_no_stray_pane_events(client, subscriber)
        assert connection.pane_streams["%0"] == (stream, 3)


def test_bridge_rejects_unannounced_stream_epoch_change(client, provision_term) -> None:
    term = provision_term(name="verify")
    first_stream = uuid4()
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        expected = _output(term.instance_id, "%0", first_stream, 1)
        bridge.send_text(expected.model_dump_json())
        # A new stream without an announced gap is a spoofed/stale epoch.
        bridge.send_text(_output(term.instance_id, "%0", uuid4(), 1).model_dump_json())
        received = _next_pane_event(client, subscriber)
        assert received.payload == expected.payload
        _assert_no_stray_pane_events(client, subscriber)
        assert connection.pane_streams["%0"] == (first_stream, 1)


def test_bridge_rejects_same_stream_after_announced_gap(client, provision_term) -> None:
    term = provision_term(name="verify")
    first_stream = uuid4()
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        expected = _output(term.instance_id, "%0", first_stream, 1)
        gap = _gap(term.instance_id, "%0", first_stream)
        bridge.send_text(expected.model_dump_json())
        bridge.send_text(gap.model_dump_json())
        # The gap announced a change; a chunk on the old stream contradicts it.
        bridge.send_text(_output(term.instance_id, "%0", first_stream, 2).model_dump_json())
        fresh = uuid4()
        fresh_output = _output(term.instance_id, "%0", fresh, 1)
        bridge.send_text(fresh_output.model_dump_json())
        received = _next_pane_event(client, subscriber)
        assert received.payload == expected.payload
        received = _next_pane_event(client, subscriber)
        assert received.type is MessageType.STREAM_GAP
        assert received.payload == gap.payload
        received = _next_pane_event(client, subscriber)
        assert received.payload == fresh_output.payload
        _assert_no_stray_pane_events(client, subscriber)
        assert connection.pane_streams["%0"] == (fresh, 1)


def test_bridge_forwards_backpressure_gap_and_accepts_fresh_stream(client, provision_term) -> None:
    term = provision_term(name="verify")
    dropped = uuid4()
    fresh = uuid4()
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        dropped_output = _output(term.instance_id, "%0", dropped, 1)
        gap = _gap(term.instance_id, "%0", dropped)
        fresh_first = _output(term.instance_id, "%0", fresh, 1)
        fresh_second = _output(term.instance_id, "%0", fresh, 2)
        bridge.send_text(dropped_output.model_dump_json())
        bridge.send_text(gap.model_dump_json())
        bridge.send_text(fresh_first.model_dump_json())
        bridge.send_text(fresh_second.model_dump_json())
        received = _next_pane_event(client, subscriber)
        assert received.payload == dropped_output.payload
        received = _next_pane_event(client, subscriber)
        assert received.type is MessageType.STREAM_GAP
        assert received.payload == gap.payload
        assert received.payload["reason"] == "backpressure"
        received = _next_pane_event(client, subscriber)
        assert received.payload == fresh_first.payload
        received = _next_pane_event(client, subscriber)
        assert received.payload == fresh_second.payload
        _assert_no_stray_pane_events(client, subscriber)
        assert connection.pane_streams["%0"] == (fresh, 2)


def test_bridge_rejects_stream_gap_for_pane_not_in_topology(client, provision_term) -> None:
    term = provision_term(name="verify")
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        bridge.send_text(_gap(term.instance_id, "%1", uuid4()).model_dump_json())
        bridge.send_text(_gap(term.instance_id, "%0", uuid4()).model_dump_json())
        gap = _next_pane_event(client, subscriber)
        assert gap.type is MessageType.STREAM_GAP
        assert gap.payload["pane_id"] == "%0"
        _assert_no_stray_pane_events(client, subscriber)
        assert "%1" not in connection.pane_gap_pending


def test_bridge_resets_pane_stream_state_when_pane_leaves_topology(client, provision_term) -> None:
    term = provision_term(name="verify")
    stream = uuid4()
    with _bridge(client, term, topology=_topology("%0", "%1")) as bridge:
        connection = _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        expected = _output(term.instance_id, "%1", stream, 1)
        bridge.send_text(expected.model_dump_json())
        # The pane leaves the topology; its accepted stream state is dropped
        # so a later reappearance anchors fresh instead of blackholing.
        bridge.send_text(_topology_message(term, _topology("%0")))
        bridge.send_text(_topology_message(term, _topology("%0", "%1")))
        reappeared = _output(term.instance_id, "%1", uuid4(), 1)
        bridge.send_text(reappeared.model_dump_json())
        first = _next_pane_event(client, subscriber)
        assert first.payload == expected.payload
        second = _next_pane_event(client, subscriber)
        assert second.payload == reappeared.payload
        _assert_no_stray_pane_events(client, subscriber)
        assert connection.pane_streams["%1"] == (
            UUID(reappeared.payload["stream_id"]),
            1,
        )


def test_bridge_rejects_foreign_instance_pane_output(client, provision_term) -> None:
    term = provision_term(name="verify")
    with _bridge(client, term, topology=_topology("%0")) as bridge:
        _wait_for_connection(client, term.instance_id)
        subscriber = _subscribe(client, term.instance_id)
        bridge.send_text(_output(uuid4(), "%0", uuid4(), 1).model_dump_json())
        with pytest.raises(WebSocketDisconnect) as caught:
            bridge.receive_text()
        assert caught.value.code == 4403
        # Only the teardown presence message may follow; the spoofed envelope
        # was never published.
        drained = client.portal.call(_hub_drain, subscriber)
        assert all(message.type is MessageType.INSTANCE_OFFLINE for message in drained)


# ---------------------------------------------------------------------------
# Watch persistence: rejected envelopes never persist a cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_envelopes_never_persist_or_regress_cursor(
    repositories: RepositoryBundle,
) -> None:
    instance_id = uuid4()
    await _register_instance(repositories, instance_id)
    store = ObservationCursorStore(repositories.session_factory)
    stream = uuid4()
    async with repositories.session_factory() as session:
        acceptance, _ = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=stream,
            seq=1,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.ACCEPTED
        # Duplicate, unannounced gap, and out-of-order envelopes are rejected
        # without moving the ledger; only the contiguous next seq is accepted.
        for seq, expected in (
            (1, CursorAcceptance.DUPLICATE),
            (3, CursorAcceptance.GAP),
            (1, CursorAcceptance.DUPLICATE),
            (2, CursorAcceptance.ACCEPTED),
            (2, CursorAcceptance.DUPLICATE),
            (4, CursorAcceptance.GAP),
            (1, CursorAcceptance.OUT_OF_ORDER),
        ):
            acceptance, _ = await store.accept(
                session,
                instance_id=instance_id,
                pane_id="%0",
                stream_id=stream,
                seq=seq,
                observed_at=datetime.now(UTC),
            )
            assert acceptance is expected
        # An unannounced stream change and a recovered epoch rollback are
        # rejected; a newer recovered incarnation is the only stream reset.
        acceptance, _ = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=uuid4(),
            seq=1,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.STREAM_CHANGED
        replacement = uuid4()
        acceptance, _ = await store.accept_recovered(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=replacement,
            seq=1,
            pane_incarnation=2,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.INCARNATION_CHANGED
        acceptance, _ = await store.accept_recovered(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=replacement,
            seq=1,
            pane_incarnation=1,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.EPOCH_ROLLBACK
        await session.commit()
    accepted = await store.get_cursor(instance_id, "%0")
    assert accepted is not None
    assert accepted.stream_id == replacement
    assert accepted.seq == 1
    assert accepted.pane_incarnation == 2
    async with repositories.session_factory() as session:
        rows = (await session.scalars(select(PaneObservationCursor))).all()
    # One pane => one persisted row; the rejected envelopes never created or
    # mutated it.
    assert len(rows) == 1
    assert rows[0].stream_id == str(replacement)
    assert rows[0].seq == 1
    assert rows[0].pane_incarnation == "2"


# ---------------------------------------------------------------------------
# Cross-Term pane identity: two Terms with the same tmux %1 pane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_ledger_keys_are_per_instance(
    repositories: RepositoryBundle,
) -> None:
    first = uuid4()
    second = uuid4()
    first_stream = uuid4()
    second_stream = uuid4()
    await _register_instance(repositories, first)
    await _register_instance(repositories, second)
    store = ObservationCursorStore(repositories.session_factory)
    async with repositories.session_factory() as session:
        for instance_id, stream_id in (
            (first, first_stream),
            (second, second_stream),
        ):
            acceptance, _ = await store.accept(
                session,
                instance_id=instance_id,
                pane_id="%1",
                stream_id=stream_id,
                seq=1,
                observed_at=datetime.now(UTC),
            )
            assert acceptance is CursorAcceptance.ACCEPTED
        await session.commit()
    first_cursor = await store.get_cursor(first, "%1")
    second_cursor = await store.get_cursor(second, "%1")
    assert first_cursor is not None and second_cursor is not None
    assert first_cursor.stream_id == first_stream
    assert second_cursor.stream_id == second_stream
    assert first_cursor.instance_id == first
    assert second_cursor.instance_id == second
    # A pane replacement on one Term never leaks into the other Term's
    # ledger entry.
    async with repositories.session_factory() as session:
        acceptance, _ = await store.accept_recovered(
            session,
            instance_id=first,
            pane_id="%1",
            stream_id=uuid4(),
            seq=1,
            pane_incarnation=2,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.INCARNATION_CHANGED
        await session.commit()
    assert (await store.get_cursor(first, "%1")).pane_incarnation == 2
    assert (await store.get_cursor(second, "%1")).pane_incarnation == 1


@pytest.mark.asyncio
async def test_observation_service_reads_same_pane_id_without_crossing_instances() -> None:
    registry = LiveInstanceRegistry(queue_size=4)
    first_id, first_connection = await _register_topology(registry, topology=_topology("%1"))
    second_id, second_connection = await _register_topology(registry, topology=_topology("%1"))
    service = ObservationService(registry)

    first_read = asyncio.create_task(
        service.read_pane(first_id, PaneReadParams(pane_id="%1", view=PaneReadView.VIEWPORT))
    )
    second_read = asyncio.create_task(
        service.read_pane(second_id, PaneReadParams(pane_id="%1", view=PaneReadView.VIEWPORT))
    )
    await asyncio.sleep(0)
    first_request = PaneCaptureRequestPayload.model_validate(
        (await first_connection.outbound.get()).payload
    )
    second_request = PaneCaptureRequestPayload.model_validate(
        (await second_connection.outbound.get()).payload
    )
    assert first_request.instance_id == first_id
    assert second_request.instance_id == second_id
    # The two requests are answered with distinct content: each read must
    # return exactly the content of its own Term.
    assert first_connection.resolve_capture(
        first_request.request_id, _capture_result(first_request, content="content-A")
    )
    assert second_connection.resolve_capture(
        second_request.request_id, _capture_result(second_request, content="content-B")
    )
    assert (await first_read).content == "content-A"
    assert (await second_read).content == "content-B"


def test_hub_isolates_same_pane_id_across_instances(client, provision_term) -> None:
    term_a = provision_term(name="verify-a")
    term_b = provision_term(name="verify-b")
    stream = uuid4()
    with _bridge(client, term_a, topology=_topology("%1")) as bridge_a:
        with _bridge(client, term_b, topology=_topology("%1")) as bridge_b:
            _wait_for_connection(client, term_a.instance_id)
            _wait_for_connection(client, term_b.instance_id)
            subscriber_a = _subscribe(client, term_a.instance_id)
            bridge_b.send_text(_output(term_b.instance_id, "%1", stream, 1, b"b").model_dump_json())
            bridge_a.send_text(_output(term_a.instance_id, "%1", stream, 1, b"a").model_dump_json())
            received = _next_pane_event(client, subscriber_a)
            assert received.instance_id == term_a.instance_id
            assert PaneOutputPayload.model_validate(received.payload).to_bytes() == b"a"
            _assert_no_stray_pane_events(client, subscriber_a)


# ---------------------------------------------------------------------------
# Old-A compatibility: fail closed end to end through the bridge API
# ---------------------------------------------------------------------------


def test_old_a_hello_fails_closed_for_capture_end_to_end(client, provision_term) -> None:
    term = provision_term(name="verify")
    with _bridge(client, term, topology=_topology("%0"), old_a=True):
        connection = _wait_for_connection(client, term.instance_id)
        assert connection.bounded_capture is False
        assert connection.typed_keys is False
        service = ObservationService(client.app.state.registry)
        with pytest.raises(TermFlowToolError) as caught:
            client.portal.call(
                service.read_pane,
                term.instance_id,
                PaneReadParams(pane_id="%0", view=PaneReadView.VIEWPORT),
            )
        assert caught.value.error_code is TermFlowErrorCode.POLICY_DENIED
        # Nothing was enqueued toward A and no capture is pending.
        assert connection.outbound.empty()
        assert not connection.pending_captures


# ---------------------------------------------------------------------------
# Pane replacement and cursor recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_recovery_reissues_since_read_from_persisted_cursor(
    repositories: RepositoryBundle,
) -> None:
    instance_id = uuid4()
    await _register_instance(repositories, instance_id)
    store = ObservationCursorStore(repositories.session_factory)
    stream = uuid4()
    async with repositories.session_factory() as session:
        acceptance, _ = await store.accept(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=stream,
            seq=5,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.ACCEPTED
        await session.commit()
    persisted = await store.get_cursor(instance_id, "%0")
    assert persisted is not None and persisted.seq == 5

    # A fresh service (as after a B restart) resolves the persisted cursor
    # and re-issues the since read with the exact persisted stream/seq.
    registry = LiveInstanceRegistry(queue_size=4)
    _, connection = await _register_topology(registry, instance_id)
    service = ObservationService(registry, cursor_store=store)
    submit = asyncio.create_task(
        service.read_pane(
            instance_id,
            PaneReadParams(
                pane_id="%0",
                view=PaneReadView.SINCE,
                cursor=persisted,
            ),
        )
    )
    await asyncio.sleep(0)
    request = PaneCaptureRequestPayload.model_validate((await connection.outbound.get()).payload)
    assert request.stream_id == str(stream)
    assert request.seq == 5
    assert request.pane_incarnation == 1

    # A's ring was reset while B was down: the capture surfaces the explicit
    # gap instead of silently returning stale content.
    result = _capture_result(
        request,
        content="",
        stream_id=str(stream),
        from_seq=5,
        to_seq=5,
        stream_gap=True,
        gap_reason="overwritten",
        result_code="stream_gap",
    )
    assert connection.resolve_capture(request.request_id, result)
    outcome = await submit
    assert outcome.stream_gap is not None
    assert outcome.stream_gap.reason == "overwritten"


@pytest.mark.asyncio
async def test_pane_replacement_rejects_stale_incarnation_before_submit(
    repositories: RepositoryBundle,
) -> None:
    instance_id = uuid4()
    await _register_instance(repositories, instance_id)
    store = ObservationCursorStore(repositories.session_factory)
    stream = uuid4()
    async with repositories.session_factory() as session:
        acceptance, _ = await store.accept_recovered(
            session,
            instance_id=instance_id,
            pane_id="%0",
            stream_id=stream,
            seq=1,
            pane_incarnation=2,
            observed_at=datetime.now(UTC),
        )
        assert acceptance is CursorAcceptance.INCARNATION_CHANGED
        await session.commit()
    registry = LiveInstanceRegistry(queue_size=4)
    _, connection = await _register_topology(registry, instance_id)
    service = ObservationService(registry, cursor_store=store)

    stale = _cursor(instance_id, "%0", incarnation=1, seq=5, stream_id=uuid4())
    with pytest.raises(TermFlowToolError) as caught:
        await service.read_pane(
            instance_id,
            PaneReadParams(pane_id="%0", view=PaneReadView.SINCE, cursor=stale),
        )
    assert caught.value.error_code is TermFlowErrorCode.INCARNATION_CHANGED
    assert connection.outbound.empty()

    # A cursorless read resolves the current incarnation and proceeds.
    submit = asyncio.create_task(
        service.read_pane(instance_id, PaneReadParams(pane_id="%0", view=PaneReadView.VIEWPORT))
    )
    await asyncio.sleep(0)
    request = PaneCaptureRequestPayload.model_validate((await connection.outbound.get()).payload)
    assert request.pane_incarnation == 2
    assert connection.resolve_capture(
        request.request_id, _capture_result(request, pane_incarnation=2)
    )
    assert (await submit).pane_id == "%0"
