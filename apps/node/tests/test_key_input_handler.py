"""Typed key input handler tests (plan §10, task M5.2).

``InputHandler.handle_keys`` shares the per-pane lock and the idempotency
results with text input (a pane's writes are serialized across both kinds),
maps protocol key names through the pinned tmux vocabulary, and fails
closed with ``invalid_key`` for names outside it - an unmapped string never
reaches tmux.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from uuid import uuid4

import pytest
from termflow_node.bridge.input_handler import InputHandler
from termflow_node.bridge.key_names import KEY_NAME_MAP, tmux_key_names
from termflow_protocol import (
    PaneKeyInputPayload,
    PaneSnapshot,
    TopologySnapshot,
    WindowSnapshot,
)
from termflow_protocol.keys import NAMED_KEYS


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


def make_keys(pane_id: str, keys: tuple[str, ...], idempotency_key):
    return PaneKeyInputPayload(
        command_id=uuid4(),
        idempotency_key=idempotency_key,
        pane_id=pane_id,
        keys=keys,
    )


class TmuxSpy:
    def __init__(self) -> None:
        self.text_calls: list[tuple] = []
        self.key_calls: list[tuple] = []
        self.active = defaultdict(int)
        self.max_concurrency_by_pane = defaultdict(int)

    async def send_text(self, pane_id: str, text: str, submit: bool) -> None:
        self.text_calls.append((pane_id, text, submit))
        await self._track(pane_id)

    async def send_keys(self, pane_id: str, keys: tuple[str, ...]) -> None:
        self.key_calls.append((pane_id, keys))
        await self._track(pane_id)

    async def _track(self, pane_id: str) -> None:
        self.active[pane_id] += 1
        self.max_concurrency_by_pane[pane_id] = max(
            self.max_concurrency_by_pane[pane_id], self.active[pane_id]
        )
        await asyncio.sleep(0.01)
        self.active[pane_id] -= 1


@pytest.mark.asyncio
async def test_named_keys_map_to_tmux_names() -> None:
    assert tmux_key_names(("ctrl-c", "enter", "f5", "shift-tab")) == (
        "C-c",
        "Enter",
        "F5",
        "BTab",
    )


@pytest.mark.asyncio
async def test_mapping_covers_the_named_keys_vocabulary_exactly() -> None:
    # Anti-drift fixture: the node mapping must cover NAMED_KEYS in full so
    # no protocol name can ever reach tmux unmapped (spec §2, risk 4).
    assert set(KEY_NAME_MAP) == set(NAMED_KEYS)
    # And every mapped value is a plausible tmux key name.
    assert all(value for value in KEY_NAME_MAP.values())


@pytest.mark.asyncio
async def test_handle_keys_sends_mapped_sequence() -> None:
    spy = TmuxSpy()
    handler = InputHandler(topology_provider=make_topology, sender=spy)
    command = make_keys("%1", ("ctrl-a", "enter"), uuid4())
    result = await handler.handle_keys(command)
    assert result.ok is True
    assert result.error_code is None
    assert spy.key_calls == [("%1", ("C-a", "Enter"))]
    assert spy.text_calls == []


@pytest.mark.asyncio
async def test_unknown_key_name_fails_closed_with_invalid_key() -> None:
    spy = TmuxSpy()
    handler = InputHandler(topology_provider=make_topology, sender=spy)
    # The wire model already rejects unknown names; this exercises the
    # handler-level defense in depth (an in-process bypass of validation
    # must still fail closed with invalid_key, never reach tmux).
    command = PaneKeyInputPayload.model_construct(
        command_id=uuid4(),
        idempotency_key=uuid4(),
        pane_id="%1",
        keys=("ctrl-c", "not-a-key"),
    )
    result = await handler.handle_keys(command)
    assert result.ok is False
    assert result.error_code == "invalid_key"
    assert spy.key_calls == []


@pytest.mark.asyncio
async def test_missing_pane_returns_pane_not_found_without_tmux_call() -> None:
    spy = TmuxSpy()
    handler = InputHandler(topology_provider=make_topology, sender=spy)
    result = await handler.handle_keys(make_keys("%99", ("enter",), uuid4()))
    assert result.ok is False
    assert result.error_code == "pane_not_found"
    assert spy.key_calls == []


@pytest.mark.asyncio
async def test_text_and_keys_on_same_pane_share_the_lock() -> None:
    spy = TmuxSpy()
    handler = InputHandler(topology_provider=make_topology, sender=spy)
    from termflow_protocol import PaneInputPayload

    text = PaneInputPayload(
        command_id=uuid4(),
        idempotency_key=uuid4(),
        pane_id="%1",
        text="make test",
        submit=True,
    )
    keys = make_keys("%1", ("enter",), uuid4())
    await asyncio.gather(handler.handle(text), handler.handle_keys(keys))
    # The same pane serialized both kinds of writes.
    assert spy.max_concurrency_by_pane["%1"] == 1
    assert len(spy.text_calls) == 1
    assert len(spy.key_calls) == 1


@pytest.mark.asyncio
async def test_duplicate_key_command_is_idempotent() -> None:
    spy = TmuxSpy()
    handler = InputHandler(topology_provider=make_topology, sender=spy)
    key = uuid4()
    command = make_keys("%1", ("enter",), key)
    first, second = await asyncio.gather(
        handler.handle_keys(command), handler.handle_keys(command)
    )
    assert first.command_id == second.command_id
    assert first.ok is True
    assert spy.key_calls == [("%1", ("Enter",))]
