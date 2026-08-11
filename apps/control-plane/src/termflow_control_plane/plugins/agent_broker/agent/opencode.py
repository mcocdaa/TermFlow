"""OpenCode reference backend adapter — non-streaming core (M4.1).

Implements the non-streaming parts of :class:`AgentBackend` against the
pinned OpenCode HTTP contract (``apps/control-plane/tests/fixtures/opencode/
opencode-pin.md`` and the captured OpenAPI spec in the same fixture
directory).  Endpoint paths, the ``directory`` persistence rule (pin §3), and
the no-exactly-once stance (pin §5/§6.1) follow that pin; SSE event streaming
(``GET /global/event``) is deliberately out of scope here and lands in a later
milestone.

**No exactly-once claim (pin §5/§6.1):** ``POST /session/:id/prompt_async``
acknowledges with ``204 No Content`` and provides no transport-level
idempotency key, so this adapter never claims exactly-once delivery.
``submit()`` reports only admission/transport acceptance; every uncertain
outcome is ``retryable`` with ``retry_safe=False`` so an action-producing turn
is never auto-resubmitted by the scheduler.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from termflow_protocol.agent import AgentInputKind, ApprovalDecision, BackendRuntimeState

from .backend import (
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
from .turns import (
    BackendCancelRequest,
    BackendConversationRef,
    BackendConversationSnapshot,
    BackendEventScope,
    BackendInteraction,
    BackendInteractionKind,
    BackendNotification,
    BackendOperationResult,
    BackendOutcome,
    BackendSubmitResult,
    BackendTurnPart,
    BackendTurnRequest,
    CreateBackendConversation,
    EvidenceRecord,
)

BACKEND_KIND = "opencode"

#: Bounded reconcile fetch (pin §2: the ``limit`` query parameter on the
#: message endpoint); the snapshot never materializes an unbounded provider
#: response.
RECONCILE_MESSAGE_LIMIT = 100

#: AgentInput kinds this milestone renders as a ``prompt_async`` text part
#: (capability matrix §1).  ``permission_resolved`` is not listed: it is
#: resolved through the optional :meth:`OpenCodeAdapter.interact` facet,
#: never submitted as a turn.
_ACCEPTED_INPUT_KINDS: tuple[AgentInputKind, ...] = (
    AgentInputKind.USER_MESSAGE,
    AgentInputKind.WATCH_TRIGGERED,
    AgentInputKind.TIMER_TRIGGERED,
    AgentInputKind.SYSTEM_NOTIFICATION,
)


class OpenCodeAdapter:
    """Non-streaming ``AgentBackend`` adapter for the pinned OpenCode contract.

    One instance is pinned to one runtime and one ``directory`` (pin §3): the
    directory is fixed at construction, sent as a query parameter on every
    session-scoped request, and persisted on the adapter — never inside
    :class:`BackendConversationRef`, which stays opaque and provider-free.

    ``client`` may be any async HTTP client exposing ``post``/``get``/
    ``delete`` (for example ``httpx.AsyncClient``) and is accepted so tests
    inject an ``httpx.MockTransport``; httpx is a dev-group dependency here,
    so it is never imported at module level.  When no client is injected the
    adapter creates and owns one, and :meth:`close` releases it.
    """

    def __init__(
        self,
        *,
        base_url: str,
        directory: str,
        backend_version: str,
        client: object | None = None,
        runtime_id: str | None = None,
        binding_capability_epoch: int = 0,
    ) -> None:
        if not base_url or not directory or not backend_version:
            raise ValueError("base_url, directory, and backend_version must be non-empty")
        if binding_capability_epoch < 0:
            raise ValueError("binding_capability_epoch must not be negative")
        self.base_url = base_url.rstrip("/")
        self.directory = directory
        self.backend_version = backend_version
        # The runtime URL uniquely identifies the runtime this adapter is
        # pinned to; an explicitly assigned runtime identity overrides it.
        self.runtime_id = runtime_id or self.base_url
        self.binding_capability_epoch = binding_capability_epoch
        self._owns_client = client is None
        if client is None:
            try:
                import httpx  # dev-group dependency: lazy import, never module-level
            except ImportError as exc:  # pragma: no cover - dev env always has httpx
                raise ImportError(
                    "httpx is required to create an owned HTTP client; "
                    "inject an async client instead"
                ) from exc
            client = httpx.AsyncClient(timeout=30.0)
        self._client: Any = client
        self._capabilities = AgentBackendCapabilities(
            streaming_output=False,  # SSE (GET /global/event) lands in a later milestone
            context_mode=ContextMode.LOST_ON_RESTART,  # until volume-proof resume (M4)
            submit_mode=SubmitMode.NON_IDEMPOTENT,  # 204, no idempotency key (pin §6.1)
            cancel_scope=CancelScope.CONVERSATION,  # POST /session/:id/abort (matrix §5)
            replay_mode=ReplayMode.NONE,  # no event stream in this milestone
            concurrency_mode=ConcurrencyMode.SERIALIZED,  # matrix §7
            tool_call_identity=ToolCallIdentity.BINDING,
            runtime_isolation=RuntimeIsolation.BINDING,  # matrix §7
            accepted_input_kinds=_ACCEPTED_INPUT_KINDS,
            structured_part_fidelity=StructuredPartFidelity.NONE,
            tool_activity_events=False,
            permission_events=False,
            usage_cost_reporting=False,
            explicit_run_boundaries=True,
        )

    async def capabilities(self) -> AgentBackendCapabilities:
        """Return the pinned capability descriptor (matrix §3/§5/§7)."""
        return self._capabilities

    async def create_conversation(
        self, request: CreateBackendConversation
    ) -> BackendConversationRef:
        """Create a backend session (``POST /session``) and wrap its id opaquely.

        The body carries ``title``/``parentID`` per the pinned contract; the
        pinned ``directory`` is sent as a query parameter so the session is
        created in it (pin §3).  The returned ``Session.id`` is stored
        opaquely in ``provider_ref`` while the directory stays persisted on
        the adapter.  A non-2xx response raises :class:`RuntimeError`; a 2xx
        response without a usable session id raises :class:`ValueError` — a
        broken ref is never returned.
        """
        body: dict[str, str] = {"title": request.display_title}
        if request.parent_ref is not None:
            body["parentID"] = request.parent_ref.provider_ref
        response = await self._client.post(
            self._url("/session"),
            params={"directory": self.directory},
            json=body,
        )
        if response.status_code >= 300:
            raise RuntimeError(
                f"opencode session create failed: HTTP {response.status_code}"
            )
        payload = _require_mapping(response)
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("opencode session create returned no session id")
        return BackendConversationRef(
            backend_kind=BACKEND_KIND,
            backend_version=self.backend_version,
            runtime_id=self.runtime_id,
            binding_capability_epoch=self.binding_capability_epoch,
            provider_ref=session_id,
        )

    async def submit(
        self, ref: BackendConversationRef, request: BackendTurnRequest
    ) -> BackendSubmitResult:
        """Admit one turn via ``POST /session/:id/prompt_async``.

        Every turn part is rendered as a ``TextPartInput``; a part carrying
        only a structured reference fails closed (fail-closed rule, matrix
        §1; ``structured_part_fidelity=none``).  Outcome mapping: ``204`` →
        ``confirmed``; any other status and any connection error →
        ``retryable`` with ``retry_safe=False``.

        **No exactly-once claim (pin §5):** ``prompt_async`` acknowledges
        with ``204`` and no transport idempotency key, so this adapter never
        claims exactly-once delivery.  A ``retryable`` result must never be
        auto-resubmitted; uncertain delivery is a visible, recoverable state.
        """
        if not request.parts:
            raise ValueError("opencode submit requires at least one turn part")
        parts = [_render_text_part(part) for part in request.parts]
        try:
            response = await self._client.post(
                self._url(f"/session/{ref.provider_ref}/prompt_async"),
                params={"directory": self.directory},
                json={"parts": parts},
            )
        except Exception as exc:
            return BackendSubmitResult(
                outcome=BackendOutcome.RETRYABLE,
                message=f"opencode prompt_async connection failed: {exc}",
            )
        if response.status_code == 204:
            return BackendSubmitResult(
                outcome=BackendOutcome.CONFIRMED,
                message="opencode accepted the prompt (204)",
            )
        return BackendSubmitResult(
            outcome=BackendOutcome.RETRYABLE,
            retry_safe=False,
            message=f"opencode prompt_async returned HTTP {response.status_code}",
        )

    async def cancel(self, request: BackendCancelRequest) -> BackendOperationResult:
        """Abort the running session (``POST /session/:id/abort``).

        Cancel scope is conversation-wide (matrix §5): a ``run_id`` on the
        request is ignored because the pinned API has no finer per-run cancel
        endpoint.  ``2xx`` → ``confirmed``; any other status or connection
        error → ``unknown``.
        """
        ref = request.conversation_ref
        try:
            response = await self._client.post(
                self._url(f"/session/{ref.provider_ref}/abort"),
                params={"directory": self.directory},
            )
        except Exception as exc:
            return BackendOperationResult(
                outcome=BackendOutcome.UNKNOWN,
                message=f"opencode abort connection failed: {exc}",
            )
        if 200 <= response.status_code < 300:
            return BackendOperationResult(
                outcome=BackendOutcome.CONFIRMED,
                message="opencode aborted the session",
            )
        return BackendOperationResult(
            outcome=BackendOutcome.UNKNOWN,
            message=f"opencode abort returned HTTP {response.status_code}",
        )

    async def interact(self, request: BackendInteraction) -> BackendOperationResult:
        """Resolve one permission (``POST /session/:id/permissions/:permissionID``).

        Only ``PERMISSION_RESOLVE`` is supported.  Decisions map per the
        pinned config policy (``opencode-config.yaml``): ``approved`` →
        ``"once"`` and ``denied`` → ``"reject"``; "always"/remember decisions
        are rejected, so any other decision raises :class:`ValueError` (fail
        closed).  ``2xx`` → ``confirmed``; any other status or connection
        error → ``unknown``.
        """
        if request.kind is not BackendInteractionKind.PERMISSION_RESOLVE:
            raise ValueError(f"opencode does not support interaction kind {request.kind}")
        if request.permission_ref is None:
            raise ValueError("permission_resolve requires a permission_ref")
        # The decision mapping fails closed before any network call, so a
        # ValueError here is never misreported as an unknown provider error.
        response_body = _permission_response_body(request)
        ref = request.conversation_ref
        try:
            response = await self._client.post(
                self._url(
                    f"/session/{ref.provider_ref}/permissions/{request.permission_ref}"
                ),
                params={"directory": self.directory},
                json={"response": response_body},
            )
        except Exception as exc:
            return BackendOperationResult(
                outcome=BackendOutcome.UNKNOWN,
                message=f"opencode permission response connection failed: {exc}",
            )
        if 200 <= response.status_code < 300:
            return BackendOperationResult(
                outcome=BackendOutcome.CONFIRMED,
                message="opencode processed the permission response",
            )
        return BackendOperationResult(
            outcome=BackendOutcome.UNKNOWN,
            message=f"opencode permission response returned HTTP {response.status_code}",
        )

    async def reconcile(
        self, ref: BackendConversationRef
    ) -> BackendConversationSnapshot:
        """Prove session message state (``GET /session/:id/message``).

        The message list is fetched with a bounded ``limit`` (pin §2).
        ``200`` → ``confirmed`` with ``message_count``; ``404`` →
        ``context_lost`` (the session no longer exists on the backend); any
        other status or connection error → ``unknown``.  ``resumable`` is
        always ``False`` while ``context_mode=lost_on_restart`` (until
        volume-proof resume lands in a later milestone).
        """
        try:
            response = await self._client.get(
                self._url(f"/session/{ref.provider_ref}/message"),
                params={
                    "directory": self.directory,
                    "limit": str(RECONCILE_MESSAGE_LIMIT),
                },
            )
        except Exception as exc:
            return _unavailable_snapshot(ref, f"message list connection failed: {exc}")
        if response.status_code == 200:
            messages = _require_list(response)
            if messages is None:
                return _unavailable_snapshot(ref, "unexpected message list body")
            return BackendConversationSnapshot(
                conversation_ref=ref,
                outcome=BackendOutcome.CONFIRMED,
                state=BackendRuntimeState.READY,
                resumable=False,
                message_count=len(messages),
            )
        if response.status_code == 404:
            return BackendConversationSnapshot(
                conversation_ref=ref,
                outcome=BackendOutcome.CONTEXT_LOST,
                state=BackendRuntimeState.CONTEXT_LOST,
                resumable=False,
            )
        return _unavailable_snapshot(
            ref, f"message list returned HTTP {response.status_code}"
        )

    async def delete_conversation(
        self, ref: BackendConversationRef
    ) -> BackendOperationResult:
        """Idempotent backend cleanup (``DELETE /session/:id``).

        ``2xx`` → ``confirmed``; ``404`` also → ``confirmed`` because the
        session is already gone, which is the desired end state; any other
        status or connection error → ``unknown``.
        """
        try:
            response = await self._client.delete(
                self._url(f"/session/{ref.provider_ref}"),
                params={"directory": self.directory},
            )
        except Exception as exc:
            return BackendOperationResult(
                outcome=BackendOutcome.UNKNOWN,
                message=f"opencode session delete connection failed: {exc}",
            )
        if 200 <= response.status_code < 300 or response.status_code == 404:
            return BackendOperationResult(
                outcome=BackendOutcome.CONFIRMED,
                message="opencode session deleted",
            )
        return BackendOperationResult(
            outcome=BackendOutcome.UNKNOWN,
            message=f"opencode session delete returned HTTP {response.status_code}",
        )

    def events(self, scope: BackendEventScope) -> AsyncIterator[BackendNotification]:
        """SSE event streaming is not part of this milestone.

        The pinned event mode (``GET /global/event``) lands with the streaming
        milestone; calling this raises :class:`NotImplementedError` so the
        absence is loud rather than a silent empty stream.
        """
        raise NotImplementedError(
            "OpenCodeAdapter SSE streaming (GET /global/event) is not implemented yet"
        )

    async def close(self) -> None:
        """Close the HTTP client only when this adapter owns it."""
        if self._owns_client:
            await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def _render_text_part(part: BackendTurnPart) -> dict[str, str]:
    """Render one turn part as an OpenCode ``TextPartInput`` (fail closed)."""
    if part.text is None:
        raise ValueError(
            f"opencode cannot render a {part.kind} part without text; "
            "structured references are unsupported in this milestone"
        )
    return {"type": "text", "text": part.text}


def _permission_response_body(request: BackendInteraction) -> str:
    """Map an approval decision to the pinned permission response vocabulary.

    The pinned config policy allows only ``"once"``/``"reject"`` responses;
    "always"/remember decisions are rejected, so anything else fails closed.
    """
    if request.decision is ApprovalDecision.APPROVED:
        return "once"
    if request.decision is ApprovalDecision.DENIED:
        return "reject"
    raise ValueError(
        f"opencode permission responses may only carry 'once'/'reject'; "
        f"got decision {request.decision!r}"
    )


def _require_mapping(response: Any) -> dict[str, Any]:
    """Parse a response body that must be a JSON object (fail closed)."""
    try:
        payload = response.json()
    except Exception as exc:
        raise ValueError("opencode returned a non-JSON body") from exc
    if not isinstance(payload, dict):
        raise ValueError("opencode returned an unexpected body")
    return payload


def _require_list(response: Any) -> list[Any] | None:
    """Parse a response body that must be a JSON array; ``None`` on failure."""
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    return payload


def _unavailable_snapshot(
    ref: BackendConversationRef, detail: str
) -> BackendConversationSnapshot:
    """Build an explicit ``unknown``/unavailable reconcile snapshot."""
    return BackendConversationSnapshot(
        conversation_ref=ref,
        outcome=BackendOutcome.UNKNOWN,
        state=BackendRuntimeState.UNAVAILABLE,
        resumable=False,
        evidence=(EvidenceRecord(key="diagnostic", value=detail),),
    )
