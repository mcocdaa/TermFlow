"""Protocol key-name to tmux key-name mapping (plan §10, task M5.2).

The shared vocabulary is pinned in ``termflow_protocol.keys.NAMED_KEYS``;
A maps every protocol name to the tmux key name tmux accepts in
``send-keys -t <pane> <name...>``.  The mapping is exact and total over
``NAMED_KEYS`` (a fixture asserts coverage), and unknown names fail closed:
``input_handler.handle_keys`` never passes an unmapped string to tmux.
"""

from __future__ import annotations

from termflow_protocol.keys import NAMED_KEYS

#: Protocol name -> tmux key name (tmux 3.2+ key-name vocabulary).
KEY_NAME_MAP: dict[str, str] = {
    "space": "Space",
    "tab": "Tab",
    "enter": "Enter",
    "backspace": "BSpace",
    "escape": "Escape",
    "delete": "DC",
    "insert": "IC",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "shift-tab": "BTab",
}
KEY_NAME_MAP.update({f"f{index}": f"F{index}" for index in range(1, 13)})
KEY_NAME_MAP.update({f"ctrl-{letter}": f"C-{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"})
KEY_NAME_MAP.update({f"alt-{letter}": f"M-{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"})

#: The protocol names this mapping covers (must equal NAMED_KEYS exactly).
MAPPED_KEYS: frozenset[str] = frozenset(KEY_NAME_MAP)

assert MAPPED_KEYS == NAMED_KEYS, "the tmux key mapping must cover NAMED_KEYS exactly"


def tmux_key_names(keys: tuple[str, ...]) -> tuple[str, ...]:
    """Map a validated protocol key sequence to tmux key names.

    Raises :class:`KeyError` for names outside the pinned vocabulary; the
    caller fails closed with an ``invalid_key`` receipt.
    """
    return tuple(KEY_NAME_MAP[key] for key in keys)
