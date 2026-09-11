"""OpenCode reference backend adapter — non-streaming core (M4.1) + SSE events (M4.2).

Implements :class:`AgentBackend` against the pinned OpenCode HTTP contract
(``apps/control-plane/tests/fixtures/opencode/opencode-pin.md`` and the
captured OpenAPI spec in the same fixture directory).  Endpoint paths, the
``directory`` persistence rule (pin §3), and the no-exactly-once stance (pin
§5/§6.1) follow that pin.

The M4.2 event stream (:meth:`OpenCodeAdapter.events`) subscribes to the pinned
SSE mode ``GET /global/event`` (pin §1), filters envelopes by the persisted
``GlobalEvent.directory`` (pin §3), and normalizes provider events into
provider-neutral :class:`BackendNotification` values (plan §4.4, §6).  Only
visible/bounded text content is surfaced; reasoning, attachments, raw tool
results, and provider-private metadata are dropped and counted (plan §4.3).
Reconnect + reconcile after an SSE disconnect is a later integration milestone
(plan §6.1 step 6); this milestone yields cleanly and never lets a malformed
event break the pump.

**No exactly-once claim (pin §5/§6.1):** ``POST /session/:id/prompt_async``
acknowledges with ``204 No Content`` and provides no transport-level
idempotency key, so this adapter never claims exactly-once delivery.
``submit()`` reports only admission/transport acceptance; every uncertain
outcome is ``retryable`` with ``retry_safe=False`` so an action-producing turn
is never auto-resubmitted by the scheduler.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from termflow_protocol.agent import (
    AgentEventKind,
    AgentInputKind,
    ApprovalDecision,
    BackendRuntimeState,
)

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
    MAX_NOTIFICATION_TEXT_BYTES,
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
    ContextBlock,
    CreateBackendConversation,
    EvidenceRecord,
    NotificationPayload,
    ProviderRef,
)

BACKEND_KIND = "opencode"

#: Bounded reconcile fetch (pin §2: the ``limit`` query parameter on the
#: message endpoint); the snapshot never materializes an unbounded provider
#: response.
RECONCILE_MESSAGE_LIMIT = 100

#: Bounded diagnostic record kept for skipped/dropped SSE events (plan §4.3):
#: identifiers and an adapter-generated reason only, never provider payload
#: content, headers, raw parts, or reasoning.
MAX_SSE_DIAGNOSTICS = 32

# Current OpenCode emits text deltas/parts before a completed
# ``message.updated`` record. Keep a bounded reconstruction cache so the
# latter can carry the visible assistant text without retaining a transcript.
MAX_TRACKED_MESSAGE_TEXTS = 256
MAX_TRACKED_MESSAGE_TEXT_BYTES = MAX_NOTIFICATION_TEXT_BYTES
MAX_TRACKED_TOOL_CALLS = 256
#: Provider user messages (the prompt parts B itself submitted) are echoed by
#: OpenCode's message stream; their ids are remembered so those parts are
#: never projected as assistant transcript.
MAX_TRACKED_USER_MESSAGES = 256

#: Fixed namespace for deterministic UUID5 derivation of ``run_id``/
#: ``message_id`` from opaque backend message IDs (``^msg_``).  The value is
#: arbitrary but fixed so the same backend message maps to the same UUIDs
#: across reconnects; B's run mapping may further map them to canonical
#: AgentRun UUIDs (plan §6.1 step 7).
_BACKEND_ID_NAMESPACE = UUID("b5e2d0e2-1f0f-4f4a-9b6a-3f7c2f0e6d1a")

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


def _require_normalized_identifier(
    value: object,
    *,
    field_name: str,
    max_length: int,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a normalized non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    return value


@dataclass
class OpenCodeSseStats:
    """Bounded counters for SSE normalization, visible to tests and audits.

    Every counter is monotonic for the adapter's lifetime; nothing here ever
    carries provider payload content (plan §4.3).
    """

    malformed_events: int = 0
    scope_mismatch_events: int = 0
    unmapped_events: int = 0
    dropped_reasoning_events: int = 0
    dropped_attachment_events: int = 0
    dropped_private_events: int = 0


@dataclass(frozen=True)
class OpenCodeSseDiagnostic:
    """One size-bounded diagnostic record (plan §4.3).

    Restricted to event type, provider event/session identifiers, and an
    adapter-generated reason; raw parts, tool arguments/results, reasoning,
    headers, and SSE bodies are never recorded here.
    """

    event_id: str
    event_type: str
    session_id: str | None
    note: str


class OpenCodeAdapter:
    """``AgentBackend`` adapter for the pinned OpenCode contract.

    One instance is pinned to one runtime and one ``directory`` (pin §3): the
    directory is fixed at construction, sent as a query parameter on every
    session-scoped request, and persisted on the adapter — never inside
    :class:`BackendConversationRef`, which stays opaque and provider-free.

    ``client`` may be any async HTTP client exposing ``post``/``get``/
    ``delete``/``stream`` (for example ``httpx.AsyncClient``) and is accepted
    so tests inject an ``httpx.MockTransport``; httpx is a dev-group
    dependency here, so it is never imported at module level.  When no client
    is injected the adapter creates and owns one, and :meth:`close` releases
    it.

    SSE normalization counters and the bounded diagnostic record (plan §4.3)
    live on :attr:`stats` and :attr:`diagnostics`; tests assert on them.
    """

    def __init__(
        self,
        *,
        base_url: str,
        directory: str,
        backend_version: str,
        provider_id: str,
        model_id: str,
        client: object | None = None,
        runtime_id: str | None = None,
        binding_capability_epoch: int = 0,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        if not base_url or not directory or not backend_version:
            raise ValueError("base_url, directory, and backend_version must be non-empty")
        if binding_capability_epoch < 0:
            raise ValueError("binding_capability_epoch must not be negative")
        if (username is None) != (password is None):
            raise ValueError("username and password must be provided together")
        self.base_url = base_url.rstrip("/")
        self.directory = directory
        self.backend_version = backend_version
        self._provider_id = _require_normalized_identifier(
            provider_id,
            field_name="provider_id",
            max_length=64,
        )
        self._model_id = _require_normalized_identifier(
            model_id,
            field_name="model_id",
            max_length=128,
        )
        # The runtime URL uniquely identifies the runtime this adapter is
        # pinned to; an explicitly assigned runtime identity overrides it.
        self.runtime_id = runtime_id or self.base_url
        self.binding_capability_epoch = binding_capability_epoch
        self._owns_client = client is None
        # Basic auth (OPENCODE_SERVER_* pair, M4 exit verified): applied at
        # the client level so every request carries it; an injected client
        # keeps whatever auth the injector configured (tests).
        basic_auth = None
        if username is not None:
            assert password is not None
            basic_auth = (username, password)
        if client is None:
            try:
                import httpx  # runtime dependency: lazy import, never module-level
            except ImportError as exc:  # pragma: no cover - dev env always has httpx
                raise ImportError(
                    "httpx is required to create an owned HTTP client; "
                    "inject an async client instead"
                ) from exc
            client = httpx.AsyncClient(timeout=30.0, auth=basic_auth)
        self._client: Any = client
        self.stats = OpenCodeSseStats()
        self.diagnostics: list[OpenCodeSseDiagnostic] = []
        self._message_texts: dict[str, str] = {}
        # OpenCode echoes the user's prompt as ``message.updated``/
        # ``message.part.*`` events; B already owns the durable user message,
        # so these ids are recorded to drop the echo from the assistant
        # transcript.
        self._user_message_ids: dict[str, None] = {}
        # Current OpenCode represents tool execution as successive updates to
        # one ``message.part.updated`` tool part (pending -> running ->
        # completed/error).  Keep only the bounded lifecycle state needed to
        # suppress duplicate starts across those updates; raw input/output is
        # never retained.
        self._tool_call_states: dict[tuple[str, str], str] = {}
        self._capabilities = AgentBackendCapabilities(
            # OpenCode 1.18.x exposes progress through session.status and
            # session.idle; B infers run boundaries from those notifications.
            streaming_output=False,
            context_mode=ContextMode.LOST_ON_RESTART,  # until volume-proof resume (M4)
            submit_mode=SubmitMode.NON_IDEMPOTENT,  # 204, no idempotency key (pin §6.1)
            cancel_scope=CancelScope.CONVERSATION,  # POST /session/:id/abort (matrix §5)
            replay_mode=ReplayMode.NONE,  # no event stream in this milestone
            concurrency_mode=ConcurrencyMode.SERIALIZED,  # matrix §7
            tool_call_identity=ToolCallIdentity.BINDING,
            runtime_isolation=RuntimeIsolation.BINDING,  # matrix §7
            accepted_input_kinds=_ACCEPTED_INPUT_KINDS,
            structured_part_fidelity=StructuredPartFidelity.NONE,
            tool_activity_events=True,
            permission_events=False,
            usage_cost_reporting=False,
            explicit_run_boundaries=False,
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
            provider_ref=ProviderRef(session_id),
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
        body: dict[str, object] = {
            "parts": parts,
            "model": {
                "providerID": self._provider_id,
                "modelID": self._model_id,
            },
        }
        system = _render_system_context(request.context)
        if system is not None:
            body["system"] = system
        try:
            response = await self._client.post(
                self._url(f"/session/{ref.provider_ref}/prompt_async"),
                params={"directory": self.directory},
                json=body,
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

    async def events(self, scope: BackendEventScope) -> AsyncIterator[BackendNotification]:
        """Stream normalized events from the pinned SSE endpoint (M4.2).

        Connects to ``GET /global/event`` (pin §1) with no query parameters;
        every envelope is self-describing with ``GlobalEvent.directory``, so
        one connection serves the whole runtime and the adapter filters by the
        persisted ``directory`` (pin §3).  Each SSE event is normalized into a
        :class:`BackendNotification` carrying the subscription ``scope``
        verbatim (binding/runtime scope).  An envelope whose ``directory``
        does not match the adapter's persisted directory is dropped, counted
        on :attr:`stats`, and recorded on :attr:`diagnostics` (pin §3) — never
        routed to another conversation.

        **Pump resilience (plan §4.3):** a malformed or unknown event is
        skipped, counted, and recorded with identifiers only; it never breaks
        the iterator.  A non-200 response or a connection failure ends the
        stream cleanly with a diagnostic — reconnect + reconcile is a later
        integration milestone (plan §6.1 step 6).  Cancellation
        (``aclose``/``CancelledError``) closes the HTTP stream; the iterator
        never promises provider replay.
        """
        try:
            async with self._client.stream(
                "GET",
                self._url("/global/event"),
                headers={"accept": "text/event-stream"},
            ) as response:
                if response.status_code != 200:
                    self._record_diagnostic(
                        "",
                        "<connection>",
                        None,
                        f"GET /global/event returned HTTP {response.status_code}",
                    )
                    return
                async for data in _iter_sse_data(response.aiter_lines()):
                    notification = self._normalize_event(scope, data)
                    if notification is not None:
                        yield notification
        except Exception as exc:
            # Connection-level failure: end the stream cleanly.  Cancellation
            # (CancelledError/GeneratorExit) is a BaseException and propagates.
            self._record_diagnostic(
                "",
                "<connection>",
                None,
                f"SSE stream failed: {type(exc).__name__}",
            )

    def _normalize_event(
        self, scope: BackendEventScope, raw_data: str
    ) -> BackendNotification | None:
        """Normalize one SSE ``data:`` payload; ``None`` means "not emitted".

        Every failure mode (unparseable JSON, malformed envelope, missing
        identifiers, unknown event type, dropped content) is counted on
        :attr:`stats`, recorded in the bounded :attr:`diagnostics` list, and
        never raised — the pump continues.
        """
        try:
            envelope = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError, ValueError):
            self._skip("", "<unparseable>", None, "SSE data is not valid JSON")
            return None
        if not isinstance(envelope, dict):
            self._skip("", "<envelope>", None, "GlobalEvent envelope is not an object")
            return None
        directory = envelope.get("directory")
        if not isinstance(directory, str) or directory != self.directory:
            self.stats.scope_mismatch_events += 1
            payload = envelope.get("payload")
            mismatched_event_id = payload.get("id") if isinstance(payload, dict) else ""
            if not isinstance(mismatched_event_id, str):
                mismatched_event_id = ""
            self._record_diagnostic(
                mismatched_event_id,
                "<scope>",
                None,
                # The mismatched value is a provider filesystem path and is
                # never embedded in the bounded diagnostic record (§4.3).
                "envelope directory <other> does not match the binding directory",
            )
            return None
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            self._skip("", "<payload>", None, "event payload is not an object")
            return None
        event_id = payload.get("id")
        event_type = payload.get("type")
        if not isinstance(event_id, str) or not event_id:
            self._skip("", "<payload>", None, "event carries no id")
            return None
        if len(event_id) > 256:
            # A truncated dedup key would collide across distinct events;
            # fail this event instead (spec ids are short ^evt_ strings).
            self._skip("", "<payload>", None, "event id exceeds the dedup key bound")
            return None
        if not isinstance(event_type, str) or not event_type:
            self._skip(event_id, "<payload>", None, "event carries no type")
            return None
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            self._skip(event_id, event_type, None, "event properties are not an object")
            return None
        session_id = properties.get("sessionID")
        if not isinstance(session_id, str) or not session_id:
            self._skip(event_id, event_type, None, "event carries no sessionID")
            return None
        return self._dispatch(scope, event_id, event_type, session_id, properties)

    def _dispatch(
        self,
        scope: BackendEventScope,
        event_id: str,
        event_type: str,
        session_id: str,
        properties: dict[str, Any],
    ) -> BackendNotification | None:
        """Map one spec event type to a canonical kind (plan §4.4).

        Event types not listed here are either explicitly dropped content
        (reasoning) or recorded as unmapped; both keep the pump alive.
        """
        if event_type == "session.next.step.started":
            return self._run_started(scope, event_id, session_id, properties)
        if event_type == "session.next.step.ended":
            return self._run_completed(scope, event_id, session_id, properties)
        if event_type in ("session.next.step.failed", "session.error"):
            return self._run_failed(scope, event_id, session_id, properties)
        if event_type == "session.next.text.delta":
            return self._message_delta(scope, event_id, session_id, properties)
        if event_type == "session.next.text.ended":
            return self._message_completed(scope, event_id, session_id, properties)
        if event_type == "message.updated":
            return self._message_updated(scope, event_id, session_id, properties)
        if event_type == "message.part.updated":
            return self._part_updated(scope, event_id, session_id, properties)
        if event_type == "message.part.delta":
            return self._part_delta(scope, event_id, session_id, properties)
        if event_type == "session.next.tool.called":
            return self._tool_started(scope, event_id, session_id, properties)
        if event_type in ("session.next.tool.success", "session.next.tool.failed"):
            return self._tool_completed(
                scope, event_id, session_id, properties, failed=event_type.endswith("failed")
            )
        if event_type in ("permission.asked", "permission.v2.asked"):
            return self._permission_requested(
                scope, event_id, session_id, properties, v2=event_type == "permission.v2.asked"
            )
        if event_type == "session.status":
            return self._backend_state_changed(scope, event_id, session_id, properties, None)
        if event_type == "session.idle":
            return self._backend_state_changed(scope, event_id, session_id, properties, "idle")
        if event_type.startswith("session.next.reasoning"):
            self.stats.dropped_reasoning_events += 1
            self._record_diagnostic(event_id, event_type, session_id, "reasoning content dropped")
            return None
        self.stats.unmapped_events += 1
        self._record_diagnostic(event_id, event_type, session_id, "no canonical mapping")
        return None

    def _notification(
        self,
        scope: BackendEventScope,
        *,
        event_id: str,
        session_id: str,
        kind: AgentEventKind,
        backend_message_id: object | None = None,
        part_id: object | None = None,
        tool_call_id: object | None = None,
        text: object | None = None,
        summary: object | None = None,
        error_code: object | None = None,
        error_message: object | None = None,
    ) -> BackendNotification:
        """Build one normalized notification with bounded, visible payload only.

        ``dedup_key`` is the backend event id (``^evt_``); ``run_id`` and
        ``message_id`` are deterministic UUIDs derived from the backend message
        id (``^msg_``), stable across reconnects.  Every payload field is
        clipped to its model bound; provider-private fields never appear.
        """
        run_id = None
        message_id = None
        if isinstance(backend_message_id, str) and backend_message_id:
            run_id = _backend_uuid(f"opencode:run:{backend_message_id}")
            message_id = _backend_uuid(f"opencode:message:{backend_message_id}")
        return BackendNotification(
            conversation_ref=BackendConversationRef(
                backend_kind=BACKEND_KIND,
                backend_version=self.backend_version,
                runtime_id=self.runtime_id,
                binding_capability_epoch=self.binding_capability_epoch,
                provider_ref=ProviderRef(session_id),
            ),
            scope=scope,
            kind=kind,
            run_id=run_id,
            message_id=message_id,
            part_id=_bounded_str(part_id, 256),
            tool_call_id=_bounded_str(tool_call_id, 256),
            dedup_key=event_id,  # validated ≤ 256 by _normalize_event; never clipped
            payload=NotificationPayload(
                text=_bounded_str(text, MAX_NOTIFICATION_TEXT_BYTES),
                summary=_bounded_str(summary, MAX_NOTIFICATION_TEXT_BYTES),
                error_code=_bounded_str(error_code, 256),
                error_message=_bounded_str(error_message, 4096),
            ),
        )

    def _run_started(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification:
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.RUN_STARTED,
            backend_message_id=properties.get("assistantMessageID"),
        )

    def _run_completed(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification:
        # finish/cost/tokens stay adapter-internal (usage_cost_reporting=False).
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.RUN_COMPLETED,
            backend_message_id=properties.get("assistantMessageID"),
        )

    def _run_failed(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification:
        code, message = _error_fields(properties.get("error"))
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.RUN_FAILED,
            backend_message_id=properties.get("assistantMessageID"),
            error_code=code,
            error_message=message,
        )

    def _message_delta(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification | None:
        delta = properties.get("delta")
        if not isinstance(delta, str):
            self._skip(event_id, "session.next.text.delta", session_id, "delta is not a string")
            return None
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.MESSAGE_DELTA,
            backend_message_id=properties.get("assistantMessageID"),
            text=delta,
        )

    def _message_completed(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification | None:
        text = properties.get("text")
        if not isinstance(text, str):
            self._skip(event_id, "session.next.text.ended", session_id, "text is not a string")
            return None
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.MESSAGE_COMPLETED,
            backend_message_id=properties.get("assistantMessageID"),
            text=text,
        )

    def _message_updated(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification | None:
        info = properties.get("info")
        if not isinstance(info, dict):
            self._skip(event_id, "message.updated", session_id, "info is not an object")
            return None
        message_id = info.get("id")
        if not isinstance(message_id, str) or not message_id:
            self._skip(event_id, "message.updated", session_id, "info carries no id")
            return None
        if info.get("role") == "user":
            # B already persisted the user's message at admission; the
            # provider echo must never become assistant transcript.
            self._remember_user_message(message_id)
            return None
        time = info.get("time")
        completed = time.get("completed") if isinstance(time, dict) else None
        if completed is not None:
            # A tool-call assistant message is an intermediate model step, not
            # the product turn's final visible message.  Its ``message.part``
            # lifecycle carries TOOL_STARTED/TOOL_COMPLETED and a later
            # assistant message (finish=stop) supplies the final text.
            if info.get("finish") == "tool-calls":
                self._record_diagnostic(
                    event_id,
                    "message.updated",
                    session_id,
                    "tool-call message completion is an intermediate step",
                )
                return None
            kind = AgentEventKind.MESSAGE_COMPLETED
            text = self._message_texts.get(message_id)
        else:
            kind = AgentEventKind.MESSAGE_DELTA
            text = None
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=kind,
            backend_message_id=message_id,
            text=text,
        )

    def _part_updated(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification | None:
        part = properties.get("part")
        if not isinstance(part, dict):
            self._skip(event_id, "message.part.updated", session_id, "part is not an object")
            return None
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str):
                self._skip(event_id, "message.part.updated", session_id, "text part has no text")
                return None
            message_id = part.get("messageID")
            part_id = part.get("id")
            if isinstance(message_id, str) and message_id in self._user_message_ids:
                return None
            if isinstance(message_id, str) and message_id:
                self._remember_message_text(message_id, text, part_id)
            return self._notification(
                scope,
                event_id=event_id,
                session_id=session_id,
                kind=AgentEventKind.MESSAGE_DELTA,
                backend_message_id=message_id,
                part_id=part_id,
                text=text,
            )
        if part_type == "reasoning":
            self.stats.dropped_reasoning_events += 1
            self._record_diagnostic(
                event_id, "message.part.updated", session_id, "reasoning part dropped"
            )
            return None
        if part_type == "tool":
            return self._tool_part_updated(scope, event_id, session_id, part)
        if part_type == "file":
            self.stats.dropped_attachment_events += 1
            self._record_diagnostic(
                event_id, "message.part.updated", session_id, "attachment part dropped"
            )
            return None
        self.stats.dropped_private_events += 1
        self._record_diagnostic(
            event_id, "message.part.updated", session_id, f"private part type {part_type!r} dropped"
        )
        return None

    def _tool_part_updated(
        self,
        scope: BackendEventScope,
        event_id: str,
        session_id: str,
        part: dict[str, Any],
    ) -> BackendNotification | None:
        """Normalize current OpenCode tool-part lifecycle updates.

        OpenCode 1.18 emits ``pending`` and ``running`` updates before a
        terminal ``completed`` or ``error`` state.  The pending/running pair
        represents one logical start; only the first is surfaced.  Tool
        arguments and output are intentionally discarded at this boundary.
        """
        call_id = part.get("callID")
        tool = part.get("tool")
        state = part.get("state")
        if not isinstance(call_id, str) or not call_id:
            self._skip(event_id, "message.part.updated", session_id, "tool part has no callID")
            return None
        if not isinstance(tool, str) or not tool:
            self._skip(event_id, "message.part.updated", session_id, "tool part has no tool name")
            return None
        if not isinstance(state, dict):
            self._skip(event_id, "message.part.updated", session_id, "tool part has no state")
            return None
        status = state.get("status")
        if not isinstance(status, str) or not status:
            self._skip(event_id, "message.part.updated", session_id, "tool state has no status")
            return None
        key = (session_id, call_id)
        if status in ("pending", "running"):
            if key in self._tool_call_states:
                return None
            self._remember_tool_call_state(key, status)
            return self._notification(
                scope,
                event_id=event_id,
                session_id=session_id,
                kind=AgentEventKind.TOOL_STARTED,
                backend_message_id=part.get("messageID"),
                part_id=part.get("id"),
                tool_call_id=call_id,
                summary=tool,
            )
        if status in ("completed", "error", "failed"):
            self._remember_tool_call_state(key, status)
            failed = status in ("error", "failed")
            code = message = None
            if failed:
                code, message = _error_fields(state.get("error"))
            return self._notification(
                scope,
                event_id=event_id,
                session_id=session_id,
                kind=AgentEventKind.TOOL_COMPLETED,
                backend_message_id=part.get("messageID"),
                part_id=part.get("id"),
                tool_call_id=call_id,
                error_code=code,
                error_message=message,
            )
        self._skip(event_id, "message.part.updated", session_id, f"unknown tool status {status!r}")
        return None

    def _remember_tool_call_state(self, key: tuple[str, str], status: str) -> None:
        if (
            len(self._tool_call_states) >= MAX_TRACKED_TOOL_CALLS
            and key not in self._tool_call_states
        ):
            self._tool_call_states.pop(next(iter(self._tool_call_states)))
        self._tool_call_states[key] = status

    def _part_delta(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification | None:
        """Normalize a current OpenCode ``message.part.delta`` text chunk."""
        message_id = properties.get("messageID")
        part_id = properties.get("partID")
        field = properties.get("field")
        delta = properties.get("delta")
        if not isinstance(message_id, str) or not message_id:
            self._skip(event_id, "message.part.delta", session_id, "messageID is missing")
            return None
        if message_id in self._user_message_ids:
            return None
        if field != "text" or not isinstance(delta, str):
            self._skip(event_id, "message.part.delta", session_id, "delta is not text")
            return None
        self._remember_message_text(message_id, delta, part_id, append=True)
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.MESSAGE_DELTA,
            backend_message_id=message_id,
            part_id=part_id,
            text=delta,
        )

    def _remember_user_message(self, message_id: str) -> None:
        """Remember one provider user-message id with a bounded cache."""
        if (
            len(self._user_message_ids) >= MAX_TRACKED_USER_MESSAGES
            and message_id not in self._user_message_ids
        ):
            self._user_message_ids.pop(next(iter(self._user_message_ids)))
        self._user_message_ids[message_id] = None

    def _remember_message_text(
        self,
        message_id: str,
        text: str,
        part_id: object | None,
        *,
        append: bool = False,
    ) -> None:
        """Keep a bounded reconstruction for one assistant message."""
        del part_id
        if (
            len(self._message_texts) >= MAX_TRACKED_MESSAGE_TEXTS
            and message_id not in self._message_texts
        ):
            self._message_texts.pop(next(iter(self._message_texts)))
        previous = self._message_texts.get(message_id, "")
        if append:
            value = previous + text
        elif text:
            value = text
        else:
            value = previous
        self._message_texts[message_id] = value[:MAX_TRACKED_MESSAGE_TEXT_BYTES]

    def _tool_started(
        self, scope: BackendEventScope, event_id: str, session_id: str, properties: dict[str, Any]
    ) -> BackendNotification | None:
        call_id = properties.get("callID")
        if not isinstance(call_id, str) or not call_id:
            self._skip(event_id, "session.next.tool.called", session_id, "callID is missing")
            return None
        tool = properties.get("tool")
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.TOOL_STARTED,
            backend_message_id=properties.get("assistantMessageID"),
            tool_call_id=call_id,
            summary=tool,
        )

    def _tool_completed(
        self,
        scope: BackendEventScope,
        event_id: str,
        session_id: str,
        properties: dict[str, Any],
        *,
        failed: bool,
    ) -> BackendNotification | None:
        call_id = properties.get("callID")
        if not isinstance(call_id, str) or not call_id:
            event_label = "session.next.tool.failed" if failed else "session.next.tool.success"
            self._skip(event_id, event_label, session_id, "callID is missing")
            return None
        code = message = None
        if failed:
            code, message = _error_fields(properties.get("error"))
        # Raw tool results/content/outputPaths are never surfaced.
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.TOOL_COMPLETED,
            backend_message_id=properties.get("assistantMessageID"),
            tool_call_id=call_id,
            error_code=code,
            error_message=message,
        )

    def _permission_requested(
        self,
        scope: BackendEventScope,
        event_id: str,
        session_id: str,
        properties: dict[str, Any],
        *,
        v2: bool,
    ) -> BackendNotification:
        # The opaque permissionID (^per) has no BackendNotification carrier in
        # this milestone; only the bounded action summary and (v1) tool call id
        # are surfaced.  B's approval flow obtains the permission ref through
        # the single-use permission mapping (plan §6, M5).
        call_id = None
        message_id = None
        if v2:
            summary = properties.get("action")
        else:
            summary = properties.get("permission")
            tool_info = properties.get("tool")
            if isinstance(tool_info, dict):
                call_id = tool_info.get("callID")
                message_id = tool_info.get("messageID")
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.PERMISSION_REQUESTED,
            backend_message_id=message_id,
            tool_call_id=call_id,
            summary=summary,
        )

    def _backend_state_changed(
        self,
        scope: BackendEventScope,
        event_id: str,
        session_id: str,
        properties: dict[str, Any],
        fallback: str | None,
    ) -> BackendNotification:
        status = properties.get("status")
        summary: str | None
        if isinstance(status, dict):
            # Spec-shaped SessionStatus: an object discriminated by ``type``
            # ({"type": "busy"} / {"type": "idle"} / {"type": "retry",
            # "attempt", "message", "next", ...}).
            status_type = status.get("type")
            if isinstance(status_type, str) and status_type:
                # Keep the client vocabulary stable and finite: ``retry`` is
                # one state; the attempt number is adapter-internal detail.
                summary = "retry" if status_type == "retry" else status_type
            else:
                summary = fallback
        elif isinstance(status, str):
            # Backward compatibility with string statuses seen in the wild.
            summary = status
        else:
            summary = fallback
        return self._notification(
            scope,
            event_id=event_id,
            session_id=session_id,
            kind=AgentEventKind.BACKEND_STATE_CHANGED,
            summary=summary,
        )

    def _skip(self, event_id: str, event_type: str, session_id: str | None, note: str) -> None:
        """Count one malformed event and record its diagnostic; the pump continues."""
        self.stats.malformed_events += 1
        self._record_diagnostic(event_id, event_type, session_id, note)

    def _record_diagnostic(
        self, event_id: str, event_type: str, session_id: str | None, note: str
    ) -> None:
        """Record one size-bounded diagnostic (plan §4.3): identifiers only."""
        self.diagnostics.append(
            OpenCodeSseDiagnostic(
                event_id=event_id, event_type=event_type, session_id=session_id, note=note
            )
        )
        if len(self.diagnostics) > MAX_SSE_DIAGNOSTICS:
            del self.diagnostics[0]

    async def close(self) -> None:
        """Close the HTTP client only when this adapter owns it."""
        if self._owns_client:
            await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"


def _render_system_context(context: ContextBlock | None) -> str | None:
    """Render trusted context facts as the provider system prompt."""
    if context is None or not context.facts:
        return None
    facts = [fact.text for fact in context.facts if fact.text]
    return "\n".join(facts) if facts else None


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


async def _iter_sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the raw ``data:`` payload of each SSE event from a line stream.

    Implements the SSE framing subset the pinned endpoint uses: ``data:``
    lines accumulate into one event (joined with ``\\n``), a blank line
    dispatches it, ``:``-prefixed lines are comments/heartbeats, and any other
    field line (``event:``/``id:``/``retry:``/unknown) is ignored.  A trailing
    unterminated data block is flushed at stream end.  Line-level framing is
    never an error — garbage lines are ignored, not raised, so the pump stays
    alive.
    """
    data_lines: list[str] = []
    async for line in lines:
        line = line.rstrip("\r")
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].removeprefix(" "))
            continue
    if data_lines:
        yield "\n".join(data_lines)


