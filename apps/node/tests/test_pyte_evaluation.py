"""Bounded pyte parser comparison fixture (plan §19 M2, §22.2).

Records the pyte adoption decision for the Observation Service *without*
making pyte a runtime dependency (M2.4 must not touch pyproject.toml or
uv.lock): whenever pyte is not importable in the environment, the whole
module is recorded as SKIP and the evaluation results and decision criteria
live in ``fixtures/pyte_evaluation.md``.

When pyte IS importable (a dev sandbox), the bounded comparison runs:

- split escape sequences are fed across ``Stream.feed`` chunk boundaries and
  the parser state must survive (SGR attributes and cursor motion must not
  depend on receiving a sequence inside a single chunk);
- rendering semantics are checked against the existing A-side approach:
  TermFlow serves tmux-rendered captures (``capture-pane`` without ``-e``)
  and raw ring chunks for ``since`` reads without re-rendering, so pyte
  would be an *added* renderer that must match tmux output, not a
  replacement for an existing screen-state parser (there is none today).

The fixture record (version, license, environment result, adoption decision)
is the deliverable; the assertions below keep that decision executable.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

import pytest

#: pyte facts verified from PyPI on 2026-08-12
#: (https://pypi.org/project/pyte/); see fixtures/pyte_evaluation.md.
PYTE_VERSION = "0.8.2"
PYTE_LICENSE = "LGPL-3.0"

pyte: Any = pytest.importorskip(
    "pyte",
    reason=(
        "pyte is not importable in the TermFlow environment: "
        "`UV_DEFAULT_INDEX=https://pypi.org/simple uv run --no-sync "
        "python -c 'import pyte'` fails with ModuleNotFoundError. "
        f"pyte {PYTE_VERSION} ({PYTE_LICENSE}) is not a project dependency, "
        "and M2.4 must not modify pyproject.toml/uv.lock. The evaluation is "
        "recorded as SKIP in apps/node/tests/fixtures/pyte_evaluation.md "
        "with the decision criteria (license fit, split-sequence handling, "
        "rendering semantics) and the adoption decision."
    ),
)


def _screen(rows: int = 24, cols: int = 80):
    # pyte's constructor is Screen(columns, lines); the helper takes the
    # conventional (rows, cols) order and translates.
    return pyte.Screen(cols, rows)


def _char(screen, row: int, col: int) -> Any:
    # ``screen.buffer`` maps row -> sparse column -> Char.
    return screen.buffer[row][col]


def test_installed_pyte_version_matches_fixture_record() -> None:
    installed = importlib.metadata.version("pyte")
    assert installed == PYTE_VERSION, (
        f"fixture records pyte {PYTE_VERSION} but {installed} is installed; "
        "re-run the evaluation and update fixtures/pyte_evaluation.md"
    )


def test_split_escape_sequence_at_every_byte_keeps_sgr_state() -> None:
    screen = _screen()
    stream = pyte.Stream(screen)
    sequence = b"\x1b[31mred"
    for index in range(len(sequence)):
        stream.feed(sequence[index : index + 1].decode("latin-1"))
    assert screen.display[0].strip() == "red"
    first = _char(screen, 0, 0)
    assert first.data == "r"
    assert first.fg == "red"


def test_split_csi_cursor_sequence_across_chunks_survives() -> None:
    screen = _screen()
    stream = pyte.Stream(screen)
    for chunk in ("\x1b[10", ";", "10", "H", "X"):
        stream.feed(chunk)
    assert (screen.cursor.y, screen.cursor.x) == (9, 10)
    assert _char(screen, 9, 9).data == "X"


def test_chunk_boundary_between_parameter_and_final_byte_keeps_state() -> None:
    screen = _screen()
    stream = pyte.Stream(screen)
    stream.feed("\x1b[3")
    stream.feed("1m")
    stream.feed("ok")
    assert screen.display[0].strip() == "ok"
    assert _char(screen, 0, 0).fg == "red"


def test_rendering_semantics_are_checked_against_a_side_approach() -> None:
    """pyte adds a renderer; it does not replace the tmux-rendered path.

    The A side serves tmux-rendered captures (no escape sequences) and raw
    ring chunks for ``since`` reads without re-rendering (see
    ``termflow_node/tmux/capture.py``). This bounded check proves pyte's own
    screen semantics on a small sample; a full tmux-equivalence comparison
    (real pane, real output) is an adoption gate recorded in the fixture.
    """
    screen = _screen(rows=3, cols=5)
    stream = pyte.Stream(screen)
    stream.feed("ab\r\nc")
    assert screen.display[0] == "ab   "
    assert screen.display[1] == "c    "
