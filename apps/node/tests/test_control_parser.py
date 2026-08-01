import pytest
from termflow_node.tmux.control_parser import (
    GenericNotification,
    MalformedControlNotification,
    OutputNotification,
    parse_control_line,
)


def test_output_notification_preserves_arbitrary_bytes() -> None:
    event = parse_control_line(b"%output %7 hi\\015\\012\\033[31m\\134x\n")
    assert event == OutputNotification("%7", b"hi\r\n\x1b[31m\\x")


def test_non_output_notification_remains_structured() -> None:
    event = parse_control_line(b"%window-add @3\n")
    assert event == GenericNotification(name="window-add", arguments=("@3",))


def test_extended_output_ignores_flow_control_metadata() -> None:
    event = parse_control_line(b"%extended-output %2 15 future : hi\\015\\012\n")
    assert event == OutputNotification("%2", b"hi\r\n")


@pytest.mark.parametrize("line", [b"%output %1 bad\\x\n", b"%output %1 bad\\12\n"])
def test_malformed_output_escape_is_rejected(line: bytes) -> None:
    with pytest.raises(MalformedControlNotification):
        parse_control_line(line)
