"""Canonical key-sequence encoding (plan §10, task M5.2).

``canonical_key_bytes`` gives B a position-sensitive digest for the approval
hash (``["enter","space"]`` must differ from ``["space","enter"]``).
"""

from __future__ import annotations

from termflow_protocol.keys import (
    canonical_key_bytes,
)


def test_canonical_key_bytes_is_compact_and_position_sensitive() -> None:
    assert canonical_key_bytes(("enter", "space")) == b'["enter","space"]'
    assert canonical_key_bytes(["enter", "space"]) == b'["enter","space"]'
    assert canonical_key_bytes(("enter", "space")) != canonical_key_bytes(("space", "enter"))
    assert canonical_key_bytes(("ctrl-c", "enter")) != canonical_key_bytes(("enter", "ctrl-c"))
