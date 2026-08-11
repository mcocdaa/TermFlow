"""Immutable capability matrix and structural contract for Agent Backends.

A backend adapter implements :class:`AgentBackend`.  Capabilities are
negotiated per binding/runtime epoch and captured as an immutable descriptor
so scheduler behaviour is driven by explicit semantic modes rather than a
growing list of booleans.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, Field
from termflow_protocol.agent import AgentInputKind

from .turns import (
    BackendCancelRequest,
    BackendConversationRef,
    BackendConversationSnapshot,
    BackendEventScope,
    BackendInteraction,
    BackendModel,
    BackendNotification,
    BackendOperationResult,
    BackendSubmitResult,
    BackendTurnRequest,
    CreateBackendConversation,
)


class ContextMode(StrEnum):
    """How a backend preserves context across restarts."""

    RESUME = "resume"
    FORK_ONLY = "fork_only"
    LOST_ON_RESTART = "lost_on_restart"


class SubmitMode(StrEnum):
    """Whether ``submit`` guarantees idempotent admission."""

    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    UNKNOWN = "unknown"


class CancelScope(StrEnum):
    """The narrowest unit a backend can cancel."""

    RUN = "run"
    CONVERSATION = "conversation"
    NONE = "none"


class ReplayMode(StrEnum):
    """What a backend can replay after a subscription disconnect."""

    EVENT = "event"
    SESSION = "session"
    NONE = "none"


class ConcurrencyMode(StrEnum):
    """How a backend serializes turns within one binding."""

    SERIALIZED = "serialized"
    PARALLEL = "parallel"


class ToolCallIdentity(StrEnum):
    """The scope in which backend tool-call IDs are unique."""

    BINDING = "binding"
    CONVERSATION = "conversation"
    RUN = "run"


class RuntimeIsolation(StrEnum):
    """The isolation boundary between backend runtimes."""

    BINDING = "binding"
    CONVERSATION = "conversation"
    TENANT = "tenant"


class StructuredPartFidelity(StrEnum):
    """How faithfully a backend preserves structured message parts."""

    FULL = "full"
    TEXT_ONLY = "text_only"
    NONE = "none"


class AgentBackendCapabilities(BackendModel):
    """Immutable, negotiated capability descriptor for one binding/runtime epoch."""

    model_config = ConfigDict(frozen=True)

    streaming_output: bool = False
    context_mode: ContextMode
    submit_mode: SubmitMode
    cancel_scope: CancelScope
    replay_mode: ReplayMode
    concurrency_mode: ConcurrencyMode
    tool_call_identity: ToolCallIdentity
    runtime_isolation: RuntimeIsolation
    accepted_input_kinds: tuple[AgentInputKind, ...] = Field(default_factory=tuple)
    structured_part_fidelity: StructuredPartFidelity = StructuredPartFidelity.NONE
    tool_activity_events: bool = False
    permission_events: bool = False
    usage_cost_reporting: bool = False
    explicit_run_boundaries: bool = False


@runtime_checkable
class AgentBackend(Protocol):
    """Structural contract every backend adapter must satisfy.

    ``events()`` reconnects from the backend's live stream and does not
    promise provider replay; ``reconcile()`` proves message/run state after a
    disconnect.  ``submit()`` reports only admission/transport acceptance.
    """

    async def capabilities(self) -> AgentBackendCapabilities: ...

    async def create_conversation(
        self, request: CreateBackendConversation
    ) -> BackendConversationRef: ...

    async def submit(
        self, ref: BackendConversationRef, request: BackendTurnRequest
    ) -> BackendSubmitResult: ...

    async def cancel(self, request: BackendCancelRequest) -> BackendOperationResult: ...

    def events(self, scope: BackendEventScope) -> AsyncIterator[BackendNotification]: ...

    async def reconcile(self, ref: BackendConversationRef) -> BackendConversationSnapshot: ...

    async def interact(self, request: BackendInteraction) -> BackendOperationResult: ...

    async def delete_conversation(self, ref: BackendConversationRef) -> BackendOperationResult: ...

    async def close(self) -> None: ...
