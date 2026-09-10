import asyncio
from collections import defaultdict
from uuid import uuid4

import pytest
from termflow_node.bridge.input_handler import InputHandler
from termflow_protocol import PaneInputPayload, PaneSnapshot, TopologySnapshot, WindowSnapshot


def make_topology() -> TopologySnapshot:
    panes = [
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
        for index, pane_id in enumerate(("%1", "%2"))
    ]
    return TopologySnapshot(
        session_id="$0",
        session_name="main",
        revision=1,
        windows=[
            WindowSnapshot(
                window_id="@0",
                index=0,
                name="main",
                active=True,
                panes=panes,
            )
        ],
    )


def make_input(pane_id: str, text: str, submit: bool, idempotency_key):
    return PaneInputPayload(
        command_id=uuid4(),
        idempotency_key=idempotency_key,
        pane_id=pane_id,
        text=text,
        submit=submit,
    )


class TmuxSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.active = defaultdict(int)
        self.max_concurrency_by_pane = defaultdict(int)
        self.global_active = 0
        self.global_max_concurrency = 0

    async def send_text(self, pane_id: str, text: str, submit: bool) -> None:
        self.calls.append((pane_id, text, submit))
        self.active[pane_id] += 1
        self.global_active += 1
        self.max_concurrency_by_pane[pane_id] = max(
            self.max_concurrency_by_pane[pane_id], self.active[pane_id]
        )
        self.global_max_concurrency = max(self.global_max_concurrency, self.global_active)
        await asyncio.sleep(0.01)
        self.active[pane_id] -= 1
        self.global_active -= 1


@pytest.mark.asyncio
async def test_duplicate_key_writes_once() -> None:
    spy = TmuxSpy()
    handler = InputHandler(topology_provider=make_topology, sender=spy)
    key = uuid4()
    command = make_input("%1", "继续", True, key)
    first, second = await asyncio.gather(handler.handle(command), handler.handle(command))
    assert first == second
    assert spy.calls == [("%1", "继续", True)]


@pytest.mark.asyncio
async def test_same_pane_serializes_while_other_pane_runs() -> None:
    spy = TmuxSpy()
    handler = InputHandler(topology_provider=make_topology, sender=spy)
    await asyncio.gather(
        handler.handle(make_input("%1", "a", False, uuid4())),
        handler.handle(make_input("%1", "b", False, uuid4())),
        handler.handle(make_input("%2", "c", False, uuid4())),
    )
    assert spy.max_concurrency_by_pane["%1"] == 1
    assert spy.global_max_concurrency >= 2


@pytest.mark.asyncio
async def test_missing_pane_returns_approved_error_without_tmux_call() -> None:
    spy = TmuxSpy()
    handler = InputHandler(topology_provider=make_topology, sender=spy)
    result = await handler.handle(make_input("%99", "private body", False, uuid4()))
    assert result.ok is False
    assert result.error_code == "pane_not_found"
    assert spy.calls == []
