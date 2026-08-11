"""Bounded rendered pane capture helper (plan §9.2/§9.3)."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest
from termflow_node.tmux.capture import (
    CaptureCommandError,
    capture_argv,
    capture_pane_bounded,
    truncate_tail,
)

SOCKET = Path("/tmp/termflow-test.sock")


def test_capture_argv_viewport_has_no_range_or_escape_flags() -> None:
    argv = capture_argv(SOCKET, "%1")
    assert argv == ["tmux", "-S", str(SOCKET), "capture-pane", "-p", "-t", "%1"]
    assert "-e" not in argv
    assert "-J" not in argv
    assert "-E" not in argv


def test_capture_argv_full_history() -> None:
    argv = capture_argv(SOCKET, "%1", full_history=True)
    assert argv == [
        "tmux",
        "-S",
        str(SOCKET),
        "capture-pane",
        "-p",
        "-S",
        "-",
        "-t",
        "%1",
    ]


def test_capture_argv_tail_lines_uses_negative_start() -> None:
    argv = capture_argv(SOCKET, "%1", tail_lines=42)
    assert argv == [
        "tmux",
        "-S",
        str(SOCKET),
        "capture-pane",
        "-p",
        "-S",
        "-42",
        "-t",
        "%1",
    ]


def test_capture_argv_start_end_range() -> None:
    argv = capture_argv(SOCKET, "%1", start_line=3, end_line=7)
    assert argv == [
        "tmux",
        "-S",
        str(SOCKET),
        "capture-pane",
        "-p",
        "-S",
        "3",
        "-E",
        "7",
        "-t",
        "%1",
    ]


def test_capture_argv_start_only_range() -> None:
    argv = capture_argv(SOCKET, "%1", start_line=3)
    assert argv == [
        "tmux",
        "-S",
        str(SOCKET),
        "capture-pane",
        "-p",
        "-S",
        "3",
        "-t",
        "%1",
    ]


def test_capture_argv_join_wrapped_adds_join_flag() -> None:
    argv = capture_argv(SOCKET, "%1", join_wrapped=True)
    assert argv == [
        "tmux",
        "-S",
        str(SOCKET),
        "capture-pane",
        "-p",
        "-J",
        "-t",
        "%1",
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tail_lines": 5, "start_line": 1},
        {"tail_lines": 5, "end_line": 9},
        {"full_history": True, "tail_lines": 5},
        {"full_history": True, "start_line": 1},
        {"full_history": True, "end_line": 9},
    ],
)
def test_capture_argv_rejects_contradictory_selectors(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        capture_argv(SOCKET, "%1", **kwargs)


def test_capture_pane_bounded_truncates_keeping_newest_tail() -> None:
    def run(argv: list[str]) -> CompletedProcess[bytes]:
        return CompletedProcess(argv, 0, stdout=b"a" * 100, stderr=b"")

    rendered = capture_pane_bounded(SOCKET, "%1", max_bytes=40, run=run)
    assert rendered.content == b"a" * 40
    assert rendered.truncated is True


def test_capture_pane_bounded_within_limit_is_not_truncated() -> None:
    def run(argv: list[str]) -> CompletedProcess[bytes]:
        return CompletedProcess(argv, 0, stdout=b"short capture", stderr=b"")

    rendered = capture_pane_bounded(SOCKET, "%1", max_bytes=1024, run=run)
    assert rendered.content == b"short capture"
    assert rendered.truncated is False


def test_capture_pane_bounded_raises_on_tmux_failure() -> None:
    def run(argv: list[str]) -> CompletedProcess[bytes]:
        return CompletedProcess(argv, 2, stdout=b"", stderr=b"no server")

    with pytest.raises(CaptureCommandError) as excinfo:
        capture_pane_bounded(SOCKET, "%1", max_bytes=10, run=run)
    assert excinfo.value.exit_code == 2
    assert "%1" in str(excinfo.value.argv)


def test_truncate_tail_never_splits_a_multibyte_character() -> None:
    raw = "héllo wörld".encode()
    truncated, flag = truncate_tail(raw, 8)
    assert flag is True
    assert truncated == raw[-8:]
    decoded = truncated.decode("utf-8", errors="replace")
    assert "\ufffd" not in decoded
    assert decoded == "o wörld"
