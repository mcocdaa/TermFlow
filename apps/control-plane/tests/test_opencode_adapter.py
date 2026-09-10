"""Request-contract tests for the Profile-bound OpenCode adapter."""

from __future__ import annotations

import json
from inspect import Parameter, signature
from typing import Any

import httpx
import pytest
from termflow_control_plane.plugins.agent_broker.agent.opencode import (
    OpenCodeAdapter,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendConversationRef,
    BackendOutcome,
    BackendTurnPart,
    BackendTurnRequest,
    ProviderRef,
)
from termflow_protocol.agent import AgentInputKind

BASE_URL = "https://opencode.test"
DIRECTORY = "/srv/termflow/workspace-1"
BACKEND_VERSION = "1.18.18"
RUNTIME_ID = "runtime://opencode-1"


def _conversation_ref() -> BackendConversationRef:
    return BackendConversationRef(
        backend_kind="opencode",
        backend_version=BACKEND_VERSION,
        runtime_id=RUNTIME_ID,
        binding_capability_epoch=3,
        provider_ref=ProviderRef("ses_opencode_1"),
    )


def _turn_request(ref: BackendConversationRef) -> BackendTurnRequest:
    return BackendTurnRequest(
        conversation_ref=ref,
        correlation_id="correlation-1",
        idempotency_key="idempotency-1",
        parts=(
            BackendTurnPart(
                kind=AgentInputKind.USER_MESSAGE,
                text="hello",
            ),
        ),
    )


async def test_submit_sends_selected_provider_and_model() -> None:
    recorded: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded["request"] = request
        recorded["json"] = json.loads(request.content)
        return httpx.Response(status_code=204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenCodeAdapter(
        base_url=BASE_URL,
        directory=DIRECTORY,
        backend_version=BACKEND_VERSION,
        provider_id="deepseek",
        model_id="deepseek-v4-flash",
        client=client,
        runtime_id=RUNTIME_ID,
        binding_capability_epoch=3,
    )
    ref = _conversation_ref()

    result = await adapter.submit(ref, _turn_request(ref))

    request = recorded["request"]
    assert isinstance(request, httpx.Request)
    assert request.url.path == "/session/ses_opencode_1/prompt_async"
    assert dict(request.url.params) == {"directory": DIRECTORY}
    assert recorded["json"] == {
        "parts": [{"type": "text", "text": "hello"}],
        "model": {
            "providerID": "deepseek",
            "modelID": "deepseek-v4-flash",
        },
    }
    assert not ({"endpoint", "credential", "key"} & recorded["json"].keys())
    assert result.outcome is BackendOutcome.CONFIRMED
    assert result.retry_safe is False

    await adapter.close()


def test_provider_and_model_are_required_constructor_arguments() -> None:
    parameters = signature(OpenCodeAdapter).parameters

    assert parameters["provider_id"].kind is Parameter.KEYWORD_ONLY
    assert parameters["provider_id"].default is Parameter.empty
    assert parameters["model_id"].kind is Parameter.KEYWORD_ONLY
    assert parameters["model_id"].default is Parameter.empty


@pytest.mark.parametrize(
    ("provider_id", "model_id", "field_name"),
    [
        ("", "deepseek-v4-flash", "provider_id"),
        ("   ", "deepseek-v4-flash", "provider_id"),
        (" deepseek", "deepseek-v4-flash", "provider_id"),
        ("deepseek ", "deepseek-v4-flash", "provider_id"),
        ("deepseek", "", "model_id"),
        ("deepseek", "   ", "model_id"),
        ("deepseek", " deepseek-v4-flash", "model_id"),
        ("deepseek", "deepseek-v4-flash ", "model_id"),
    ],
)
def test_provider_and_model_must_be_normalized_non_empty(
    provider_id: str,
    model_id: str,
    field_name: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=rf"^{field_name} must be a normalized non-empty string$",
    ):
        OpenCodeAdapter(
            base_url=BASE_URL,
            directory=DIRECTORY,
            backend_version=BACKEND_VERSION,
            provider_id=provider_id,
            model_id=model_id,
            client=object(),
        )


@pytest.mark.parametrize(
    ("provider_id", "model_id", "field_name", "max_length"),
    [
        ("p" * 65, "deepseek-v4-flash", "provider_id", 64),
        ("deepseek", "m" * 129, "model_id", 128),
    ],
)
def test_provider_and_model_enforce_profile_identifier_limits(
    provider_id: str,
    model_id: str,
    field_name: str,
    max_length: int,
) -> None:
    with pytest.raises(
        ValueError,
        match=rf"^{field_name} must not exceed {max_length} characters$",
    ):
        OpenCodeAdapter(
            base_url=BASE_URL,
            directory=DIRECTORY,
            backend_version=BACKEND_VERSION,
            provider_id=provider_id,
            model_id=model_id,
            client=object(),
        )
