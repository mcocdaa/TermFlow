"""Named-key vocabulary and canonical key-sequence encoding (plan §10, task M5.2).

The shared key vocabulary is pinned here so B's early rejection, the wire
model, and A's tmux mapping all agree: an unknown name fails closed on both
sides, and ``canonical_key_bytes`` gives B a position-sensitive digest for
the approval hash (``["enter","space"]`` must differ from
``["space","enter"]``).
"""

from __future__ import annotations

import pytest
from termflow_protocol.keys import (
    MAX_KEY_SEQUENCE_LENGTH,
    NAMED_KEYS,
    canonical_key_bytes,
)


def test_named_keys_is_a_bounded_lowercase_vocabulary() -> None:
    assert isinstance(NAMED_KEYS, frozenset)
    assert all(key == key.lower() for key in NAMED_KEYS)
    assert all(1 <= len(key) <= 16 for key in NAMED_KEYS)
    assert "space" in NAMED_KEYS
    assert "shift-tab" in NAMED_KEYS


def test_named_keys_covers_the_pinned_contract_surface() -> None:
    # The spec pins the vocabulary (plan §10: "named keys use the new typed A
    # input command"). Plain characters are not named keys: agents send text.
    assert {
        "space",
        "tab",
        "enter",
        "backspace",
        "escape",
        "delete",
        "insert",
        "home",
        "end",
        "pageup",
        "pagedown",
        "up",
        "down",
        "left",
        "right",
        "shift-tab",
    } <= NAMED_KEYS
    assert {f"f{index}" for index in range(1, 13)} <= NAMED_KEYS
    assert {f"ctrl-{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"} <= NAMED_KEYS
    assert {f"alt-{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"} <= NAMED_KEYS


def test_plain_characters_are_not_named_keys() -> None:
    # ``send_text`` carries plain characters; the key vocabulary is closed.
    assert "a" not in NAMED_KEYS
    assert "1" not in NAMED_KEYS
    assert "C-c" not in NAMED_KEYS


def test_canonical_key_bytes_is_a_compact_position_sensitive_json_array() -> None:
    assert canonical_key_bytes(("enter", "space")) == b'["enter","space"]'
    assert canonical_key_bytes(["enter", "space"]) == b'["enter","space"]'


def test_canonical_key_bytes_is_position_sensitive() -> None:
    assert canonical_key_bytes(("enter", "space")) != canonical_key_bytes(
        ("space", "enter")
    )
    assert canonical_key_bytes(("ctrl-c", "enter")) != canonical_key_bytes(
        ("enter", "ctrl-c")
    )


def test_canonical_key_bytes_is_deterministic() -> None:
    keys = ("ctrl-a", "f5", "enter")
    assert canonical_key_bytes(keys) == canonical_key_bytes(keys)


def test_max_key_sequence_length_is_bounded() -> None:
    assert MAX_KEY_SEQUENCE_LENGTH == 16
    # Guard against accidental vocabulary growth beyond the wire bound.
    assert len(NAMED_KEYS) >= 50
