"""Typed contract definitions for the B feature plugin surface.

This module defines the structural contracts (``Protocol``) and minimal
concrete registries that B feature plugins bind against.  It carries no
runtime wiring: registration, routing, and context assembly live in the
plugin registry and composition root workstreams.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import NewType, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

_AGENT_ROUTE_PREFIX = "/api/v1/agent"

MAX_EVIDENCE_ITEMS = 32


def _require_non_empty(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


class ContractModel(BaseModel):
    """Strict data shapes so contract drift fails loudly."""

    model_config = ConfigDict(extra="forbid")


# Opaque backend identifiers.  Callers must never interpret these values.
RuntimeRef = NewType("RuntimeRef", str)
CapabilityRef = NewType("CapabilityRef", str)


class RuntimeStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"


class DrainStatus(StrEnum):
    DRAINED = "drained"
    DRAIN_TIMEOUT = "drain_timeout"
    NOT_ATTEMPTED = "not_attempted"


class BackendOperationOutcome(StrEnum):
    CONFIRMED = "confirmed"
    REQUESTED = "requested"
    UNSUPPORTED = "unsupported"
    RETRYABLE = "retryable"
    CONTEXT_LOST = "context_lost"
    UNKNOWN = "unknown"


class SubscriptionScopeKind(StrEnum):
    GLOBAL = "global"
    BINDING = "binding"
    RUNTIME = "runtime"


class RuntimeHealth(ContractModel):
    status: RuntimeStatus
    epoch: int = Field(ge=1)
    observed_at: datetime
    detail: str = ""


class EvidenceRecord(ContractModel):
    key: str
    value: str
    detail: str = ""

    @field_validator("key")
    @classmethod
    def _require_key(cls, value: str) -> str:
        _require_non_empty(value, "evidence key")
        return value


class BackendOperationResult(ContractModel):
    outcome: BackendOperationOutcome
    message: str = ""
    evidence: tuple[EvidenceRecord, ...] = ()

    @field_validator("evidence")
    @classmethod
    def _bound_evidence(
        cls, value: tuple[EvidenceRecord, ...]
    ) -> tuple[EvidenceRecord, ...]:
        if len(value) > MAX_EVIDENCE_ITEMS:
            raise ValueError(f"evidence must not exceed {MAX_EVIDENCE_ITEMS} records")
        return value


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    """Auth/ACL/CSRF requirements attached to a plugin-owned route."""

    auth_required: bool = True
    acls: tuple[str, ...] = ()
    csrf_required: bool = True


@dataclass(frozen=True, slots=True)
class RouteEntry:
    method: str
    path: str
    handler: Callable[..., object]
    owner: str
    policy: RoutePolicy


@runtime_checkable
class FeatureRouteRegistry(Protocol):
    """Registration surface enforcing the ``/api/v1/agent`` plugin namespace."""

    def add_route(
        self,
        path: str,
        *,
        method: str,
        handler: Callable[..., object],
        owner: str,
        policy: RoutePolicy,
    ) -> None: ...

    def routes(self) -> tuple[RouteEntry, ...]: ...


class InMemoryFeatureRouteRegistry:
    """Minimal concrete registry for tests and fake composition roots."""

    def __init__(self) -> None:
        self._routes: list[RouteEntry] = []

    def add_route(
        self,
        path: str,
        *,
        method: str,
        handler: Callable[..., object],
        owner: str,
        policy: RoutePolicy,
    ) -> None:
        _require_non_empty(owner, "route owner")
        _require_non_empty(method, "HTTP method")
        if path != _AGENT_ROUTE_PREFIX and not path.startswith(_AGENT_ROUTE_PREFIX + "/"):
            raise ValueError(
                f"plugin routes must live under {_AGENT_ROUTE_PREFIX}, got {path!r}"
            )
        self._routes.append(
            RouteEntry(method=method, path=path, handler=handler, owner=owner, policy=policy)
        )

    def routes(self) -> tuple[RouteEntry, ...]:
        return tuple(self._routes)


@dataclass(frozen=True, slots=True)
class SubscriptionScope:
    kind: SubscriptionScopeKind
    binding_id: str | None = None
    runtime_ref: RuntimeRef | None = None

    def __post_init__(self) -> None:
        if self.kind is SubscriptionScopeKind.BINDING and self.binding_id is None:
            raise ValueError("binding-scoped subscriptions require a binding_id")
        if self.kind is SubscriptionScopeKind.RUNTIME and self.runtime_ref is None:
            raise ValueError("runtime-scoped subscriptions require a runtime_ref")
        if self.kind is SubscriptionScopeKind.GLOBAL and (
            self.binding_id is not None or self.runtime_ref is not None
        ):
            raise ValueError("global-scoped subscriptions cannot carry a binding or runtime")


@dataclass(frozen=True, slots=True)
class EventSubscription:
    owner: str
    scope: SubscriptionScope
    handler: Callable[[object], Awaitable[None]]
    id: UUID = field(default_factory=uuid4)


@runtime_checkable
class EventSubscriptionRegistry(Protocol):
    """Registration surface for owner- and scope-scoped event subscriptions."""

    def subscribe(
        self,
        owner: str,
        scope: SubscriptionScope,
        handler: Callable[[object], Awaitable[None]],
    ) -> EventSubscription: ...

    def subscriptions(self) -> tuple[EventSubscription, ...]: ...


class InMemoryEventSubscriptionRegistry:
    """Minimal concrete registry for tests and fake composition roots."""

    def __init__(self) -> None:
        self._subscriptions: list[EventSubscription] = []

    def subscribe(
        self,
        owner: str,
        scope: SubscriptionScope,
        handler: Callable[[object], Awaitable[None]],
    ) -> EventSubscription:
        _require_non_empty(owner, "subscription owner")
        if not isinstance(scope, SubscriptionScope):
            raise TypeError("subscriptions require a typed SubscriptionScope")
        if not callable(handler):
            raise TypeError("subscription handler must be callable")
        subscription = EventSubscription(owner=owner, scope=scope, handler=handler)
        self._subscriptions.append(subscription)
        return subscription

    def subscriptions(self) -> tuple[EventSubscription, ...]:
        return tuple(self._subscriptions)


@dataclass(frozen=True, slots=True)
class MigrationRevision:
    id: str
    owner: str
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "migration revision id")
        _require_non_empty(self.owner, "migration revision owner")


@runtime_checkable
class MigrationManifest(Protocol):
    """Static migration manifest that records owner-scoped revisions."""

    def add_revision(self, revision: MigrationRevision) -> None: ...

    def revisions(self) -> tuple[MigrationRevision, ...]: ...


class StaticMigrationManifest:
    """Concrete manifest rejecting duplicate revision ids."""

    def __init__(self) -> None:
        self._revisions: dict[str, MigrationRevision] = {}

    def add_revision(self, revision: MigrationRevision) -> None:
        if revision.id in self._revisions:
            raise ValueError(f"duplicate migration revision id: {revision.id!r}")
        self._revisions[revision.id] = revision

    def revisions(self) -> tuple[MigrationRevision, ...]:
        return tuple(self._revisions.values())


@runtime_checkable
class AuthPort(Protocol):
    """Authentication/authorization boundary for B feature plugins."""

    async def authenticate(self, token: str) -> object: ...

    async def authorize(
        self, permission: str, *, resource: object | None = None
    ) -> bool: ...


@runtime_checkable
class TermPort(Protocol):
    """Read access to terminal terms owned by the deployment."""

    async def get_term(self, term_id: str) -> object: ...

    async def list_terms(self) -> tuple[object, ...]: ...


@runtime_checkable
class TerminalObservationPort(Protocol):
    """Bounded capture/observation of terminal pane output."""

    async def capture(self, pane_ref: str, *, max_bytes: int) -> object: ...

    async def watch(
        self, pane_ref: str, handler: Callable[[object], Awaitable[None]]
    ) -> object: ...


@runtime_checkable
class TerminalCommandPort(Protocol):
    """Policy-gated terminal input with typed receipts."""

    async def send(self, pane_ref: str, text: str, *, timeout_seconds: float) -> object: ...


@runtime_checkable
class UnitOfWorkFactory(Protocol):
    """Factory for opaque database transaction boundaries."""

    def create(self) -> object: ...


@runtime_checkable
class LifecyclePort(Protocol):
    """Registration surface for plugin-managed lifecycle callbacks."""

    def on_startup(self, callback: Callable[[], Awaitable[None]]) -> None: ...

    def on_shutdown(self, callback: Callable[[], Awaitable[None]]) -> None: ...


@runtime_checkable
class AgentRuntimeSupervisor(Protocol):
    """Lifecycle authority over binding-scoped agent backend runtimes."""

    async def register(
        self,
        binding_id: str,
        runtime_ref: RuntimeRef,
        epoch: int,
        capability_ref: CapabilityRef,
    ) -> None: ...

    async def health(self, runtime_ref: RuntimeRef) -> RuntimeHealth: ...

    async def quiesce(self, runtime_ref: RuntimeRef, deadline: datetime) -> DrainStatus: ...

    async def restart(
        self,
        runtime_ref: RuntimeRef,
        epoch: int,
        capability_ref: CapabilityRef,
    ) -> RuntimeHealth: ...

    async def cleanup(self, runtime_ref: RuntimeRef) -> BackendOperationResult: ...


@runtime_checkable
class BFeatureContext(Protocol):
    """Dependency surface handed to a B feature plugin at registration time."""

    auth: AuthPort
    terms: TermPort
    observation: TerminalObservationPort
    commands: TerminalCommandPort
    persistence: UnitOfWorkFactory
    lifecycle: LifecyclePort
    runtime: AgentRuntimeSupervisor


@runtime_checkable
class BFeaturePlugin(Protocol):
    """Contract every B feature plugin must satisfy."""

    id: str
    version: str
    requires_core_api: str

    def register_services(self, context: BFeatureContext) -> None: ...

    def register_routes(self, routes: FeatureRouteRegistry) -> None: ...

    def register_event_handlers(self, subscriptions: EventSubscriptionRegistry) -> None: ...

    def register_migrations(self, manifest: MigrationManifest) -> None: ...

    async def startup(self, context: BFeatureContext) -> None: ...

    async def shutdown(self) -> None: ...
