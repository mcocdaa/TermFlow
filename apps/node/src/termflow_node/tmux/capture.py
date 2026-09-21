"""Bounded, rendered tmux pane capture for ordinary agent text reads.

Semantics (plan §9.2/§9.3):

- ``viewport``: ``capture-pane -p`` with no ``-S``/``-E`` flags (visible screen
  only).
- ``history``: ``capture-pane -p -S -`` (full history) or a bounded range:
  ``-S <start_line>`` / ``-E <end_line>`` (absolute lines from the top of the
  history) or ``-S -<tail_lines>`` (newest N lines).
- ``join_wrapped`` adds ``-J`` so tmux joins wrapped lines into logical lines.
- No ``-e`` flag: output is rendered by tmux without escape sequences. Raw
  stream chunks (which may contain control sequences) are served only by the
  ``since`` path, which reads the buffered ring directly and never re-renders.
- ``max_bytes`` is the hard byte bound; the newest tail is kept and the result
  reports whether it truncated.

The capture helper never touches the live output ring; snapshots produced
here are served out-of-band to the requester and are never appended into the
ring as though they were new terminal output (§9.2).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .runner import tmux_subprocess_environment


class CaptureCommandError(RuntimeError):
    """tmux ``capture-pane`` exited non-zero."""

    def __init__(self, argv: list[str], exit_code: int) -> None:
        self.argv = tuple(argv)
        self.exit_code = exit_code
        super().__init__(f"tmux capture-pane failed with exit code {exit_code}: {self.argv!r}")


class CaptureRun(Protocol):
    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]: ...


def _capture_run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        env=tmux_subprocess_environment(),
    )


def capture_argv(
    socket_path: Path,
    pane_id: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    tail_lines: int | None = None,
    join_wrapped: bool = False,
    full_history: bool = False,
) -> list[str]:
    """Build a rendered (no ``-e``) bounded ``capture-pane`` argv.

    Selectors are mutually exclusive: ``tail_lines`` and ``full_history``
    cannot be combined with absolute line ranges.
    """
    if tail_lines is not None and (start_line is not None or end_line is not None):
        raise ValueError("tail_lines cannot be combined with start_line or end_line")
    if full_history and (start_line is not None or end_line is not None or tail_lines is not None):
        raise ValueError("full_history cannot be combined with a bounded range")
    argv = ["tmux", "-S", str(socket_path), "capture-pane", "-p"]
    if join_wrapped:
        argv.append("-J")
    if full_history:
        argv += ["-S", "-"]
    elif tail_lines is not None:
        argv += ["-S", f"-{tail_lines}"]
    else:
        if start_line is not None:
            argv += ["-S", str(start_line)]
        if end_line is not None:
            argv += ["-E", str(end_line)]
    argv += ["-t", pane_id]
    return argv


def truncate_tail(raw: bytes, max_bytes: int) -> tuple[bytes, bool]:
    """Keep the newest ``max_bytes`` bytes; reports whether it truncated.

    Truncation is byte-level; callers decode with ``errors="replace"`` so a
    multibyte character split by the cut never raises.
    """
    if len(raw) <= max_bytes:
        return raw, False
    return raw[-max_bytes:], True


@dataclass(frozen=True, slots=True)
class RenderedCapture:
    content: bytes
    truncated: bool


def capture_pane_bounded(
    socket_path: Path,
    pane_id: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    tail_lines: int | None = None,
    join_wrapped: bool = False,
    full_history: bool = False,
    max_bytes: int,
    run: CaptureRun = _capture_run,
) -> RenderedCapture:
    """Run one bounded, rendered tmux capture and enforce ``max_bytes``."""
    argv = capture_argv(
        socket_path,
        pane_id,
        start_line=start_line,
        end_line=end_line,
        tail_lines=tail_lines,
        join_wrapped=join_wrapped,
        full_history=full_history,
    )
    result = run(argv)
    if result.returncode != 0:
        raise CaptureCommandError(argv, result.returncode)
    content, truncated = truncate_tail(result.stdout, max_bytes)
    return RenderedCapture(content=content, truncated=truncated)
