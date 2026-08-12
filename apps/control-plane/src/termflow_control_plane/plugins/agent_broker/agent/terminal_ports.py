"""Typed terminal capability ports and concrete services (plan §10, §18).

``TerminalObservationPort`` covers bounded pane listing, cursor-based reads,
and cursor resolution; ``TerminalCommandPort`` covers typed pane writes and
is intentionally *declared but not implemented* in the observe-only M2
milestone; ``ContinuationPort`` covers durable watch continuations.

The concrete services run against B's live instance registry and the M1.3
watch repositories.  Every failure surfaces as a structured
:class:`TermFlowToolError` carrying a stable
:class:`~termflow_protocol.mcp.TermFlowErrorCode`; the MCP server adapter
(M4/M5) translates those into structured MCP errors without re-interpreting
pane semantics.

M2.3 persistence note: the watch repositories do not yet expose dedicated
condition columns, so ``WatchContinuationService`` serializes the full
creation contract (condition + start cursor) into the persisted
``start_cursor`` column as a versioned JSON envelope.  The M3 watch engine
owns the production matcher/cursor persistence format.
"""

from __future__ import annotations

import json
import logging
from typing import Literal, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

from termflow_protocol.mcp import (
    MAX_PANE_READ_BYTES,
    PaneCursor,
    PaneReadParams,
    PaneReadResult,
    PaneReadView,
    PaneSendKeysParams,
    PaneSendKeysResult,
    PaneSendTextParams,
    PaneSendTextResult,
    PaneSummary,
    StreamGapInfo,
    TermFlowErrorCode,
    WatchCancelParams,
    WatchCancelResult,
    WatchCondition,
    WatchCreateParams,
    WatchCreateResult,
    WatchDetail,
    WatchGetParams,
    WatchGetResult,
    WatchListParams,
    WatchListResult,
    WatchStatus,
    WatchSummary,
)
from termflow_protocol.messages import (
    CaptureKind,
    PaneCaptureErrorPayload,
    PaneCaptureRequestPayload,
    PaneCaptureResultPayload,
)
from termflow_protocol.topology import PaneId

from termflow_control_plane.connections.registry import (
    CapabilityUnavailable,
    ConnectionBackpressure,
    InstanceOffline,
    LiveConnection,
    LiveInstanceRegistry,
)
from termflow_control_plane.persistence.models import Watch
from termflow_control_plane.persistence.repositories import AgentBindingRepository, WatchRepository

logger = logging.getLogger(__name__)

#: A-side capture replies whose ``error_code`` maps to a stable tool code.
_WIRE_ERROR_CODES: dict[str, TermFlowErrorCode] = {
    "pane_not_found": TermFlowErrorCode.PANE_NOT_FOUND,
    "incarnation_changed": TermFlowErrorCode.INCARNATION_CHANGED,
    "quota_exceeded": TermFlowErrorCode.QUOTA_EXCEEDED,
    "stream_gap": TermFlowErrorCode.STREAM_GAP,
    "invalid_request": TermFlowErrorCode.INVALID_REQUEST,
    #: An old A that never negotiated ``bounded_capture`` fails closed (M2.4);
    #: surface the denial as a policy error, not an internal error.
    "capture_unsupported": TermFlowErrorCode.POLICY_DENIED,
}

#: The literal gap reasons A can report on the wire (§9.2).
GapReason = Literal["stream_changed", "overwritten", "backpressure", "control_paused"]
_GAP_REASONS: frozenset[str] = frozenset(
    {"stream_changed", "overwritten", "backpressure", "control_paused"}
)

#: Watch row state values mapped onto the protocol status vocabulary.
_WATCH_STATUSES: dict[str, WatchStatus] = {
    "active": WatchStatus.ACTIVE,
    "triggered": WatchStatus.TRIGGERED,
    "expired": WatchStatus.EXPIRED,
    "cancelled": WatchStatus.CANCELLED,
    "failed": WatchStatus.FAILED,
}


