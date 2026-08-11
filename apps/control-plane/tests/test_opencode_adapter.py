"""OpenCode adapter tests (M4.1 non-streaming core).

The adapter under test implements the non-streaming parts of
:class:`AgentBackend` against the pinned OpenCode HTTP contract
(``tests/fixtures/opencode/opencode-pin.md`` and the captured OpenAPI spec in
the same fixture directory).  These tests drive the adapter through an
``httpx.MockTransport`` and freeze the pinned endpoint paths, the ``directory``
persistence rule (pin §3), the outcome mapping, and the no-exactly-once stance
(pin §5/§6.1).  httpx is a dev-group dependency and is imported here only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from termflow_control_plane.plugins.agent_broker.agent.backend import (
    AgentBackend,
    AgentBackendCapabilities,
    CancelScope,
    ConcurrencyMode,
    ContextMode,
    ReplayMode,
    RuntimeIsolation,
    StructuredPartFidelity,
    SubmitMode,
    ToolCallIdentity,
)
from termflow_control_plane.plugins.agent_broker.agent.opencode import (
    BACKEND_KIND,
    RECONCILE_MESSAGE_LIMIT,
    OpenCodeAdapter,
)
from termflow_control_plane.plugins.agent_broker.agent.turns import (
    BackendCancelRequest,
    BackendConversationRef,
    BackendInteraction,
    BackendInteractionKind,
    BackendOperationResult,
    BackendOutcome,
    BackendSubmitResult,
    BackendTurnPart,
    BackendTurnRequest,
    CreateBackendConversation,
    TurnPartTrust,
)
from termflow_protocol.agent import AgentInputKind, ApprovalDecision, BackendRuntimeState

BASE_URL = "https://opencode.test"
DIRECTORY = "/srv/termflow/workspace-1"
BACKEND_VERSION = "0.1.0"
RUNTIME_ID = "runtime://opencode-1"
CAPABILITY_EPOCH = 3
PROVIDER_SESSION_ID = "ses_opencode_1"


def _ref(**overrides: Any) -> BackendConversationRef:
    values: dict[str, Any] = {
        "backend_kind": BACKEND_KIND,
        "backend_version": BACKEND_VERSION,
        "runtime_id": RUNTIME_ID,
        "binding_capability_epoch": CAPABILITY_EPOCH,
        "provider_ref": PROVIDER_SESSION_ID,
    }
    values.update(overrides)
    return BackendConversationRef(**values)


def _turn_request(**overrides: Any) -> BackendTurnRequest:
    values: dict[str, Any] = {
        "conversation_ref": _ref(),
        "correlation_id": "corr-1",
        "idempotency_key": "idem-1",
        "parts": (
            BackendTurnPart(
                kind=AgentInputKind.USER_MESSAGE,
                text="please fix the build",
                trust=TurnPartTrust.TRUSTED,
            ),
        ),
    }
    values.update(overrides)
    return BackendTurnRequest(**values)


def _cancel_request(**overrides: Any) -> BackendCancelRequest:
    values: dict[str, Any] = {
        "conversation_ref": _ref(),
        "correlation_id": "corr-cancel",
        "reason": "test",
    }
    values.update(overrides)
    return BackendCancelRequest(**values)


def _interaction(**overrides: Any) -> BackendInteraction:
    values: dict[str, Any] = {
        "conversation_ref": _ref(),
        "kind": BackendInteractionKind.PERMISSION_RESOLVE,
        "permission_ref": "per_opencode_1",
        "decision": ApprovalDecision.APPROVED,
    }
    values.update(overrides)
    return BackendInteraction(**values)


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[OpenCodeAdapter, list[httpx.Request]]:
    """Build an adapter over a recording MockTransport handler."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    adapter = OpenCodeAdapter(
        base_url=BASE_URL,
        directory=DIRECTORY,
        backend_version=BACKEND_VERSION,
        client=client,
        runtime_id=RUNTIME_ID,
        binding_capability_epoch=CAPABILITY_EPOCH,
    )
    return adapter, requests