def _backend_uuid(seed: str) -> UUID:
    """Deterministic UUID5 over a backend identifier seed (stable across reconnects)."""
    return uuid5(_BACKEND_ID_NAMESPACE, seed)


def _bounded_str(value: object | None, limit: int) -> str | None:
    """Clip a string to the model's field bound; non-strings become ``None``.

    The bound is applied in characters, matching the pydantic ``max_length``
    semantics of the ``BackendNotification`` payload fields.
    """
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= limit else value[:limit]


def _error_fields(error: object) -> tuple[str | None, str | None]:
    """Extract bounded error code/message from either spec error shape.

    ``session.next.step.failed`` carries ``{type, message}``
    (``SessionErrorUnknown``); ``session.error`` carries effect-style
    ``{name, data: {message, ...}}`` (``APIError``/``UnknownError``/...).  Raw
    provider error bodies (headers, response bodies, metadata) stay internal.
    """
    if not isinstance(error, dict):
        return None, None
    code = error.get("type")
    if not isinstance(code, str) or not code:
        code = error.get("name")
    if not isinstance(code, str) or not code:
        code = None
    message = error.get("message")
    if not isinstance(message, str) or not message:
        message = None
    data = error.get("data")
    if isinstance(data, dict):
        inner = data.get("message")
        if isinstance(inner, str) and inner:
            message = inner
    return code, message
