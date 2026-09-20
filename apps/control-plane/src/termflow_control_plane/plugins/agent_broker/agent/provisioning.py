"""Atomic product setup workflow for Agent Broker.

The service intentionally owns the database transaction.  Repository helper
methods are useful for ordinary mutations (each opens its own session), but
setup must either materialise the complete aggregate or leave no rows behind.
External runtime work starts only after the transaction commits.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import delete, func, select, update

from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentProfile,
    AgentProviderDisclosureAcceptance,
    AgentRuntimeBinding,
    AgentSetupReceipt,
    AgentToken,
    Instance,
    PanePolicy,
)
from termflow_control_plane.persistence.repositories import AgentSetupReceiptRepository
from termflow_control_plane.plugins.agent_broker.agent.provider_catalog import (
    ProviderCatalog,
    canonicalize_profile_config,
)


@dataclass(frozen=True, slots=True)
class AgentSetupCommand:
    term_id: UUID
    profile_id: UUID | None
    profile_display_name: str | None
    pane_ids: tuple[str, ...]
    topology_revision: int
    disclosure_fingerprint: str
    accepted: bool
    idempotency_key: UUID


@dataclass(frozen=True, slots=True)
class AgentSetupResult:
    state: str
    term_id: UUID
    binding_id: UUID | None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSetupView:
    state: str
    term_id: UUID
    binding_id: UUID | None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRuntimeAssignment:
    """Server-owned desired runtime identity written during setup.

    Clients never provide any of these values.  The composition root may
    inject a provider for a multi-runtime deployment; the reference
    deployment uses the deterministic ``opencode-agent`` default below.
    """

    runtime_ref: str
    runtime_epoch: int
    capability_ref: str


RuntimeAssignmentSource = Callable[[UUID], object | Awaitable[object]]


def _auth_value(auth: Any, name: str, default: Any = None) -> Any:
    if isinstance(auth, dict):
        return auth.get(name, default)
    return getattr(auth, name, default)


class AgentProvisioningService:
    """Create the canonical setup aggregate and request bounded reconcile."""

    def __init__(
        self,
        repositories: Any,
        sessions: Any | None = None,
        topology: Any | None = None,
        catalog: ProviderCatalog | Any | None = None,
        bootstrap_secret: Any | None = None,
        controller: Any | None = None,
        runtime_assignment: RuntimeAssignmentSource | object | None = None,
    ) -> None:
        self._repositories = repositories
        sessions_factory = sessions or getattr(repositories, "_sessions", None)
        if sessions_factory is None:
            raise ValueError("repositories must expose a session factory")
        self._sessions: Any = sessions_factory
        self._topology = topology
        self._catalog = catalog
        self._bootstrap_secret = bootstrap_secret
        self._controller = controller
        self._runtime_assignment = runtime_assignment

    async def _topology_snapshot(self, term_id: UUID) -> tuple[int, set[str]]:
        source = self._topology
        value: Any = None
        if source is not None:
            if callable(source):
                value = source(term_id)
                if inspect.isawaitable(value):
                    value = await value
            elif hasattr(source, "snapshot"):
                value = source.snapshot(term_id)
                if inspect.isawaitable(value):
                    value = await value
            elif isinstance(source, dict):
                value = source.get(term_id)
        if value is None:
            raise TermFlowError("topology_unavailable", 409, "Term topology is unavailable.")
        if isinstance(value, dict):
            revision = value.get("revision", value.get("topology_revision"))
            panes = value.get("pane_ids", value.get("panes", ()))
        else:
            revision = getattr(value, "revision", getattr(value, "topology_revision", None))
            panes = getattr(value, "pane_ids", None)
            if panes is None and hasattr(value, "windows"):
                panes = [p.pane_id for w in value.windows for p in w.panes]
        try:
            normalized_panes = {
                str(p.pane_id if hasattr(p, "pane_id") else p) for p in (panes or ())
            }
            if revision is None:
                raise ValueError("missing revision")
            return int(revision), normalized_panes
        except (TypeError, ValueError):
            raise TermFlowError(
                "topology_unavailable", 409, "Term topology is unavailable."
            ) from None

    async def _resolve_bootstrap_secret(self, term_id: UUID) -> str | bytes | None:
        """Resolve one deployment secret without ever persisting its value.

        The composition root normally supplies a :class:`SecretStr`, a
        term-scoped mapping, or a synchronous/asynchronous provider.  Resolve
        each layer explicitly so a coroutine is never mistaken for a missing
        secret (and never left unawaited).  A bounded number of layers keeps a
        malicious/self-referential provider from creating an infinite loop.
        """

        source: Any = self._bootstrap_secret
        for _ in range(8):
            if source is None:
                return None
            if inspect.isawaitable(source):
                try:
                    source = await source
                except Exception:
                    return None
                continue
            if isinstance(source, SecretStr):
                source = source.get_secret_value()
                continue
            if isinstance(source, Mapping):
                source = source.get(term_id, source.get(str(term_id)))
                continue
            method = next(
                (
                    getattr(source, name, None)
                    for name in ("get_secret", "secret_for", "resolve")
                    if callable(getattr(source, name, None))
                ),
                None,
            )
            if method is not None:
                try:
                    source = method(term_id)
                except Exception:
                    return None
                continue
            if callable(source):
                try:
                    source = source(term_id)
                except Exception:
                    return None
                continue
            if isinstance(source, (str, bytes)):
                return source if bool(source) else None
            return None
        return None

    async def _bootstrap_available(self, term_id: UUID) -> bool:
        return await self._resolve_bootstrap_secret(term_id) is not None

    @staticmethod
    def _default_runtime_assignment(binding_id: UUID) -> AgentRuntimeAssignment:
        return AgentRuntimeAssignment(
            runtime_ref="opencode-agent",
            runtime_epoch=1,
            capability_ref=f"termflow-mcp:{binding_id}",
        )

    async def _resolve_runtime_assignment(
        self,
        term_id: UUID,
        binding_id: UUID,
    ) -> AgentRuntimeAssignment:
        """Resolve and validate server-owned runtime identity.

        A missing injection is intentionally usable for the reference
        single-runtime deployment.  Custom providers are still fail-closed:
        malformed or unavailable assignments become a stable public conflict,
        never a partially populated Binding.
        """

        source = self._runtime_assignment
        if source is None:
            return self._default_runtime_assignment(binding_id)
        try:
            value: Any = source
            if callable(value):
                value = value(term_id)
            else:
                # Permit a small object-provider seam without coupling the
                # service to a concrete settings class.  These names are
                # server-side only; the request never controls the lookup.
                method = next(
                    (
                        getattr(value, name, None)
                        for name in ("for_term", "get_assignment", "resolve")
                        if callable(getattr(value, name, None))
                    ),
                    None,
                )
                if method is not None:
                    value = method(term_id)
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            raise TermFlowError(
                "runtime_assignment_conflict",
                409,
                "The Agent runtime assignment is unavailable.",
            ) from None

        runtime_ref: Any = None
        runtime_epoch: Any = None
        capability_ref: Any = None
        if isinstance(value, Mapping):
            runtime_ref = value.get("runtime_ref")
            runtime_epoch = value.get("runtime_epoch")
            capability_ref = value.get("capability_ref")
        elif isinstance(value, (tuple, list)) and len(value) == 3:
            runtime_ref, runtime_epoch, capability_ref = value
        else:
            runtime_ref = getattr(value, "runtime_ref", None)
            runtime_epoch = getattr(value, "runtime_epoch", None)
            capability_ref = getattr(value, "capability_ref", None)

        if (
            not isinstance(runtime_ref, str)
            or not runtime_ref
            or runtime_ref != runtime_ref.strip()
            or len(runtime_ref) > 128
            or isinstance(runtime_epoch, bool)
            or not isinstance(runtime_epoch, int)
            or runtime_epoch < 1
            or not isinstance(capability_ref, str)
            or not capability_ref
            or capability_ref != capability_ref.strip()
            or len(capability_ref) > 128
        ):
            raise TermFlowError(
                "runtime_assignment_conflict",
                409,
                "The Agent runtime assignment is invalid.",
            )
        return AgentRuntimeAssignment(runtime_ref, runtime_epoch, capability_ref)

    @staticmethod
    def _secret_hash(secret: str | bytes) -> str:
        return hashlib.sha256(secret.encode() if isinstance(secret, str) else secret).hexdigest()

    @staticmethod
    def _request_digest(command: AgentSetupCommand) -> str:
        """Hash only deterministic request fields, never credentials."""

        payload = {
            "term_id": str(command.term_id),
            "profile_id": str(command.profile_id) if command.profile_id is not None else None,
            "profile_display_name": command.profile_display_name,
            "pane_ids": list(command.pane_ids),
            "topology_revision": command.topology_revision,
            "disclosure_fingerprint": command.disclosure_fingerprint,
            "accepted": command.accepted,
            "idempotency_key": str(command.idempotency_key),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _result_from_receipt(receipt: AgentSetupReceipt) -> AgentSetupResult:
        return AgentSetupResult(
            state=receipt.state,
            term_id=receipt.term_id,
            binding_id=receipt.binding_id,
            reason_code=receipt.reason_code,
        )

    @staticmethod
    def _stable_runtime_reason(error: BaseException) -> str:
        """Translate controller failures to the public reason vocabulary."""

        # ``TermFlowError`` keeps its stable code separately from its safe
        # human message; inspect both so a generic message cannot collapse a
        # known controller reason to ``runtime_unreachable``.
        detail = f"{getattr(error, 'code', '')} {error}".lower()
        if "recovery_failed" in detail:
            return "recovery_failed"
        if "mcp_not_connected" in detail:
            return "mcp_not_connected"
        if "pipeline_start_failed" in detail or "pipeline" in detail or "start" in detail:
            return "pipeline_start_failed"
        if "runtime_assignment_conflict" in detail or "assignment" in detail:
            return "runtime_assignment_conflict"
        return "runtime_unreachable"

    @classmethod
    def _result_from_controller(
        cls,
        _binding_id: UUID,
        outcome: Any,
    ) -> tuple[str, str | None]:
        readiness = str(getattr(outcome, "readiness", getattr(outcome, "state", "")))
        if outcome is None or readiness in {"", "activating", "reconciling"}:
            return "activating", None
        reason = getattr(outcome, "reason_code", None)
        if readiness == "ready":
            return "ready", None
        if reason is not None:
            reason = str(reason)
        if reason not in {
            "runtime_unreachable",
            "mcp_not_connected",
            "pipeline_start_failed",
            "recovery_failed",
            "runtime_assignment_conflict",
        }:
            reason = "runtime_unreachable"
        # A controller can report a blocked/not-ready outcome while the setup
        # contract exposes only activating/ready/unavailable.
        return "unavailable", reason

    async def _lookup_receipt(
        self,
        key: UUID,
        request_digest: str,
    ) -> AgentSetupResult | None:
        receipts = getattr(self._repositories, "agent_setup_receipts", None)
        receipt: AgentSetupReceipt | None
        if receipts is not None:
            receipt = cast(AgentSetupReceipt | None, await receipts.get_by_idempotency_key(key))
        else:
            async with self._sessions() as session:
                receipt = cast(
                    AgentSetupReceipt | None,
                    await session.scalar(
                        select(AgentSetupReceipt).where(AgentSetupReceipt.idempotency_key == key)
                    ),
                )
        if receipt is None:
            return None
        if not secrets.compare_digest(receipt.request_digest, request_digest):
            raise TermFlowError("idempotency_conflict", 409, "Idempotency key was reused.")
        return self._result_from_receipt(receipt)

    async def _record_receipt_result(
        self,
        key: UUID,
        *,
        state: str,
        binding_id: UUID | None,
        reason_code: str | None,
    ) -> AgentSetupReceipt | None:
        receipts = getattr(self._repositories, "agent_setup_receipts", None)
        if receipts is not None:
            return cast(
                AgentSetupReceipt | None,
                await receipts.record_result(
                    key,
                    state=state,
                    binding_id=binding_id,
                    reason_code=reason_code,
                ),
            )
        async with self._sessions() as session:
            result = await session.execute(
                select(AgentSetupReceipt).where(AgentSetupReceipt.idempotency_key == key)
            )
            receipt = cast(AgentSetupReceipt | None, result.scalar_one_or_none())
            if receipt is None:
                return None
            if receipt.state == "activating":
                receipt.state = state
                receipt.binding_id = binding_id
                receipt.reason_code = reason_code
                receipt.updated_at = datetime.now(UTC)
            await session.commit()
            return receipt

    async def setup(self, command: AgentSetupCommand, auth: Any) -> AgentSetupResult:
        if command.accepted is not True:
            raise TermFlowError("disclosure_required", 422, "Provider disclosure must be accepted.")
        if (command.profile_id is None) == (command.profile_display_name is None):
            raise TermFlowError(
                "profile_selector_required", 422, "Select exactly one Agent Profile."
            )
        if not command.pane_ids or len(set(command.pane_ids)) != len(command.pane_ids):
            raise TermFlowError(
                "invalid_pane_policy", 422, "Pane IDs must be unique and non-empty."
            )
        try:
            key = UUID(str(command.idempotency_key))
        except (TypeError, ValueError):
            raise TermFlowError(
                "invalid_idempotency_key", 422, "Idempotency key is invalid."
            ) from None
        request_digest = self._request_digest(command)
        prior = await self._lookup_receipt(key, request_digest)
        if prior is not None:
            return prior

        observed_revision, available_panes = await self._topology_snapshot(command.term_id)
        if observed_revision != command.topology_revision:
            raise TermFlowError("stale_topology", 409, "Term topology revision is stale.")
        if not set(command.pane_ids) <= available_panes:
            raise TermFlowError("invalid_pane_policy", 422, "Pane policy contains an unknown Pane.")
        # Resolve once before opening the aggregate transaction.  This keeps
        # provider calls outside the DB lock while still ensuring the exact
        # secret hashed below is the one that was availability-checked.
        bootstrap_secret = await self._resolve_bootstrap_secret(command.term_id)
        if bootstrap_secret is None:
            raise TermFlowError("deployment_required", 409, "Agent runtime deployment is required.")

        actor_kind = str(
            _auth_value(auth, "credential_kind", _auth_value(auth, "actor_kind", "admin"))
        )
        actor_ref = str(_auth_value(auth, "actor_ref", "admin"))
        auth_epoch = int(_auth_value(auth, "auth_epoch", 1) or 1)
        now = datetime.now(UTC)

        async with self._sessions() as session:
            receipt, owns_receipt = await AgentSetupReceiptRepository.claim_in_session(
                session,
                idempotency_key=key,
                request_digest=request_digest,
                term_id=command.term_id,
            )
            if not owns_receipt:
                if not secrets.compare_digest(receipt.request_digest, request_digest):
                    await session.rollback()
                    raise TermFlowError("idempotency_conflict", 409, "Idempotency key was reused.")
                result = self._result_from_receipt(receipt)
                await session.rollback()
                return result

            term = await session.get(Instance, command.term_id)
            if term is None or term.revoked_at is not None:
                raise TermFlowError("term_not_found", 404, "Term was not found.")
            profile: AgentProfile | None
            if command.profile_id is not None:
                profile = await session.get(AgentProfile, command.profile_id)
            else:
                profile = await session.scalar(
                    select(AgentProfile).where(
                        AgentProfile.display_name == command.profile_display_name
                    )
                )
                # A display-name selector may provision a new canonical
                # profile.  Catalogs normally expose a deployment default;
                # retain a strict fail-closed fallback when they do not.
                if profile is None and self._catalog is not None:
                    default_config = self._catalog.default_config()
                    if default_config is not None:
                        profile = AgentProfile(
                            display_name=str(command.profile_display_name),
                            backend_kind="opencode",
                            config=json.dumps(
                                default_config.model_dump(mode="json"),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )
                        session.add(profile)
                        await session.flush()
            if profile is None:
                raise TermFlowError("profile_not_found", 404, "Agent Profile was not found.")
            config, encoded_config = canonicalize_profile_config(profile.config)
            profile_was_noncanonical = profile.config != encoded_config
            if profile_was_noncanonical:
                # Persist the validated canonical representation in the same
                # setup transaction so future digest/provider lookups cannot
                # observe two encodings of one profile.
                profile.config = encoded_config
                profile.updated_at = now
            if self._catalog is None:
                raise TermFlowError(
                    "unknown_provider", 422, "The Agent Profile provider or model is unavailable."
                )
            entry = self._catalog.resolve(config)
            expected_fp = self._catalog.disclosure_fingerprint(config)
            if not secrets.compare_digest(expected_fp, command.disclosure_fingerprint):
                raise TermFlowError(
                    "disclosure_mismatch", 409, "Provider disclosure fingerprint is stale."
                )

            # A setup command addresses the selected Profile/Term pair.  The
            # schema intentionally permits multiple Profiles on one Term, so
            # reconfiguration of this pair must reuse its existing Binding;
            # selecting another Profile creates (or reuses) that pair's own
            # Binding rather than silently rewriting a different one.
            binding = await session.scalar(
                select(AgentBinding)
                .where(
                    AgentBinding.profile_id == profile.id,
                    AgentBinding.term_id == term.id,
                    # ``pending``/``ready`` are accepted only as a narrow
                    # compatibility bridge for databases that have not yet
                    # completed the 0011 status migration; new writes always
                    # normalize them to ``enabled`` below.
                    AgentBinding.status.in_(("disabled", "enabled", "pending", "ready")),
                )
                .order_by(AgentBinding.created_at.desc(), AgentBinding.id.desc())
                .limit(1)
            )
            is_new_binding = binding is None
            if binding is None:
                binding = AgentBinding(
                    profile_id=profile.id,
                    term_id=term.id,
                    status="enabled",
                    config_revision=1,
                )
                session.add(binding)
                await session.flush()

            previous_panes = set(
                await session.scalars(
                    select(PanePolicy.pane_id).where(
                        PanePolicy.binding_id == binding.id,
                        PanePolicy.allowed.is_(True),
                    )
                )
            )
            current_disclosures = list(
                await session.scalars(
                    select(AgentProviderDisclosureAcceptance)
                    .where(
                        AgentProviderDisclosureAcceptance.binding_id == binding.id,
                        AgentProviderDisclosureAcceptance.revoked_at.is_(None),
                    )
                    .order_by(
                        AgentProviderDisclosureAcceptance.accepted_at.desc(),
                        AgentProviderDisclosureAcceptance.id.desc(),
                    )
                )
            )
            current_fingerprints = {
                disclosure.disclosure_fingerprint for disclosure in current_disclosures
            }

            # Runtime identity is server-owned.  Resolve an injected provider
            # for every explicit composition-root assignment; otherwise fill
            # only missing fields with the deterministic reference assignment.
            previous_runtime_epoch = binding.runtime_epoch
            assignment: AgentRuntimeAssignment | None = None
            if (
                is_new_binding
                or self._runtime_assignment is not None
                or not all(
                    (
                        binding.runtime_ref,
                        binding.runtime_epoch is not None,
                        binding.capability_ref,
                    )
                )
            ):
                assignment = await self._resolve_runtime_assignment(
                    command.term_id,
                    binding.id,
                )
            if assignment is not None and not is_new_binding:
                identity_changed = (
                    binding.runtime_ref != assignment.runtime_ref
                    or binding.runtime_epoch != assignment.runtime_epoch
                    or binding.capability_ref != assignment.capability_ref
                )
                if identity_changed and binding.runtime_epoch is not None:
                    # A replacement/capability change is an authority change;
                    # never permit a provider to move an existing Binding back
                    # to an old epoch.  Initial assignment (all fields NULL)
                    # retains the provider's declared epoch.
                    if (
                        assignment.runtime_ref != binding.runtime_ref
                        or assignment.capability_ref != binding.capability_ref
                    ):
                        assignment = AgentRuntimeAssignment(
                            runtime_ref=assignment.runtime_ref,
                            runtime_epoch=max(
                                assignment.runtime_epoch,
                                binding.runtime_epoch + 1,
                            ),
                            capability_ref=assignment.capability_ref,
                        )
                    elif assignment.runtime_epoch < binding.runtime_epoch:
                        raise TermFlowError(
                            "runtime_assignment_conflict",
                            409,
                            "The Agent runtime assignment epoch is stale.",
                        )

            if assignment is not None and is_new_binding and self._runtime_assignment is None:
                # The default assignment is deterministic and does not know
                # about previously closed Bindings.  Runtime rows are unique
                # per (runtime_ref, observed epoch), so a fresh Binding must
                # start above every epoch this runtime has ever used;
                # otherwise re-setup after a revoke collides with the closed
                # Binding's observed row.
                highest_binding_epoch = await session.scalar(
                    select(func.max(AgentBinding.runtime_epoch)).where(
                        AgentBinding.runtime_ref == assignment.runtime_ref
                    )
                )
                highest_observed_epoch = await session.scalar(
                    select(func.max(AgentRuntimeBinding.observed_runtime_epoch)).where(
                        AgentRuntimeBinding.observed_runtime_ref == assignment.runtime_ref
                    )
                )
                used_epochs = [
                    epoch
                    for epoch in (highest_binding_epoch, highest_observed_epoch)
                    if epoch is not None
                ]
                if used_epochs and assignment.runtime_epoch <= max(used_epochs):
                    assignment = AgentRuntimeAssignment(
                        runtime_ref=assignment.runtime_ref,
                        runtime_epoch=max(used_epochs) + 1,
                        capability_ref=assignment.capability_ref,
                    )

            assignment_changed = assignment is not None and (
                binding.runtime_ref != assignment.runtime_ref
                or binding.runtime_epoch != assignment.runtime_epoch
                or binding.capability_ref != assignment.capability_ref
            )
            epoch_changed = (
                assignment_changed
                and previous_runtime_epoch is not None
                and assignment is not None
                and assignment.runtime_epoch != previous_runtime_epoch
            )
            configuration_changed = (
                is_new_binding
                or profile_was_noncanonical
                or previous_panes != set(command.pane_ids)
                or current_fingerprints != {command.disclosure_fingerprint}
                or assignment_changed
                or binding.status != "enabled"
            )

            if is_new_binding:
                # ``assignment`` is always resolved for a newly-created row.
                assert assignment is not None
                binding.runtime_ref = assignment.runtime_ref
                binding.runtime_epoch = assignment.runtime_epoch
                binding.capability_ref = assignment.capability_ref
            elif configuration_changed:
                values: dict[str, object] = {
                    "status": "enabled",
                    "updated_at": now,
                    # SQL expression, rather than a Python read/modify/write,
                    # makes a controller's stale revision CAS fail after this
                    # setup transaction commits.
                    "config_revision": AgentBinding.config_revision + 1,
                }
                if assignment_changed and assignment is not None:
                    values.update(
                        {
                            "runtime_ref": assignment.runtime_ref,
                            "runtime_epoch": assignment.runtime_epoch,
                            "capability_ref": assignment.capability_ref,
                        }
                    )
                updated = await session.execute(
                    update(AgentBinding)
                    .where(AgentBinding.id == binding.id)
                    .values(**values)
                    .returning(AgentBinding)
                )
                binding = cast(AgentBinding, updated.scalar_one())
            else:
                # Keep the desired state explicit even for a no-op retry with
                # a fresh idempotency key; no revision bump is needed.
                binding.status = "enabled"
            receipt.binding_id = binding.id

            matching_disclosures = [
                disclosure
                for disclosure in current_disclosures
                if disclosure.disclosure_fingerprint == command.disclosure_fingerprint
            ]
            existing_disclosure = matching_disclosures[0] if matching_disclosures else None
            if existing_disclosure is None:
                # Acceptance is a current consent, while rows remain an
                # immutable history.  Revoke a previous current fingerprint
                # before inserting the new one, atomically with setup.
                await session.execute(
                    update(AgentProviderDisclosureAcceptance)
                    .where(
                        AgentProviderDisclosureAcceptance.binding_id == binding.id,
                        AgentProviderDisclosureAcceptance.revoked_at.is_(None),
                        AgentProviderDisclosureAcceptance.disclosure_fingerprint
                        != command.disclosure_fingerprint,
                    )
                    .values(revoked_at=now)
                )
                session.add(
                    AgentProviderDisclosureAcceptance(
                        binding_id=binding.id,
                        disclosure_fingerprint=command.disclosure_fingerprint,
                        provider_id=entry.provider_id,
                        model_id=config.model_id,
                        endpoint_origin=entry.endpoint_origin,
                        region=entry.region,
                        retention_terms=entry.retention_terms,
                        retention_version=entry.retention_version,
                        no_training=entry.no_training,
                        policy_version=entry.policy_version,
                        accepted_at=now,
                        accepted_auth_epoch=auth_epoch,
                        actor_kind=actor_kind,
                        actor_ref=actor_ref,
                    )
                )
            elif len(current_disclosures) > 1:
                # Repair a pre-existing duplicate-current state without
                # rewriting history: retain the newest matching acceptance and
                # revoke every other current row, including duplicate copies
                # of that same fingerprint.
                await session.execute(
                    update(AgentProviderDisclosureAcceptance)
                    .where(
                        AgentProviderDisclosureAcceptance.binding_id == binding.id,
                        AgentProviderDisclosureAcceptance.revoked_at.is_(None),
                        AgentProviderDisclosureAcceptance.id != existing_disclosure.id,
                    )
                    .values(revoked_at=now)
                )

            for pane_id in command.pane_ids:
                policy = await session.scalar(
                    select(PanePolicy)
                    .where(PanePolicy.binding_id == binding.id, PanePolicy.pane_id == pane_id)
                    .limit(1)
                )
                if policy is None:
                    session.add(PanePolicy(binding_id=binding.id, pane_id=pane_id, allowed=True))
                else:
                    policy.allowed = True
            # Setup owns an exact allow-list.  A retry or reconfiguration must
            # not leave a previously allowed Pane attached to this Binding.
            await session.execute(
                delete(PanePolicy).where(
                    PanePolicy.binding_id == binding.id,
                    PanePolicy.pane_id.not_in(command.pane_ids),
                )
            )

            # ``bootstrap_secret`` was resolved and type-checked before the
            # transaction.  Hash only that local value; the raw secret never
            # enters a model, receipt, exception, or log.
            token_hash = self._secret_hash(bootstrap_secret)
            token = await session.scalar(
                select(AgentToken)
                .where(
                    AgentToken.binding_id == binding.id,
                    AgentToken.token_hash == token_hash,
                    AgentToken.revoked_at.is_(None),
                )
                .limit(1)
            )
            if token is not None and (
                epoch_changed or token.binding_epoch != (binding.runtime_epoch or 1)
            ):
                # An old bootstrap secret is itself an epoch-bound capability;
                # silently moving its row to a newer epoch would preserve
                # authority across a runtime identity change.  Require the
                # caller to present a new secret, while the surrounding setup
                # transaction rolls back every desired-row mutation.
                raise TermFlowError(
                    "capability_rotation_required",
                    409,
                    "A new bootstrap capability is required for this runtime epoch.",
                )
            if token is None:
                if epoch_changed or assignment_changed:
                    # A new secret/identity supersedes all previous active
                    # capability rows.  They remain as revocation history and
                    # can never authenticate against the new epoch.
                    await session.execute(
                        update(AgentToken)
                        .where(
                            AgentToken.binding_id == binding.id,
                            AgentToken.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
                # ``agent_tokens.token_hash`` is globally unique: one
                # deployment bootstrap secret maps to exactly one capability
                # row.  A previous Binding may have been revoked, leaving its
                # row behind while the same secret is re-issued here, so
                # re-arm that row instead of inserting a duplicate hash.  A
                # capability that is still active for another Binding must
                # never be hijacked silently.
                prior = await session.scalar(
                    select(AgentToken).where(AgentToken.token_hash == token_hash).limit(1)
                )
                if prior is not None and prior.revoked_at is None:
                    prior_binding = await session.get(AgentBinding, prior.binding_id)
                    if prior_binding is not None and prior_binding.status in (
                        "enabled",
                        "pending",
                        "ready",
                    ):
                        raise TermFlowError(
                            "capability_conflict",
                            409,
                            "The deployment bootstrap capability is already "
                            "active for another Binding.",
                        )
                capability: dict[str, object] = {
                    "binding_id": binding.id,
                    "scopes": json.dumps(
                        ["terminal.observe", "terminal.write"], separators=(",", ":")
                    ),
                    "expiry_epoch": int(now.timestamp()) + 31536000,
                    "binding_epoch": binding.runtime_epoch or 1,
                    "revoked_at": None,
                }
                if prior is not None:
                    await session.execute(
                        update(AgentToken).where(AgentToken.id == prior.id).values(**capability)
                    )
                else:
                    session.add(AgentToken(token_hash=token_hash, **capability))
            else:
                # Same-epoch idempotent setup leaves the existing capability
                # row untouched; in particular it never rebinds a token to a
                # different epoch.
                token.scopes = json.dumps(
                    ["terminal.observe", "terminal.write"], separators=(",", ":")
                )

            runtime = await session.scalar(
                select(AgentRuntimeBinding)
                .where(AgentRuntimeBinding.binding_id == binding.id)
                .limit(1)
            )
            if runtime is None:
                session.add(
                    AgentRuntimeBinding(
                        binding_id=binding.id,
                        readiness="reconciling",
                        config_fingerprint=command.disclosure_fingerprint,
                        transition_started_at=now,
                        provider_readiness="configured_unverified",
                    )
                )
            else:
                runtime.readiness = "reconciling"
                runtime.reason_code = None
                runtime.config_fingerprint = command.disclosure_fingerprint
                runtime.transition_started_at = now
                runtime.provider_readiness = "configured_unverified"
                runtime.provider_verified_revision = None
                runtime.provider_last_checked_at = None
                runtime.provider_reason_code = None
            await session.commit()

        result = AgentSetupResult("activating", command.term_id, binding.id, None)
        if self._controller is not None:
            try:
                callback = cast(Any, getattr(self._controller, "reconcile", self._controller))
                outcome = callback(binding.id)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                state, reason = self._result_from_controller(binding.id, outcome)
                persisted = await self._record_receipt_result(
                    key,
                    state=state,
                    binding_id=binding.id,
                    reason_code=reason,
                )
                if persisted is not None:
                    result = self._result_from_receipt(persisted)
            except Exception as exc:
                # Keep the enabled desired Binding and observed row; callers can
                # retry reconciliation through the controller/startup sweep.
                reason = self._stable_runtime_reason(exc)
                async with self._sessions() as session:
                    runtime = await session.scalar(
                        select(AgentRuntimeBinding).where(
                            AgentRuntimeBinding.binding_id == binding.id
                        )
                    )
                    if runtime is not None:
                        runtime.readiness = "not_ready"
                        runtime.reason_code = reason
                        await session.commit()
                persisted = await self._record_receipt_result(
                    key,
                    state="unavailable",
                    binding_id=binding.id,
                    reason_code=reason,
                )
                if persisted is not None:
                    result = self._result_from_receipt(persisted)
        return result

    async def inspect(self, term_id: UUID) -> AgentSetupView:
        async with self._sessions() as session:
            binding = await session.scalar(
                select(AgentBinding)
                .where(
                    AgentBinding.term_id == term_id,
                    AgentBinding.status.in_(("disabled", "enabled", "pending", "ready")),
                )
                .order_by(AgentBinding.created_at.desc())
                .limit(1)
            )
            if binding is None:
                return AgentSetupView("unconfigured", term_id, None)
            runtime = await session.scalar(
                select(AgentRuntimeBinding).where(AgentRuntimeBinding.binding_id == binding.id)
            )
            if runtime is None:
                return AgentSetupView(
                    "deployment_required", term_id, binding.id, "deployment_required"
                )
            state = (
                "ready"
                if runtime.readiness == "ready"
                else "unavailable"
                if runtime.readiness == "not_ready"
                else "activating"
            )
            return AgentSetupView(state, term_id, binding.id, runtime.reason_code)