class TermFlowToolError(Exception):
    """Structured tool failure carrying a stable :class:`TermFlowErrorCode`.

    Raised by the terminal capability services and the MCP tool handlers;
    the MCP server adapter translates it into a structured MCP error without
    re-deriving semantics.
    """

    def __init__(self, error_code: TermFlowErrorCode, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@runtime_checkable
class TerminalObservationPort(Protocol):
    """Bounded capture/observation of terminal pane output (plan §10)."""

    async def list_panes(self, instance_id: UUID) -> list[PaneSummary]: ...

    async def read_pane(self, instance_id: UUID, params: PaneReadParams) -> PaneReadResult: ...

    async def resolve_cursor(self, instance_id: UUID, pane_id: PaneId) -> PaneCursor | None: ...


@runtime_checkable
class TerminalCommandPort(Protocol):
    """Policy-gated terminal input with typed receipts (plan §12).

    Declared in the observe-only milestone but not implemented: the concrete
    service raises :class:`NotImplementedError` until writes land with M5.
    """

    async def send_text(
        self, instance_id: UUID, params: PaneSendTextParams
    ) -> PaneSendTextResult: ...

    async def send_keys(
        self, instance_id: UUID, params: PaneSendKeysParams
    ) -> PaneSendKeysResult: ...


@runtime_checkable
class ContinuationPort(Protocol):
    """Durable watch continuations on one binding (plan §11)."""

    async def create_watch(
        self, binding_id: UUID, params: WatchCreateParams
    ) -> WatchCreateResult: ...

    async def list_watches(
        self, binding_id: UUID, params: WatchListParams | None = None
    ) -> WatchListResult: ...

    async def get_watch(self, binding_id: UUID, params: WatchGetParams) -> WatchGetResult: ...

    async def cancel_watch(
        self, binding_id: UUID, params: WatchCancelParams
    ) -> WatchCancelResult: ...


@runtime_checkable
class PaneCursorStore(Protocol):
    """Source of the last accepted observation cursor for a pane (plan §11.2).

    The watch engine milestone (M3) owns the durable, transactionally
    persisted implementation; the Observation Service only depends on this
    narrow read surface.
    """

    async def get_cursor(self, instance_id: UUID, pane_id: PaneId) -> PaneCursor | None: ...


class ObservationService:
    """Concrete :class:`TerminalObservationPort` over live Term connections.

    Pane listing reads the connection topology; reads map the bounded tool
    parameters onto ``PaneCaptureRequestPayload`` and await A's correlated
    capture through ``LiveConnection.submit_capture``.  The pane incarnation
    is rechecked before every request (from the caller's cursor or the
    injected cursor store), ``max_bytes`` is enforced on both sides of the
    wire, and A-side gaps/errors surface as structured results/errors.
    """

    def __init__(
        self,
        instances: LiveInstanceRegistry,
        *,
        cursor_store: PaneCursorStore | None = None,
    ) -> None:
        self._instances = instances
        self._cursor_store = cursor_store

    async def list_panes(self, instance_id: UUID) -> list[PaneSummary]:
        connection = await self._connection(instance_id)
        if connection.topology is None:
            return []
        panes: list[PaneSummary] = []
        for window in connection.topology.windows:
            for pane in window.panes:
                panes.append(
                    PaneSummary(
                        pane_id=pane.pane_id,
                        index=pane.index,
                        title=pane.title,
                        active=pane.active,
                        dead=pane.dead,
                    )
                )
        return panes

    async def read_pane(self, instance_id: UUID, params: PaneReadParams) -> PaneReadResult:
        connection = await self._connection(instance_id)
        if connection.topology is None or not connection.topology.contains_pane(params.pane_id):
            raise TermFlowToolError(
                TermFlowErrorCode.PANE_NOT_FOUND,
                f"pane {params.pane_id} is not present in the Term topology",
            )
        if params.max_bytes > MAX_PANE_READ_BYTES:
            raise TermFlowToolError(
                TermFlowErrorCode.QUOTA_EXCEEDED,
                f"max_bytes must not exceed the {MAX_PANE_READ_BYTES} byte protocol bound",
            )
        incarnation = await self._request_incarnation(instance_id, params)
        request = self._build_capture_request(instance_id, params, incarnation=incarnation)
        try:
            outcome = await connection.submit_capture(request)
        except InstanceOffline as exc:
            raise TermFlowToolError(
                TermFlowErrorCode.INTERNAL_ERROR, "the Term instance went offline"
            ) from exc
        except ConnectionBackpressure as exc:
            raise TermFlowToolError(
                TermFlowErrorCode.INTERNAL_ERROR,
                "the Term bridge queue is full; retry later",
            ) from exc
        except CapabilityUnavailable as exc:
            raise TermFlowToolError(
                TermFlowErrorCode.POLICY_DENIED,
                "bounded pane capture was not negotiated by this Instance",
            ) from exc
        if isinstance(outcome, PaneCaptureErrorPayload):
            raise self._map_capture_error(outcome)
        return self._map_capture_result(instance_id, params, outcome)

    async def resolve_cursor(self, instance_id: UUID, pane_id: PaneId) -> PaneCursor | None:
        if self._cursor_store is None:
            return None
        return await self._cursor_store.get_cursor(instance_id, pane_id)

    async def _connection(self, instance_id: UUID) -> LiveConnection:
        try:
            return await self._instances.get(instance_id)
        except InstanceOffline as exc:
            raise TermFlowToolError(
                TermFlowErrorCode.INTERNAL_ERROR, "the Term instance is offline"
            ) from exc

    async def _request_incarnation(self, instance_id: UUID, params: PaneReadParams) -> int:
        if params.cursor is not None:
            known = await self.resolve_cursor(instance_id, params.pane_id)
            if (
                known is not None
                and known.pane_incarnation != params.cursor.pane_incarnation
            ):
                raise TermFlowToolError(
                    TermFlowErrorCode.INCARNATION_CHANGED,
                    f"pane {params.pane_id} was replaced; the read cursor is stale",
                )
            return params.cursor.pane_incarnation
        known = await self.resolve_cursor(instance_id, params.pane_id)
        if known is not None:
            return known.pane_incarnation
        # Fresh pane default matching A-side incarnation semantics.
        return 1

    @staticmethod
    def _build_capture_request(
        instance_id: UUID,
        params: PaneReadParams,
        *,
        incarnation: int,
    ) -> PaneCaptureRequestPayload:
        if params.view is PaneReadView.VIEWPORT:
            capture_kind = CaptureKind.VIEWPORT
            start_line = end_line = tail_lines = None
            stream_id = seq = None
        elif params.view is PaneReadView.SINCE:
            # ``since`` reads are served from A's live ring addressed by a
            # cursor; the wire kind stays HISTORY with a stream/seq cursor
            # (plan §9.2).
            assert params.cursor is not None
            capture_kind = CaptureKind.HISTORY
            start_line = end_line = tail_lines = None
            stream_id = str(params.cursor.stream_id)
            seq = params.cursor.seq
        else:
            capture_kind = CaptureKind.HISTORY
            start_line = params.start_line
            end_line = params.end_line
            tail_lines = params.tail_lines
            stream_id = seq = None
        try:
            return PaneCaptureRequestPayload(
                instance_id=instance_id,
                pane_id=params.pane_id,
                capture_kind=capture_kind,
                request_id=uuid4(),
                start_line=start_line,
                end_line=end_line,
                tail_lines=tail_lines,
                max_bytes=params.max_bytes,
                join_wrapped=params.join_wrapped,
                stream_id=stream_id,
                seq=seq,
                pane_incarnation=incarnation,
            )
        except ValueError as exc:
            raise TermFlowToolError(
                TermFlowErrorCode.INVALID_REQUEST, str(exc)
            ) from exc

    @staticmethod
    def _map_capture_error(error: PaneCaptureErrorPayload) -> TermFlowToolError:
        code = _WIRE_ERROR_CODES.get(error.error_code, TermFlowErrorCode.INTERNAL_ERROR)
        return TermFlowToolError(code, error.message or f"capture failed: {error.error_code}")

    @staticmethod
    def _map_capture_result(
        instance_id: UUID,
        params: PaneReadParams,
        payload: PaneCaptureResultPayload,
    ) -> PaneReadResult:
        try:
            stream_id = UUID(payload.stream_id) if payload.stream_id else None
        except ValueError as exc:
            raise TermFlowToolError(
                TermFlowErrorCode.INTERNAL_ERROR,
                "the instance reported a malformed stream id",
            ) from exc
        stream_gap = None
        if payload.stream_gap:
            if not payload.stream_id:
                raise TermFlowToolError(
                    TermFlowErrorCode.STREAM_GAP,
                    "the instance reported a stream gap without a stream id",
                )
            reason: GapReason = cast(
                GapReason,
                payload.gap_reason if payload.gap_reason in _GAP_REASONS else "stream_changed",
            )
            assert stream_id is not None
            stream_gap = StreamGapInfo(
                pane_id=params.pane_id,
                previous_stream_id=stream_id,
                reason=reason,
            )
        content = payload.content
        truncated = payload.truncated
        if len(content.encode("utf-8")) > params.max_bytes:
            # Defensive bound: never hand the caller more than it asked for,
            # even when A misbehaves.  The newest tail is kept, mirroring A.
            raw = content.encode("utf-8")
            content = raw[-params.max_bytes :].decode("utf-8", errors="replace")
            truncated = True
        capture_range = payload.capture_range
        return PaneReadResult(
            instance_id=instance_id,
            pane_id=params.pane_id,
            view=params.view,
            content=content,
            stream_id=stream_id,
            from_seq=payload.from_seq,
            to_seq=payload.to_seq,
            captured_start_line=capture_range.start if capture_range is not None else None,
            captured_end_line=capture_range.end if capture_range is not None else None,
            truncated=truncated,
            stream_gap=stream_gap,
        )


class UnsupportedCommandService:
    """Concrete :class:`TerminalCommandPort` for the observe-only milestone.

    Writes are declared but not implemented: any call raises
    :class:`NotImplementedError`.  The M5 milestone replaces this service
    with the policy/approval-gated command router.
    """

    async def send_text(
        self, instance_id: UUID, params: PaneSendTextParams
    ) -> PaneSendTextResult:
        raise NotImplementedError(
            "pane writes land with M5; the M2 observe-only milestone declares "
            "TerminalCommandPort without an implementation"
        )

    async def send_keys(
        self, instance_id: UUID, params: PaneSendKeysParams
    ) -> PaneSendKeysResult:
        raise NotImplementedError(
            "pane writes land with M5; the M2 observe-only milestone declares "
            "TerminalCommandPort without an implementation"
        )


def _decode_watch_start(encoded: str) -> tuple[WatchCondition, PaneCursor | None]:
    """Decode the versioned creation envelope persisted in ``start_cursor``."""
    try:
        envelope = json.loads(encoded)
        if not isinstance(envelope, dict) or envelope.get("v") != 1:
            raise ValueError("unsupported watch start envelope")
        condition = WatchCondition.model_validate(envelope["condition"])
        cursor = None
        if envelope.get("cursor") is not None:
            cursor = PaneCursor.model_validate(envelope["cursor"])
        return condition, cursor
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise TermFlowToolError(
            TermFlowErrorCode.INTERNAL_ERROR, "persisted watch state is malformed"
        ) from exc


def _watch_status(state: str) -> WatchStatus:
    try:
        return _WATCH_STATUSES[state]
    except KeyError as exc:
        raise TermFlowToolError(
            TermFlowErrorCode.INTERNAL_ERROR,
            f"persisted watch state {state!r} is malformed",
        ) from exc


def _to_summary(watch: Watch, instance_id: UUID) -> WatchSummary:
    condition, _ = _decode_watch_start(watch.start_cursor)
    return WatchSummary(
        watch_id=watch.id,
        conversation_id=watch.conversation_id,
        instance_id=instance_id,
        pane_id=watch.pane_id,
        condition=condition,
        status=_watch_status(watch.state),
        generation=watch.watch_generation,
        created_at=watch.created_at,
    )


def _to_detail(watch: Watch, instance_id: UUID) -> WatchDetail:
    condition, start_cursor = _decode_watch_start(watch.start_cursor)
    summary = _to_summary(watch, instance_id)
    return WatchDetail(
        watch_id=summary.watch_id,
        conversation_id=summary.conversation_id,
        instance_id=summary.instance_id,
        pane_id=summary.pane_id,
        condition=summary.condition,
        status=summary.status,
        generation=summary.generation,
        created_at=summary.created_at,
        start_cursor=start_cursor,
        expires_at=watch.expiry_at,
        one_shot=watch.one_shot,
    )


class WatchContinuationService:
    """Concrete :class:`ContinuationPort` over the M1.3 watch repositories.

    Every watch is scoped to its owning binding: lookups for other bindings
    are indistinguishable from missing watches (``watch_not_found``).  The
    creation contract (condition + start cursor) is persisted as a versioned
    envelope in ``start_cursor`` so list/get can round-trip the bounded
    condition fields; the M3 engine owns the production format.
    """

    def __init__(
        self,
        watches: WatchRepository,
        bindings: AgentBindingRepository,
    ) -> None:
        self._watches = watches
        self._bindings = bindings

    async def create_watch(
        self, binding_id: UUID, params: WatchCreateParams
    ) -> WatchCreateResult:
        envelope = json.dumps(
            {
                "v": 1,
                "condition": params.condition.model_dump(mode="json"),
                "cursor": (
                    params.start_cursor.model_dump(mode="json")
                    if params.start_cursor is not None
                    else None
                ),
            },
            separators=(",", ":"),
        )
        watch = await self._watches.create(
            binding_id=binding_id,
            conversation_id=params.conversation_id,
            pane_id=params.pane_id,
            condition_kind=params.condition.kind.value,
            start_cursor=envelope,
            intent_summary=params.intent or params.condition.kind.value,
            watch_generation=0,
            rearm_cursor=None,
            expiry_at=params.expires_at,
            one_shot=params.one_shot,
            state="active",
        )
        return WatchCreateResult(
            watch_id=watch.id,
            generation=watch.watch_generation,
            start_cursor=params.start_cursor,
            expires_at=watch.expiry_at,
        )

    async def list_watches(
        self, binding_id: UUID, params: WatchListParams | None = None
    ) -> WatchListResult:
        rows = await self._watches.list_for_binding(binding_id)
        summaries: list[WatchSummary] = []
        for watch in rows:
            if params is not None and params.conversation_id is not None:
                if watch.conversation_id != params.conversation_id:
                    continue
            if params is not None and params.status is not None:
                if _watch_status(watch.state) is not params.status:
                    continue
            summaries.append(_to_summary(watch, await self._instance_id(watch.binding_id)))
        return WatchListResult(watches=summaries)

    async def get_watch(self, binding_id: UUID, params: WatchGetParams) -> WatchGetResult:
        watch = await self._watches.get_by_id(params.watch_id)
        if watch is None or watch.binding_id != binding_id:
            raise TermFlowToolError(
                TermFlowErrorCode.WATCH_NOT_FOUND, "the watch does not exist for this binding"
            )
        return WatchGetResult(watch=_to_detail(watch, await self._instance_id(watch.binding_id)))

    async def cancel_watch(
        self, binding_id: UUID, params: WatchCancelParams
    ) -> WatchCancelResult:
        watch = await self._watches.get_by_id(params.watch_id)
        if watch is None or watch.binding_id != binding_id:
            raise TermFlowToolError(
                TermFlowErrorCode.WATCH_NOT_FOUND, "the watch does not exist for this binding"
            )
        cancelled = await self._watches.cancel(params.watch_id)
        if cancelled is None:
            # Already inactive (cancelled/expired/triggered): cancellation is
            # idempotent, the watch can no longer fire either way.
            logger.info("watch %s was already inactive; cancel is a no-op", params.watch_id)
        return WatchCancelResult(watch_id=params.watch_id, ok=True)

    async def _instance_id(self, binding_id: UUID) -> UUID:
        binding = await self._bindings.get_by_id(binding_id)
        if binding is None:
            raise TermFlowToolError(
                TermFlowErrorCode.INTERNAL_ERROR,
                "the watch owning binding no longer exists",
            )
        return binding.term_id
