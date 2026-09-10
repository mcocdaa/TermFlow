from uuid import uuid4

import pytest
from pydantic import ValidationError
from termflow_protocol import (
    BridgeHelloPayload,
    WireMessage,
    parse_payload,
)


def test_bridge_hello_capability_flags_round_trip() -> None:
    hello = BridgeHelloPayload(name="term", bounded_capture=True, typed_keys=False)
    parsed = parse_payload("bridge.hello", hello.model_dump(mode="json"))
    assert parsed == hello
    assert parsed.bounded_capture is True
    assert parsed.typed_keys is False


def test_unknown_protocol_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WireMessage(
            protocol_version=2,
            message_id=uuid4(),
            type="bridge.heartbeat",
            instance_id=uuid4(),
            payload={},
        )
