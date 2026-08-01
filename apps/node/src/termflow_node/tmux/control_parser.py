"""Byte-preserving parser for tmux control-mode notifications."""

from __future__ import annotations

import shlex
from dataclasses import dataclass


class MalformedControlNotification(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OutputNotification:
    pane_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class CommandBoundary:
    kind: str
    timestamp: int
    command_number: int
    flags: int


@dataclass(frozen=True, slots=True)
class PauseNotification:
    pane_id: str
    paused: bool


@dataclass(frozen=True, slots=True)
class ExitNotification:
    reason: str | None


@dataclass(frozen=True, slots=True)
class GenericNotification:
    name: str
    arguments: tuple[str, ...]


ControlNotification = (
    OutputNotification
    | CommandBoundary
    | PauseNotification
    | ExitNotification
    | GenericNotification
)


def _unescape_output(payload: bytes) -> bytes:
    decoded = bytearray()
    cursor = 0
    while cursor < len(payload):
        byte = payload[cursor]
        if byte != 0x5C:
            decoded.append(byte)
            cursor += 1
            continue
        escape = payload[cursor + 1 : cursor + 4]
        if len(escape) != 3 or any(digit < 0x30 or digit > 0x37 for digit in escape):
            raise MalformedControlNotification("output contains an invalid octal escape")
        decoded.append(int(escape, 8))
        cursor += 4
    return bytes(decoded)


def parse_control_line(line: bytes) -> ControlNotification:
    body = line.removesuffix(b"\n").removesuffix(b"\r")
    if not body.startswith(b"%"):
        raise MalformedControlNotification("control notification must start with %")
    if body.startswith(b"%output "):
        fields = body.split(b" ", 2)
        if len(fields) != 3:
            raise MalformedControlNotification("output notification is incomplete")
        try:
            pane_id = fields[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise MalformedControlNotification("Pane ID must be ASCII") from exc
        if not pane_id.startswith("%") or not pane_id[1:].isdigit():
            raise MalformedControlNotification("output Pane ID is invalid")
        return OutputNotification(pane_id, _unescape_output(fields[2]))
    if body.startswith(b"%extended-output "):
        header, separator, payload = body.partition(b" : ")
        header_fields = header.split(b" ")
        if not separator or len(header_fields) < 3:
            raise MalformedControlNotification("extended output notification is incomplete")
        try:
            pane_id = header_fields[1].decode("ascii")
            int(header_fields[2])
        except (UnicodeDecodeError, ValueError) as exc:
            raise MalformedControlNotification("extended output header is invalid") from exc
        if not pane_id.startswith("%") or not pane_id[1:].isdigit():
            raise MalformedControlNotification("extended output Pane ID is invalid")
        return OutputNotification(pane_id, _unescape_output(payload))

    try:
        words = shlex.split(body.decode("utf-8", errors="surrogateescape"))
    except ValueError as exc:
        raise MalformedControlNotification("invalid notification quoting") from exc
    if not words:
        raise MalformedControlNotification("empty notification")
    name = words[0][1:]
    arguments = tuple(words[1:])
    if name in {"begin", "end", "error"}:
        if len(arguments) != 3:
            raise MalformedControlNotification(f"%{name} requires three fields")
        try:
            return CommandBoundary(name, *(int(value) for value in arguments))
        except ValueError as exc:
            raise MalformedControlNotification(f"%{name} fields must be integers") from exc
    if name in {"pause", "continue"}:
        if len(arguments) != 1:
            raise MalformedControlNotification(f"%{name} requires one Pane ID")
        return PauseNotification(arguments[0], paused=name == "pause")
    if name == "exit":
        return ExitNotification(" ".join(arguments) if arguments else None)
    return GenericNotification(name, arguments)
