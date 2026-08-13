"""Pinned named-key vocabulary and canonical key-sequence encoding (plan §10, M5.2).

Typed key input never travels as arbitrary strings: B, the wire protocol, and
A all share this closed vocabulary.  ``NAMED_KEYS`` is the bounded set of
lowercase protocol key names (``space``/``enter``/``ctrl-a``/...), and
:func:`canonical_key_bytes` produces the position-sensitive byte digest B
binds into the approval hash (a JSON array, never sorted, so
``["enter","space"]`` differs from ``["space","enter"]``).

The mapping from these protocol names to tmux key names is pinned on the A
side (``termflow_node.bridge.key_names``); a fixture asserts that mapping
covers this vocabulary in full so neither side can drift.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

#: The longest key sequence one send_keys call may carry (wire bound).
MAX_KEY_SEQUENCE_LENGTH = 16

_NAMED_KEYS: set[str] = {
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
}
_NAMED_KEYS.update(f"f{index}" for index in range(1, 13))
_NAMED_KEYS.update(f"ctrl-{letter}" for letter in "abcdefghijklmnopqrstuvwxyz")
_NAMED_KEYS.update(f"alt-{letter}" for letter in "abcdefghijklmnopqrstuvwxyz")

#: The closed vocabulary of named keys agents may send (lowercase, exact).
NAMED_KEYS: frozenset[str] = frozenset(_NAMED_KEYS)


def canonical_key_bytes(keys: Sequence[str]) -> bytes:
    """Position-sensitive canonical encoding of one key sequence.

    The encoding is a compact JSON array in call order (no ``sort_keys``),
    so the exact sequence an approval authorized is exactly the sequence the
    hash covers; reordering any two keys changes the digest.
    """
    return json.dumps(list(keys), separators=(",", ":")).encode("utf-8")
