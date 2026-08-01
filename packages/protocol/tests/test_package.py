from termflow_protocol import PROTOCOL_VERSION


def test_protocol_version_is_one() -> None:
    assert PROTOCOL_VERSION == 1