class TestCapabilities:
    async def test_matches_pinned_capability_matrix(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(204))
        caps = await adapter.capabilities()
        assert isinstance(caps, AgentBackendCapabilities)
        assert caps.streaming_output is False  # SSE lands in a later milestone
        assert caps.context_mode is ContextMode.LOST_ON_RESTART
        assert caps.submit_mode is SubmitMode.NON_IDEMPOTENT
        assert caps.cancel_scope is CancelScope.CONVERSATION
        assert caps.replay_mode is ReplayMode.NONE
        assert caps.concurrency_mode is ConcurrencyMode.SERIALIZED
        assert caps.tool_call_identity is ToolCallIdentity.BINDING
        assert caps.runtime_isolation is RuntimeIsolation.BINDING
        assert caps.accepted_input_kinds == (
            AgentInputKind.USER_MESSAGE,
            AgentInputKind.WATCH_TRIGGERED,
            AgentInputKind.TIMER_TRIGGERED,
            AgentInputKind.SYSTEM_NOTIFICATION,
        )
        assert caps.structured_part_fidelity is StructuredPartFidelity.NONE
        assert caps.tool_activity_events is False
        assert caps.permission_events is False
        assert caps.usage_cost_reporting is False
        assert caps.explicit_run_boundaries is True

    async def test_adapter_satisfies_backend_protocol(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(204))
        assert isinstance(adapter, AgentBackend)


class TestCreateConversation:
    def _session_response(self) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": PROVIDER_SESSION_ID,
                "slug": "slug",
                "projectID": "proj_1",
                "directory": DIRECTORY,
                "title": "Fix the build",
                "version": "0.1.0",
                "time": {"created": 1, "updated": 1},
            },
        )

    async def test_posts_session_with_pinned_body_and_directory(self) -> None:
        adapter, requests = _adapter(lambda request: self._session_response())
        ref = await adapter.create_conversation(
            CreateBackendConversation(
                display_title="Fix the build",
                workspace_alias="workspace-1",
                parent_ref=_ref(),
            )
        )
        request = requests[0]
        assert request.method == "POST"
        assert request.url.path == "/session"
        assert request.url.params["directory"] == DIRECTORY
        body = json.loads(request.content)
        assert body == {"title": "Fix the build", "parentID": PROVIDER_SESSION_ID}
        assert ref.backend_kind == BACKEND_KIND
        assert ref.backend_version == BACKEND_VERSION
        assert ref.runtime_id == RUNTIME_ID
        assert ref.binding_capability_epoch == CAPABILITY_EPOCH
        # The provider session id is carried opaquely, never interpreted.
        assert ref.provider_ref == PROVIDER_SESSION_ID

    async def test_create_without_parent_omits_parent_id(self) -> None:
        adapter, requests = _adapter(lambda request: self._session_response())
        await adapter.create_conversation(
            CreateBackendConversation(display_title="New", workspace_alias="workspace-1")
        )
        body = json.loads(requests[0].content)
        assert body == {"title": "New"}
        assert "parentID" not in body

    async def test_directory_stays_persisted_on_adapter(self) -> None:
        adapter, _ = _adapter(lambda request: self._session_response())
        await adapter.create_conversation(
            CreateBackendConversation(display_title="New", workspace_alias="workspace-1")
        )
        assert adapter.directory == DIRECTORY

    async def test_create_failure_raises(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(500, json={"error": "boom"}))
        with pytest.raises(RuntimeError):
            await adapter.create_conversation(
                CreateBackendConversation(display_title="New", workspace_alias="workspace-1")
            )

    async def test_create_without_session_id_raises(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(200, json={}))
        with pytest.raises(ValueError):
            await adapter.create_conversation(
                CreateBackendConversation(display_title="New", workspace_alias="workspace-1")
            )


class TestSubmit:
    async def test_204_is_confirmed_and_never_none(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(204))
        result = await adapter.submit(_ref(), _turn_request())
        assert result is not None
        assert isinstance(result, BackendSubmitResult)
        assert result.outcome is BackendOutcome.CONFIRMED

    async def test_500_is_retryable_and_not_retry_safe(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(500, json={"error": "boom"}))
        result = await adapter.submit(_ref(), _turn_request())
        assert result.outcome is BackendOutcome.RETRYABLE
        assert result.retry_safe is False

    async def test_connection_error_is_retryable_and_never_none(self) -> None:
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        adapter, _ = _adapter(refused)
        result = await adapter.submit(_ref(), _turn_request())
        assert result is not None
        assert result.outcome is BackendOutcome.RETRYABLE
        assert result.retry_safe is False

    async def test_sends_text_part_and_directory_on_prompt_async(self) -> None:
        adapter, requests = _adapter(lambda request: httpx.Response(204))
        await adapter.submit(_ref(), _turn_request())
        request = requests[0]
        assert request.method == "POST"
        assert request.url.path == f"/session/{PROVIDER_SESSION_ID}/prompt_async"
        assert request.url.params["directory"] == DIRECTORY
        body = json.loads(request.content)
        assert body == {"parts": [{"type": "text", "text": "please fix the build"}]}

    async def test_empty_parts_fail_closed(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(204))
        with pytest.raises(ValueError):
            await adapter.submit(_ref(), _turn_request(parts=()))

    async def test_structured_only_part_fails_closed(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(204))
        request = _turn_request(
            parts=(
                BackendTurnPart(
                    kind=AgentInputKind.USER_MESSAGE,
                    structured_ref="structured://ref",
                ),
            )
        )
        with pytest.raises(ValueError):
            await adapter.submit(_ref(), request)


class TestCancel:
    async def test_posts_abort_and_confirms_on_2xx(self) -> None:
        adapter, requests = _adapter(lambda request: httpx.Response(200, json=True))
        result = await adapter.cancel(_cancel_request())
        assert isinstance(result, BackendOperationResult)
        request = requests[0]
        assert request.method == "POST"
        assert request.url.path == f"/session/{PROVIDER_SESSION_ID}/abort"
        assert request.url.params["directory"] == DIRECTORY
        assert result.outcome is BackendOutcome.CONFIRMED

    async def test_conversation_scope_ignores_run_id(self) -> None:
        adapter, requests = _adapter(lambda request: httpx.Response(200, json=True))
        result = await adapter.cancel(_cancel_request(run_id=uuid4()))
        assert requests[0].url.path == f"/session/{PROVIDER_SESSION_ID}/abort"
        assert result.outcome is BackendOutcome.CONFIRMED

    async def test_error_is_unknown(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(500, json={}))
        result = await adapter.cancel(_cancel_request())
        assert result.outcome is BackendOutcome.UNKNOWN


class TestInteract:
    async def test_approved_maps_to_once_on_permissions_endpoint(self) -> None:
        adapter, requests = _adapter(lambda request: httpx.Response(200, json=True))
        result = await adapter.interact(_interaction())
        assert isinstance(result, BackendOperationResult)
        request = requests[0]
        assert request.method == "POST"
        assert (
            request.url.path
            == f"/session/{PROVIDER_SESSION_ID}/permissions/per_opencode_1"
        )
        assert request.url.params["directory"] == DIRECTORY
        assert json.loads(request.content) == {"response": "once"}
        assert result.outcome is BackendOutcome.CONFIRMED

    async def test_denied_maps_to_reject(self) -> None:
        adapter, requests = _adapter(lambda request: httpx.Response(200, json=True))
        result = await adapter.interact(_interaction(decision=ApprovalDecision.DENIED))
        assert json.loads(requests[0].content) == {"response": "reject"}
        assert result.outcome is BackendOutcome.CONFIRMED

    async def test_error_is_unknown(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(500, json={}))
        result = await adapter.interact(_interaction())
        assert result.outcome is BackendOutcome.UNKNOWN

    @pytest.mark.parametrize(
        "decision",
        [
            ApprovalDecision.EXPIRED,
            ApprovalDecision.REVOKED,
            ApprovalDecision.UNKNOWN,
        ],
    )
    async def test_unsupported_decisions_fail_closed(self, decision: ApprovalDecision) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(200, json=True))
        with pytest.raises(ValueError):
            await adapter.interact(_interaction(decision=decision))


class TestDeleteConversation:
    async def test_delete_confirms_on_2xx(self) -> None:
        adapter, requests = _adapter(lambda request: httpx.Response(200, json=True))
        result = await adapter.delete_conversation(_ref())
        request = requests[0]
        assert request.method == "DELETE"
        assert request.url.path == f"/session/{PROVIDER_SESSION_ID}"
        assert request.url.params["directory"] == DIRECTORY
        assert result.outcome is BackendOutcome.CONFIRMED

    async def test_delete_404_counts_as_confirmed(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(404, json={}))
        result = await adapter.delete_conversation(_ref())
        assert result.outcome is BackendOutcome.CONFIRMED

    async def test_delete_error_is_unknown(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(500, json={}))
        result = await adapter.delete_conversation(_ref())
        assert result.outcome is BackendOutcome.UNKNOWN


class TestReconcile:
    def _messages(self, count: int = 3) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"info": {"id": f"msg_{index}"}, "parts": []}
                for index in range(count)
            ],
        )

    async def test_200_confirms_with_bounded_message_count(self) -> None:
        adapter, _ = _adapter(lambda request: self._messages(count=3))
        snapshot = await adapter.reconcile(_ref())
        assert snapshot.outcome is BackendOutcome.CONFIRMED
        assert snapshot.message_count == 3
        assert snapshot.state is BackendRuntimeState.READY
        assert snapshot.resumable is False

    async def test_sends_bounded_limit_and_directory(self) -> None:
        adapter, requests = _adapter(lambda request: self._messages())
        await adapter.reconcile(_ref())
        request = requests[0]
        assert request.method == "GET"
        assert request.url.path == f"/session/{PROVIDER_SESSION_ID}/message"
        assert request.url.params["directory"] == DIRECTORY
        assert request.url.params["limit"] == str(RECONCILE_MESSAGE_LIMIT)

    async def test_404_is_context_lost(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(404, json={}))
        snapshot = await adapter.reconcile(_ref())
        assert snapshot.outcome is BackendOutcome.CONTEXT_LOST
        assert snapshot.state is BackendRuntimeState.CONTEXT_LOST
        assert snapshot.resumable is False

    async def test_error_is_unknown(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(500, json={}))
        snapshot = await adapter.reconcile(_ref())
        assert snapshot.outcome is BackendOutcome.UNKNOWN
        assert snapshot.state is BackendRuntimeState.UNAVAILABLE

    async def test_connection_error_is_unknown(self) -> None:
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        adapter, _ = _adapter(refused)
        snapshot = await adapter.reconcile(_ref())
        assert snapshot.outcome is BackendOutcome.UNKNOWN

    async def test_unexpected_body_is_unknown(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(200, json={"not": "a list"}))
        snapshot = await adapter.reconcile(_ref())
        assert snapshot.outcome is BackendOutcome.UNKNOWN
        assert snapshot.message_count is None


class TestClose:
    async def test_close_closes_owned_client(self) -> None:
        adapter = OpenCodeAdapter(
            base_url=BASE_URL,
            directory=DIRECTORY,
            backend_version=BACKEND_VERSION,
        )
        assert adapter._owns_client is True
        await adapter.close()
        assert adapter._client.is_closed

    async def test_close_leaves_injected_client_open(self) -> None:
        adapter, _ = _adapter(lambda request: httpx.Response(204))
        await adapter.close()
        assert adapter._client.is_closed is False
