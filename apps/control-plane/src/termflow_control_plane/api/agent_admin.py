"""Administration API for the Agent Broker plugin (plan §13.1, task M1.5).

Admin-only endpoints under ``/api/v1/agent/admin`` for Agent Profiles,
Bindings, and binding-scoped AgentTokens.  Every route requires admin
authentication (``require_admin``).

Token security contract: ``POST /tokens`` returns the raw AgentToken exactly
once in the response body; only its SHA-256 hash is persisted.  The raw token
is never stored at rest, logged, or returned by any list/detail endpoint, so
callers must capture it from the create response and treat it as a secret.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from termflow_control_plane.api.agent_cleanup import existing_deletion_response, pending_response
from termflow_control_plane.api.dependencies import (
    get_repositories,
    get_session_factory,
    require_admin,
    require_fresh_admin,
)
from termflow_control_plane.auth.context import AdminAuthContext
from termflow_control_plane.auth.tokens import hash_token, issue_token
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.models import (
    AgentBinding,
    AgentProfile,
    AgentProviderDisclosureAcceptance,
    AgentRuntimeBinding,
    AgentToken,
    PanePolicy,
)
from termflow_control_plane.persistence.repositories import (
    AgentProfileNameConflict,
    RepositoryBundle,
    decode_scopes,
)
from termflow_control_plane.plugins.agent_broker.agent.permissions import ApprovalPolicy
from termflow_control_plane.plugins.agent_broker.agent.provider_catalog import (
    ProviderCatalog,
    canonicalize_profile_config,
)
from termflow_control_plane.plugins.agent_broker.agent.provisioning import (
    AgentProvisioningService,
    AgentSetupCommand,
)
from termflow_control_plane.plugins.agent_broker.agent.runtime_registry import (
    RuntimeShutdownError,
)
from termflow_control_plane.plugins.agent_broker.cleanup import create_deletion_manifest

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/agent/admin",
    tags=["agent"],
    dependencies=[Depends(require_admin)],
)

#: The compatibility PATCH is deliberately safety-reducing only.  Authority
#: activation stays on the fresh-authenticated ``/activate`` endpoint.
_BINDING_STATUSES = frozenset({"revoked", "disabled"})


class AgentProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=128)
    backend_kind: str = Field(min_length=1, max_length=32)
    config: str


class AgentProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    config: str | None = None


class AgentProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    display_name: str
    backend_kind: str
    config: str
    created_at: datetime
    updated_at: datetime


class AgentProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[AgentProfileResponse]


class AgentBindingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    term_id: UUID


class AgentBindingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["disabled", "revoked"] | None = None


class AgentWritePolicyRequest(BaseModel):
    """Two-mode write approval policy the agent panel toggles."""

    model_config = ConfigDict(extra="forbid")

    write_policy: Literal["manual", "auto"]


class AgentBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    profile_id: UUID
    term_id: UUID
    status: str
    write_policy: str = "manual"
    runtime_ref: str | None
    runtime_epoch: int | None
    capability_ref: str | None
    created_at: datetime
    updated_at: datetime


class AgentBindingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bindings: list[AgentBindingResponse]


class AgentSetupRequest(BaseModel):
    """Strict, credential-free product setup command (spec §6.1)."""

    model_config = ConfigDict(extra="forbid")

    term_id: UUID
    profile_id: UUID | None = None
    profile_display_name: str | None = Field(default=None, min_length=1, max_length=128)
    pane_ids: list[str] = Field(min_length=1)
    topology_revision: int = Field(ge=0)
    disclosure_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted: Literal[True]
    idempotency_key: UUID

    @model_validator(mode="after")
    def exactly_one_profile_selector(self) -> AgentSetupRequest:
        if (self.profile_id is None) == (self.profile_display_name is None):
            raise ValueError("exactly one of profile_id or profile_display_name is required")
        if len(set(self.pane_ids)) != len(self.pane_ids):
            raise ValueError("pane_ids must be unique")
        if any(not pane_id.strip() or pane_id.strip() != pane_id for pane_id in self.pane_ids):
            raise ValueError("pane_ids must be normalized non-empty strings")
        if any(pane_id in {"all_panes", "*"} for pane_id in self.pane_ids):
            raise ValueError("all_panes is not a valid pane policy")
        return self


class AgentSetupProfileSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    display_name: str
    backend_kind: str
    provider_id: str | None = None
    model_id: str | None = None


class AgentSetupTokenSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installed: bool
    expires_at: datetime | None = None


class AgentRuntimeStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readiness: str
    reason_code: str | None
    config_revision: int
    applied_revision: int | None
    runtime_epoch: int | None
    provider_readiness: str
    observed_runtime_ref: str | None = None
    observed_capability_ref: str | None = None
    provider_reason_code: str | None = None


class AgentPanePolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    pane_ids: list[str]
    topology_revision: int | None


class AgentProviderDisclosureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID | None
    disclosure_fingerprint: str
    provider_id: str
    model_id: str
    endpoint_origin: str
    region: str
    retention_terms: str
    retention_version: str
    no_training: bool
    policy_version: str
    credential_source: str | None = None
    accepted: bool
    accepted_at: datetime | None = None
    accepted_auth_epoch: int | None = None


class AgentBindingDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    profile_id: UUID
    term_id: UUID
    status: str
    write_policy: str = "manual"
    config_revision: int
    runtime_ref: str | None
    runtime_epoch: int | None
    capability_ref: str | None
    profile: AgentSetupProfileSummary | None = None
    runtime: AgentRuntimeStateResponse | None = None
    pane_policy: AgentPanePolicyResponse
    disclosure: AgentProviderDisclosureResponse | None = None


class AgentSetupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal[
        "unconfigured",
        "deployment_required",
        "activating",
        "ready",
        "unavailable",
    ]
    term_id: UUID
    binding_id: UUID | None
    profile: AgentSetupProfileSummary | None = None
    profiles: list[AgentSetupProfileSummary] = Field(default_factory=list)
    token: AgentSetupTokenSummary
    runtime: AgentRuntimeStateResponse | None = None
    write_policy: str | None = None
    pane_policy: AgentPanePolicyResponse | None = None
    disclosure: AgentProviderDisclosureResponse | None = None
    topology_revision: int | None = None
    reason_code: str | None = None


class AgentBindingRuntimeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_ref: str = Field(min_length=1, max_length=128)
    capability_ref: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class AgentPanePolicyReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pane_ids: list[str] = Field(min_length=1)
    topology_revision: int = Field(ge=0)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_exact_allowlist(self) -> AgentPanePolicyReplaceRequest:
        if len(set(self.pane_ids)) != len(self.pane_ids):
            raise ValueError("pane_ids must be unique")
        if any(not pane_id.strip() or pane_id.strip() != pane_id for pane_id in self.pane_ids):
            raise ValueError("pane_ids must be normalized non-empty strings")
        if any(pane_id in {"all_panes", "*"} for pane_id in self.pane_ids):
            raise ValueError("all_panes is not a valid pane policy")
        return self


class AgentDisclosureAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disclosure_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted: Literal[True]


class AgentTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime


class AgentTokenCreatedResponse(BaseModel):
    """Token issuance response.

    ``raw_token`` is returned exactly once and is never persisted; only its
    SHA-256 hash is stored at rest.  Callers must store it with the client
    immediately and treat it as a secret.
    """

    model_config = ConfigDict(extra="forbid")

    token_id: UUID
    binding_id: UUID
    scopes: list[str]
    expires_at: datetime
    raw_token: str
    created_at: datetime


class AgentTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: UUID
    binding_id: UUID
    scopes: list[str]
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


class AgentTokenListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: list[AgentTokenResponse]


def _binding_response(binding: AgentBinding) -> AgentBindingResponse:
    return AgentBindingResponse(
        binding_id=binding.id,
        profile_id=binding.profile_id,
        term_id=binding.term_id,
        status=binding.status,
        write_policy=binding.write_policy,
        runtime_ref=binding.runtime_ref,
        runtime_epoch=binding.runtime_epoch,
        capability_ref=binding.capability_ref,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _profile_response(profile: AgentProfile) -> AgentProfileResponse:
    return AgentProfileResponse(
        profile_id=profile.id,
        display_name=profile.display_name,
        backend_kind=profile.backend_kind,
        config=profile.config,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _token_response(token: AgentToken) -> AgentTokenResponse:
    return AgentTokenResponse(
        token_id=token.id,
        binding_id=token.binding_id,
        scopes=list(decode_scopes(token.scopes)),
        expires_at=datetime.fromtimestamp(token.expiry_epoch, tz=UTC),
        revoked_at=token.revoked_at,
        created_at=token.created_at,
    )


def _provider_catalog(request: Request) -> ProviderCatalog:
    """Return the composition-root catalog, or a fail-closed settings catalog."""

    catalog = getattr(request.app.state, "agent_provider_catalog", None)
    if isinstance(catalog, ProviderCatalog):
        return catalog
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return ProviderCatalog(())
    return ProviderCatalog.from_settings(settings)


def _profile_summary(
    profile: AgentProfile | None,
    catalog: ProviderCatalog,
) -> AgentSetupProfileSummary | None:
    if profile is None:
        return None
    provider_id: str | None = None
    model_id: str | None = None
    try:
        config, _ = canonicalize_profile_config(profile.config)
        catalog.resolve(config)
        provider_id = config.provider_id
        model_id = config.model_id
    except (TermFlowError, ValueError, TypeError):
        # A malformed/retired profile remains visible by name, but its
        # provider identity is deliberately not guessed or echoed.
        pass
    return AgentSetupProfileSummary(
        profile_id=profile.id,
        display_name=profile.display_name,
        backend_kind=profile.backend_kind,
        provider_id=provider_id,
        model_id=model_id,
    )


async def _topology_info(
    request: Request,
    term_id: UUID,
) -> tuple[int | None, set[str]]:
    """Read the current live Term topology without persisting terminal data."""

    source = getattr(request.app.state, "agent_topology_provider", None)
    value: Any = None
    if source is not None:
        try:
            value = source(term_id) if callable(source) else source
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            value = None
    if value is None:
        registry = getattr(request.app.state, "registry", None)
        if registry is not None:
            try:
                connection = await registry.maybe_get(term_id)
                value = getattr(connection, "topology", None)
            except Exception:
                value = None
    if value is None:
        return None, set()
    if isinstance(value, dict):
        revision = value.get("revision", value.get("topology_revision"))
        panes = value.get("pane_ids", value.get("panes", ()))
    else:
        revision = getattr(value, "revision", getattr(value, "topology_revision", None))
        panes = getattr(value, "pane_ids", None)
        if panes is None and hasattr(value, "windows"):
            panes = [pane for window in value.windows for pane in window.panes]
    normalized = {str(getattr(pane, "pane_id", pane)) for pane in (panes or ())}
    try:
        return (int(revision) if revision is not None else None), normalized
    except (TypeError, ValueError):
        return None, normalized


async def _current_disclosure(
    binding: AgentBinding,
    profile: AgentProfile | None,
    catalog: ProviderCatalog,
    repositories: RepositoryBundle,
) -> AgentProviderDisclosureResponse | None:
    """Build a disclosure view from server-owned catalog facts and acceptance."""

    if profile is None:
        return None
    try:
        config, _ = canonicalize_profile_config(profile.config)
        entry = catalog.resolve(config)
        fingerprint = catalog.disclosure_fingerprint(config)
    except (TermFlowError, ValueError, TypeError):
        return None
    accepted = await repositories.agent_provider_disclosures.get_current(
        binding.id,
        fingerprint,
    )
    if accepted is None:
        return AgentProviderDisclosureResponse(
            binding_id=binding.id,
            disclosure_fingerprint=fingerprint,
            provider_id=entry.provider_id,
            model_id=config.model_id,
            endpoint_origin=entry.endpoint_origin,
            region=entry.region,
            retention_terms=entry.retention_terms,
            retention_version=entry.retention_version,
            no_training=entry.no_training,
            policy_version=entry.policy_version,
            credential_source=entry.credential_source,
            accepted=False,
        )
    return AgentProviderDisclosureResponse(
        binding_id=binding.id,
        disclosure_fingerprint=accepted.disclosure_fingerprint,
        provider_id=accepted.provider_id,
        model_id=accepted.model_id,
        endpoint_origin=accepted.endpoint_origin,
        region=accepted.region,
        retention_terms=accepted.retention_terms,
        retention_version=accepted.retention_version,
        no_training=accepted.no_training,
        policy_version=accepted.policy_version,
        credential_source=entry.credential_source,
        accepted=True,
        accepted_at=accepted.accepted_at,
        accepted_auth_epoch=accepted.accepted_auth_epoch,
    )


def _runtime_response(
    binding: AgentBinding,
    runtime: AgentRuntimeBinding | None,
) -> AgentRuntimeStateResponse | None:
    if runtime is None:
        return None
    return AgentRuntimeStateResponse(
        readiness=runtime.readiness,
        reason_code=runtime.reason_code,
        config_revision=binding.config_revision,
        applied_revision=runtime.applied_revision,
        runtime_epoch=(
            runtime.observed_runtime_epoch
            if runtime.observed_runtime_epoch is not None
            else binding.runtime_epoch
        ),
        provider_readiness=runtime.provider_readiness,
        observed_runtime_ref=runtime.observed_runtime_ref,
        observed_capability_ref=runtime.observed_capability_ref,
        provider_reason_code=runtime.provider_reason_code,
    )


async def _binding_detail(
    binding: AgentBinding,
    *,
    request: Request,
    repositories: RepositoryBundle,
) -> AgentBindingDetailResponse:
    profile = await repositories.agent_profiles.get_by_id(binding.profile_id)
    runtime = await repositories.agent_runtime_bindings.get_by_binding(binding.id)
    policies = await repositories.pane_policies.get_for_binding(binding.id)
    topology_revision, _ = await _topology_info(request, binding.term_id)
    catalog = _provider_catalog(request)
    return AgentBindingDetailResponse(
        binding_id=binding.id,
        profile_id=binding.profile_id,
        term_id=binding.term_id,
        status=binding.status,
        write_policy=binding.write_policy,
        config_revision=binding.config_revision,
        runtime_ref=binding.runtime_ref,
        runtime_epoch=binding.runtime_epoch,
        capability_ref=binding.capability_ref,
        profile=_profile_summary(profile, catalog),
        runtime=_runtime_response(binding, runtime),
        pane_policy=AgentPanePolicyResponse(
            binding_id=binding.id,
            pane_ids=sorted(policy.pane_id for policy in policies if policy.allowed),
            topology_revision=topology_revision,
        ),
        disclosure=await _current_disclosure(binding, profile, catalog, repositories),
    )


async def _setup_response(
    term_id: UUID,
    *,
    request: Request,
    repositories: RepositoryBundle,
) -> AgentSetupResponse:
    catalog = _provider_catalog(request)
    profiles = await repositories.agent_profiles.list()
    profile_summaries = [
        summary
        for profile in profiles
        if (summary := _profile_summary(profile, catalog)) is not None
    ]
    bindings = [
        binding
        for binding in await repositories.agent_bindings.list_for_term(term_id)
        if binding.status in {"disabled", "enabled", "pending", "ready"}
    ]
    binding = bindings[-1] if bindings else None
    topology_revision, _ = await _topology_info(request, term_id)
    if binding is None:
        default_config = catalog.default_config()
        preview = None
        if default_config is not None:
            entry = catalog.resolve(default_config)
            preview = AgentProviderDisclosureResponse(
                binding_id=None,
                disclosure_fingerprint=catalog.disclosure_fingerprint(default_config),
                provider_id=entry.provider_id,
                model_id=default_config.model_id,
                endpoint_origin=entry.endpoint_origin,
                region=entry.region,
                retention_terms=entry.retention_terms,
                retention_version=entry.retention_version,
                no_training=entry.no_training,
                policy_version=entry.policy_version,
                credential_source=entry.credential_source,
                accepted=False,
            )
        return AgentSetupResponse(
            state="unconfigured",
            term_id=term_id,
            binding_id=None,
            profiles=profile_summaries,
            token=AgentSetupTokenSummary(installed=False),
            topology_revision=topology_revision,
            disclosure=preview,
            reason_code="provider_selection_unavailable" if preview is None else None,
        )
    profile = await repositories.agent_profiles.get_by_id(binding.profile_id)
    runtime = await repositories.agent_runtime_bindings.get_by_binding(binding.id)
    policies = await repositories.pane_policies.get_for_binding(binding.id)
    tokens = await repositories.agent_tokens.list_for_binding(binding.id)
    now_epoch = int(datetime.now(UTC).timestamp())
    live_tokens = [
        token for token in tokens if token.revoked_at is None and token.expiry_epoch > now_epoch
    ]
    token = max(live_tokens, key=lambda row: row.created_at) if live_tokens else None
    disclosure = await _current_disclosure(binding, profile, catalog, repositories)
    reason: str | None
    if runtime is None:
        state: Literal[
            "unconfigured", "deployment_required", "activating", "ready", "unavailable"
        ] = "deployment_required"
        reason = "deployment_required"
    elif runtime.readiness == "ready":
        state = "ready"
        reason = runtime.reason_code
    elif runtime.readiness in {"not_ready", "blocked"}:
        state = (
            "deployment_required" if runtime.reason_code == "deployment_required" else "unavailable"
        )
        reason = runtime.reason_code
    else:
        state = "activating"
        reason = runtime.reason_code
    return AgentSetupResponse(
        state=state,
        term_id=term_id,
        binding_id=binding.id,
        profile=_profile_summary(profile, catalog),
        profiles=profile_summaries,
        token=AgentSetupTokenSummary(
            installed=token is not None,
            expires_at=(
                datetime.fromtimestamp(token.expiry_epoch, tz=UTC) if token is not None else None
            ),
        ),
        runtime=_runtime_response(binding, runtime),
        write_policy=binding.write_policy,
        pane_policy=AgentPanePolicyResponse(
            binding_id=binding.id,
            pane_ids=sorted(policy.pane_id for policy in policies if policy.allowed),
            topology_revision=topology_revision,
        ),
        disclosure=disclosure,
        topology_revision=topology_revision,
        reason_code=reason,
    )


def _controller(request: Request) -> Any | None:
    return getattr(request.app.state, "agent_runtime_controller", None)


async def _validate_server_runtime_assignment(
    request: Request,
    binding: AgentBinding,
    runtime_ref: str,
    capability_ref: str,
) -> None:
    """Reject client-invented runtime identities before the authority CAS.

    The composition root may expose a deployment-specific assignment validator.
    In the reference deployment the only new identity is the deterministic
    OpenCode/MCP pair; an unchanged persisted pair remains valid for an
    idempotent revision update.
    """

    if runtime_ref != runtime_ref.strip() or capability_ref != capability_ref.strip():
        raise TermFlowError(
            "runtime_assignment_conflict",
            409,
            "The Agent runtime assignment is invalid.",
        )
    validator = getattr(request.app.state, "agent_runtime_assignment_validator", None)
    if validator is not None:
        try:
            result = validator(binding.term_id, runtime_ref, capability_ref)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            raise TermFlowError(
                "runtime_assignment_conflict",
                409,
                "The Agent runtime assignment is unavailable.",
            ) from None
        if result is False:
            raise TermFlowError(
                "runtime_assignment_conflict",
                409,
                "The Agent runtime assignment is invalid.",
            )
        return

    # Existing assignments are server-created and may be opaque.  They can be
    # retained, but an identity change must use the reference deployment's
    # server policy rather than arbitrary client strings.
    unchanged = runtime_ref == binding.runtime_ref and capability_ref == binding.capability_ref
    reference_assignment = (
        runtime_ref == "opencode-agent" and capability_ref == f"termflow-mcp:{binding.id}"
    )
    if not unchanged and not reference_assignment:
        raise TermFlowError(
            "runtime_assignment_conflict",
            409,
            "The Agent runtime assignment is invalid.",
        )


async def _call_controller(
    controller: Any,
    method_name: str,
    binding_id: UUID,
    *args: Any,
    **kwargs: Any,
) -> Any:
    method = getattr(controller, method_name, None)
    if method is None:
        return None
    try:
        outcome = method(binding_id, *args, **kwargs)
    except TypeError:
        # Small test fakes often expose a positional-only fence signature.
        outcome = method(binding_id, *args)
    if inspect.isawaitable(outcome):
        return await outcome
    return outcome


async def _fence_binding(request: Request, binding_id: UUID, *, rotate_epoch: bool) -> Any:
    controller = _controller(request)
    if controller is not None:
        return await _call_controller(
            controller,
            "fence",
            binding_id,
            rotate_epoch=rotate_epoch,
        )
    # Isolated API mounts may not have a controller.  Preserve the security
    # ordering by unpublishing/stopping the local mapping before returning.
    registry = getattr(request.app.state, "agent_runtime_registry", None)
    if registry is not None:
        await registry.stop_binding(binding_id)
    stream_hub = getattr(request.app.state, "agent_stream_hub", None)
    if stream_hub is not None:
        await stream_hub.close_for_binding(binding_id)
    return None


async def _reconcile_binding(request: Request, binding_id: UUID) -> Any:
    _require_startup_ready(request)
    controller = _controller(request)
    if controller is None:
        return None
    return await _call_controller(controller, "reconcile", binding_id)


def _require_startup_ready(request: Request) -> None:
    plugin = getattr(request.app.state, "agent_broker_plugin", None)
    coordinator = getattr(plugin, "startup_coordinator", None)
    if coordinator is not None and coordinator.result.state != "ready":
        raise TermFlowError("recovery_failed", 503, "Agent recovery is incomplete.")


def _controller_reason(exc: BaseException) -> str:
    detail = str(exc).lower()
    if "mcp_not_connected" in detail:
        return "mcp_not_connected"
    if "pipeline" in detail or "start" in detail:
        return "pipeline_start_failed"
    if "assignment" in detail or "runtime_ref" in detail:
        return "runtime_assignment_conflict"
    return "runtime_unreachable"


async def _require_binding(
    binding_id: UUID,
    repositories: RepositoryBundle,
) -> AgentBinding:
    binding = await repositories.agent_bindings.get_by_id(binding_id)
    if binding is None:
        raise TermFlowError("binding_not_found", 404, "The Agent Binding does not exist.")
    return binding


# ---------------------------------------------------------------------------
# Product setup aggregate
# ---------------------------------------------------------------------------


def _setup_service(request: Request) -> AgentProvisioningService:
    service = getattr(request.app.state, "agent_provisioning", None)
    if service is None:
        raise TermFlowError(
            "agent_setup_unavailable",
            503,
            "Agent setup is not available.",
        )
    return cast(AgentProvisioningService, service)


@router.get("/setup", response_model=AgentSetupResponse)
async def get_agent_setup(
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    term_id: Annotated[UUID, Query()],
) -> AgentSetupResponse:
    # ``inspect`` is intentionally advisory; the response is assembled from
    # the current aggregate so token/disclosure/policy details cannot become
    # stale between a controller transition and this read.
    return await _setup_response(term_id, request=request, repositories=repositories)


@router.post("/setup", response_model=AgentSetupResponse)
async def post_agent_setup(
    body: AgentSetupRequest,
    request: Request,
    response: Response,
    auth: Annotated[AdminAuthContext, Depends(require_fresh_admin)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentSetupResponse:
    _require_startup_ready(request)
    service = _setup_service(request)
    command = AgentSetupCommand(
        term_id=body.term_id,
        profile_id=body.profile_id,
        profile_display_name=body.profile_display_name,
        pane_ids=tuple(body.pane_ids),
        topology_revision=body.topology_revision,
        disclosure_fingerprint=body.disclosure_fingerprint,
        accepted=body.accepted,
        idempotency_key=body.idempotency_key,
    )
    result = await service.setup(command, auth)
    # Re-read the aggregate after the service has committed and (possibly)
    # completed a bounded reconcile.  This also guarantees the response has
    # no raw bootstrap token or provider credential fields.
    payload = await _setup_response(body.term_id, request=request, repositories=repositories)
    if result.state in {"activating", "unavailable"}:
        response.status_code = status.HTTP_202_ACCEPTED
    elif result.state == "ready":
        response.status_code = status.HTTP_200_OK
    # A persisted receipt is authoritative for idempotent replay.  If the
    # aggregate read races a cleanup, retain the receipt's public state.
    if payload.binding_id == result.binding_id:
        payload = payload.model_copy(
            update={"state": result.state, "reason_code": result.reason_code}
        )
    return payload


async def _update_profile(
    sessions: async_sessionmaker[AsyncSession],
    profile_id: UUID,
    *,
    display_name: str | None,
    config: str | None,
) -> AgentProfile | None:
    """Apply the rename/config patch; the profile repository only exposes rename."""
    observed_at = datetime.now(UTC)
    values: dict[str, object] = {"updated_at": observed_at}
    if display_name is not None:
        values["display_name"] = display_name
    if config is not None:
        values["config"] = config
    async with sessions() as session:
        try:
            result = await session.execute(
                update(AgentProfile)
                .where(AgentProfile.id == profile_id)
                .values(**values)
                .returning(AgentProfile)
            )
            profile = result.scalar_one_or_none()
            await session.commit()
            return profile
        except IntegrityError as exc:
            await session.rollback()
            if display_name is None:
                raise
            raise AgentProfileNameConflict from exc


async def _update_profile_and_invalidate_bindings(
    sessions: async_sessionmaker[AsyncSession],
    profile_id: UUID,
    *,
    display_name: str | None,
    config: str,
) -> tuple[AgentProfile | None, tuple[UUID, ...]]:
    """Atomically replace config and fence every non-revoked desired Binding.

    The profile row is the serialization point.  All authority-adjacent
    invalidation is committed with the canonical config so no reader can see a
    new model paired with an old accepted disclosure or a still-ready runtime.
    Runtime identity, capability epoch, tokens, and pane policy are deliberately
    outside the update set.
    """

    observed_at = datetime.now(UTC)
    async with sessions() as session:
        try:
            async with session.begin():
                profile = await session.scalar(
                    select(AgentProfile).where(AgentProfile.id == profile_id).with_for_update()
                )
                if profile is None:
                    return None, ()

                if display_name is not None:
                    profile.display_name = display_name
                config_changed = profile.config != config
                profile.config = config
                profile.updated_at = observed_at
                if not config_changed:
                    return profile, ()

                binding_ids = tuple(
                    await session.scalars(
                        select(AgentBinding.id)
                        .where(
                            AgentBinding.profile_id == profile_id,
                            AgentBinding.status != "revoked",
                        )
                        .order_by(AgentBinding.created_at, AgentBinding.id)
                        .with_for_update()
                    )
                )
                if not binding_ids:
                    return profile, ()

                await session.execute(
                    update(AgentBinding)
                    .where(AgentBinding.id.in_(binding_ids))
                    .values(
                        config_revision=AgentBinding.config_revision + 1,
                        updated_at=observed_at,
                    )
                )
                await session.execute(
                    update(AgentProviderDisclosureAcceptance)
                    .where(
                        AgentProviderDisclosureAcceptance.binding_id.in_(binding_ids),
                        AgentProviderDisclosureAcceptance.revoked_at.is_(None),
                    )
                    .values(revoked_at=observed_at)
                )

                runtimes = list(
                    await session.scalars(
                        select(AgentRuntimeBinding)
                        .where(AgentRuntimeBinding.binding_id.in_(binding_ids))
                        .with_for_update()
                    )
                )
                runtime_by_binding = {runtime.binding_id: runtime for runtime in runtimes}
                for binding_id in binding_ids:
                    runtime = runtime_by_binding.get(binding_id)
                    if runtime is None:
                        runtime = AgentRuntimeBinding(binding_id=binding_id)
                        session.add(runtime)
                    runtime.readiness = "blocked"
                    runtime.reason_code = "binding_disclosure_stale"
                    runtime.last_health_at = None
                    runtime.transition_started_at = observed_at
                    runtime.provider_readiness = "configured_unverified"
                    runtime.provider_verified_revision = None
                    runtime.provider_last_checked_at = None
                    runtime.provider_reason_code = None
                    runtime.updated_at = observed_at
                return profile, binding_ids
        except IntegrityError as exc:
            if display_name is None:
                raise
            raise AgentProfileNameConflict from exc


async def _restore_profile_invalidation_state(
    sessions: async_sessionmaker[AsyncSession],
    binding_ids: tuple[UUID, ...],
) -> None:
    """Preserve the durable stale-disclosure reason after controller fencing."""

    if not binding_ids:
        return
    observed_at = datetime.now(UTC)
    async with sessions() as session:
        await session.execute(
            update(AgentRuntimeBinding)
            .where(AgentRuntimeBinding.binding_id.in_(binding_ids))
            .values(
                readiness="blocked",
                reason_code="binding_disclosure_stale",
                last_health_at=None,
                provider_readiness="configured_unverified",
                provider_verified_revision=None,
                provider_last_checked_at=None,
                provider_reason_code=None,
                updated_at=observed_at,
            )
        )
        await session.commit()


async def _list_bindings(
    sessions: async_sessionmaker[AsyncSession],
    *,
    profile_id: UUID | None,
    term_id: UUID | None,
) -> list[AgentBinding]:
    """List bindings with optional filters; the repository has no unfiltered list."""
    statement = select(AgentBinding).order_by(AgentBinding.created_at)
    if profile_id is not None:
        statement = statement.where(AgentBinding.profile_id == profile_id)
    if term_id is not None:
        statement = statement.where(AgentBinding.term_id == term_id)
    async with sessions() as session:
        rows = await session.scalars(statement)
        return list(rows)


async def _get_agent_token(
    sessions: async_sessionmaker[AsyncSession],
    token_id: UUID,
) -> AgentToken | None:
    async with sessions() as session:
        return await session.get(AgentToken, token_id)


async def _revoke_agent_token(
    sessions: async_sessionmaker[AsyncSession],
    token_id: UUID,
) -> bool:
    """Revoke by id; the token repository only exposes revoke-by-hash."""
    observed_at = datetime.now(UTC)
    async with sessions() as session:
        result = await session.execute(
            update(AgentToken)
            .where(AgentToken.id == token_id, AgentToken.revoked_at.is_(None))
            .values(revoked_at=observed_at)
            .returning(AgentToken.id)
        )
        revoked = result.scalar_one_or_none() is not None
        await session.commit()
        return revoked


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@router.post(
    "/profiles",
    response_model=AgentProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_profile(
    request: AgentProfileCreateRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentProfileResponse:
    _, canonical_config = canonicalize_profile_config(request.config)
    try:
        profile = await repositories.agent_profiles.create(
            display_name=request.display_name,
            backend_kind=request.backend_kind,
            config=canonical_config,
        )
    except AgentProfileNameConflict:
        raise TermFlowError(
            "agent_profile_name_conflict",
            409,
            "An Agent Profile with this display name already exists.",
        ) from None
    return _profile_response(profile)


@router.get("/profiles", response_model=AgentProfileListResponse)
async def list_agent_profiles(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentProfileListResponse:
    profiles = await repositories.agent_profiles.list()
    return AgentProfileListResponse(profiles=[_profile_response(profile) for profile in profiles])


@router.get("/profiles/{profile_id}", response_model=AgentProfileResponse)
async def get_agent_profile(
    profile_id: UUID,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentProfileResponse:
    profile = await repositories.agent_profiles.get_by_id(profile_id)
    if profile is None:
        raise TermFlowError("profile_not_found", 404, "The Agent Profile does not exist.")
    return _profile_response(profile)


@router.patch("/profiles/{profile_id}", response_model=AgentProfileResponse)
async def update_agent_profile(
    profile_id: UUID,
    request: AgentProfileUpdateRequest,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AgentProfileResponse:
    if request.display_name is None and request.config is None:
        raise TermFlowError(
            "invalid_request",
            422,
            "Provide at least one of display_name or config.",
        )
    canonical_config = None
    if request.config is not None:
        _, canonical_config = canonicalize_profile_config(request.config)
    try:
        if canonical_config is None:
            profile = await _update_profile(
                sessions,
                profile_id,
                display_name=request.display_name,
                config=None,
            )
            affected_binding_ids: tuple[UUID, ...] = ()
        else:
            profile, affected_binding_ids = await _update_profile_and_invalidate_bindings(
                sessions,
                profile_id,
                display_name=request.display_name,
                config=canonical_config,
            )
    except AgentProfileNameConflict:
        raise TermFlowError(
            "agent_profile_name_conflict",
            409,
            "An Agent Profile with this display name already exists.",
        ) from None
    if profile is None:
        raise TermFlowError("profile_not_found", 404, "The Agent Profile does not exist.")
    if request.config is not None:
        # Stop any current in-process pipelines so provider events from the
        # old Profile configuration cannot continue.  Configuration revision
        # reconciliation is owned by the runtime controller; model-only
        # updates do not rotate the capability epoch.
        try:
            controller = _controller(http_request)
            if controller is not None:
                for binding_id in affected_binding_ids:
                    await _fence_binding(http_request, binding_id, rotate_epoch=False)
                # ``fence`` publishes its generic assignment-conflict state;
                # the profile transaction's stable disclosure reason remains
                # authoritative while the user reviews the new disclosure.
                await _restore_profile_invalidation_state(sessions, affected_binding_ids)
            else:
                registry = getattr(http_request.app.state, "agent_runtime_registry", None)
                if registry is not None:
                    # Batch fallback is used by isolated API mounts which have
                    # no controller but still need to remove every mapping.
                    await registry.stop_bindings(affected_binding_ids)
        except RuntimeShutdownError:
            raise TermFlowError(
                "runtime_shutdown_failed",
                500,
                "One or more Agent runtimes could not be stopped.",
            ) from None
    return _profile_response(profile)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_profile(
    profile_id: UUID,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> Response:
    """Delete a profile through the durable tombstone/cleanup path.

    Plan §15/§17 delete contract: a durable cleanup tombstone is created
    BEFORE the row deletion, the profile's active runs are cancelled, and
    every backend session of its bindings is proven deleted before any row
    goes away.  When the backend deletion cannot be proven, the rows stay
    and the request fails with ``deletion_pending`` while the tombstone
    keeps retrying.
    """
    from termflow_control_plane.api.agent_conversations import get_agent_runtime_registry
    from termflow_control_plane.plugins.agent_broker.plugin import (
        BackendRuntimeUnavailableError,
        delete_binding_backend_sessions,
        record_cleanup_failure,
    )

    if await repositories.agent_profiles.get_by_id(profile_id) is None:
        existing = await existing_deletion_response(
            http_request, repositories, "profile", profile_id
        )
        if existing is not None:
            return existing
        raise TermFlowError("profile_not_found", 404, "The Agent Profile does not exist.")
    registry = get_agent_runtime_registry(http_request)

    # 1) Durable cleanup tombstone BEFORE the row deletion (plan §15), so a
    #    crash mid-delete leaves a retryable job instead of orphaned rows.
    job = await create_deletion_manifest(repositories, target_kind="profile", target_id=profile_id)

    # 2) Cancel the profile's active runs and prove every backend session of
    #    its bindings is gone before any row goes away (plan §17).
    failures: list[str] = []
    for binding in await repositories.agent_bindings.list_for_profile(profile_id):
        for run in await repositories.agent_runs.list_active_for_binding(binding.id):
            await repositories.agent_runs.set_state(
                run.id, "cancelled", expected_state=run.run_state
            )
        try:
            failures.extend(
                await delete_binding_backend_sessions(repositories, registry, binding.id)
            )
        except BackendRuntimeUnavailableError as exc:
            failures.append(str(exc))
    if failures:
        await record_cleanup_failure(job, repositories, "; ".join(failures))
        return pending_response(http_request, job.id)

    # 3) Delete the profile row (cascading its bindings and conversations).
    await repositories.agent_profiles.delete(profile_id)
    await repositories.cleanup_jobs.confirm_pending_internal(job.id)
    await repositories.cleanup_jobs.complete(job.id)
    return await existing_deletion_response(
        http_request, repositories, "profile", profile_id
    ) or Response(status_code=204)


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


@router.post(
    "/bindings",
    response_model=AgentBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_binding(
    request: AgentBindingCreateRequest,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentBindingResponse:
    if await repositories.instances.get(request.term_id) is None:
        raise TermFlowError(
            "instance_not_found",
            404,
            "The Term does not exist.",
        )
    if (
        await repositories.agent_bindings.active_binding_for(
            request.profile_id,
            request.term_id,
        )
        is not None
    ):
        raise TermFlowError(
            "binding_already_exists",
            409,
            "An active Agent Binding already exists for this Profile and Term.",
        )
    binding = await repositories.agent_bindings.create(
        profile_id=request.profile_id,
        term_id=request.term_id,
    )
    return _binding_response(binding)


@router.get("/bindings", response_model=AgentBindingListResponse)
async def list_agent_bindings(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    profile_id: Annotated[UUID | None, Query()] = None,
    term_id: Annotated[UUID | None, Query()] = None,
) -> AgentBindingListResponse:
    bindings = await _list_bindings(
        sessions,
        profile_id=profile_id,
        term_id=term_id,
    )
    return AgentBindingListResponse(bindings=[_binding_response(binding) for binding in bindings])


@router.get("/bindings/{binding_id}", response_model=AgentBindingDetailResponse)
async def get_agent_binding(
    binding_id: UUID,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentBindingDetailResponse:
    binding = await _require_binding(binding_id, repositories)
    return await _binding_detail(binding, request=request, repositories=repositories)


@router.post(
    "/bindings/{binding_id}/activate",
    response_model=AgentBindingDetailResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def activate_agent_binding(
    binding_id: UUID,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    _auth: Annotated[AdminAuthContext, Depends(require_fresh_admin)],
) -> AgentBindingDetailResponse:
    binding = await _require_binding(binding_id, repositories)
    if binding.status == "revoked":
        raise TermFlowError("binding_revoked", 409, "The Agent Binding is revoked.")
    updated = await repositories.agent_bindings.set_status(
        binding_id,
        "enabled",
        expected_status=binding.status,
    )
    if updated is None:
        raise TermFlowError("binding_state_conflict", 409, "The Binding changed concurrently.")
    try:
        await _reconcile_binding(request, binding_id)
    except Exception:
        # Reconciliation persists its own bounded reason when a concrete
        # controller is installed.  The API remains an asynchronous activation
        # contract and exposes the durable observed state on the next read.
        logger.exception("Agent Binding activation reconcile failed")
    current = await _require_binding(binding_id, repositories)
    return await _binding_detail(current, request=request, repositories=repositories)


@router.post(
    "/bindings/{binding_id}/disable",
    response_model=AgentBindingDetailResponse,
    status_code=status.HTTP_200_OK,
)
async def disable_agent_binding(
    binding_id: UUID,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    _auth: Annotated[AdminAuthContext, Depends(require_admin)],
) -> AgentBindingDetailResponse:
    binding = await _require_binding(binding_id, repositories)
    updated = await repositories.agent_bindings.set_status(
        binding_id,
        "disabled",
        expected_status=binding.status,
        advance_runtime_epoch=True,
    )
    if updated is None:
        raise TermFlowError("binding_state_conflict", 409, "The Binding changed concurrently.")
    # Fencing is deliberately awaited before the response.  The desired epoch
    # was already advanced by the transaction above, so the controller must
    # not rotate it a second time.
    try:
        await _fence_binding(request, binding_id, rotate_epoch=False)
    except Exception as exc:
        raise TermFlowError(
            "runtime_fence_failed",
            503,
            "The Agent runtime could not be fenced; retry the disable operation.",
        ) from exc
    current = await _require_binding(binding_id, repositories)
    return await _binding_detail(current, request=request, repositories=repositories)


@router.put(
    "/bindings/{binding_id}/runtime",
    response_model=AgentBindingDetailResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def update_agent_binding_runtime(
    binding_id: UUID,
    body: AgentBindingRuntimeUpdateRequest,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    _auth: Annotated[AdminAuthContext, Depends(require_fresh_admin)],
) -> AgentBindingDetailResponse:
    binding = await _require_binding(binding_id, repositories)
    if binding.status == "revoked":
        raise TermFlowError("binding_revoked", 409, "The Agent Binding is revoked.")
    await _validate_server_runtime_assignment(
        request,
        binding,
        body.runtime_ref,
        body.capability_ref,
    )
    identity_changed = (
        body.runtime_ref != binding.runtime_ref or body.capability_ref != binding.capability_ref
    )
    try:
        updated = await repositories.agent_bindings.update_desired_runtime(
            binding_id,
            body.runtime_ref,
            body.capability_ref,
            body.expected_revision,
            identity_changed,
        )
    except ValueError as exc:
        raise TermFlowError("invalid_runtime_update", 422, str(exc)) from None
    if updated is None:
        raise TermFlowError(
            "stale_binding_revision",
            409,
            "The Binding revision is stale or its runtime identity conflicts.",
        )
    try:
        await _fence_binding(request, binding_id, rotate_epoch=False)
    except Exception as exc:
        raise TermFlowError(
            "runtime_fence_failed",
            503,
            "The Agent runtime could not be fenced; retry the runtime update.",
        ) from exc
    if updated.status == "enabled":
        try:
            await _reconcile_binding(request, binding_id)
        except Exception:
            logger.exception("Agent Binding runtime reconcile failed")
    current = await _require_binding(binding_id, repositories)
    return await _binding_detail(current, request=request, repositories=repositories)


@router.put(
    "/bindings/{binding_id}/pane-policies",
    response_model=AgentPanePolicyResponse,
    status_code=status.HTTP_200_OK,
)
async def replace_agent_pane_policies(
    binding_id: UUID,
    body: AgentPanePolicyReplaceRequest,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    _auth: Annotated[AdminAuthContext, Depends(require_fresh_admin)],
) -> AgentPanePolicyResponse:
    binding = await _require_binding(binding_id, repositories)
    revision, pane_ids = await _topology_info(request, binding.term_id)
    if revision is None:
        raise TermFlowError("topology_unavailable", 409, "Term topology is unavailable.")
    if revision != body.topology_revision:
        raise TermFlowError("stale_topology", 409, "Term topology revision is stale.")
    if not set(body.pane_ids) <= pane_ids:
        raise TermFlowError("invalid_pane_policy", 422, "Pane policy contains an unknown Pane.")
    async with sessions() as session:
        current = await session.get(AgentBinding, binding_id)
        if current is None:
            raise TermFlowError("binding_not_found", 404, "The Agent Binding does not exist.")
        if body.expected_revision is not None and current.config_revision != body.expected_revision:
            await session.rollback()
            raise TermFlowError(
                "stale_binding_revision",
                409,
                "The Binding revision is stale.",
            )
        await session.execute(delete(PanePolicy).where(PanePolicy.binding_id == binding_id))
        for pane_id in body.pane_ids:
            session.add(PanePolicy(binding_id=binding_id, pane_id=pane_id, allowed=True))
        current.config_revision += 1
        await session.commit()
    # A policy replacement invalidates the currently published pipeline.  The
    # controller will publish a fresh candidate only after its final CAS.
    try:
        await _fence_binding(request, binding_id, rotate_epoch=False)
        if binding.status == "enabled":
            await _reconcile_binding(request, binding_id)
    except Exception:
        logger.exception("Agent Pane Policy reconcile failed")
    return AgentPanePolicyResponse(
        binding_id=binding_id,
        pane_ids=list(body.pane_ids),
        topology_revision=revision,
    )


@router.get(
    "/bindings/{binding_id}/disclosure",
    response_model=AgentProviderDisclosureResponse,
)
async def get_agent_binding_disclosure(
    binding_id: UUID,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentProviderDisclosureResponse:
    binding = await _require_binding(binding_id, repositories)
    profile = await repositories.agent_profiles.get_by_id(binding.profile_id)
    disclosure = await _current_disclosure(
        binding,
        profile,
        _provider_catalog(request),
        repositories,
    )
    if disclosure is None:
        raise TermFlowError(
            "unknown_provider",
            422,
            "The Agent Profile provider or model is unavailable.",
        )
    return disclosure


@router.post(
    "/bindings/{binding_id}/disclosure/accept",
    response_model=AgentProviderDisclosureResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def accept_agent_binding_disclosure(
    binding_id: UUID,
    body: AgentDisclosureAcceptRequest,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    auth: Annotated[AdminAuthContext, Depends(require_fresh_admin)],
) -> AgentProviderDisclosureResponse:
    binding = await _require_binding(binding_id, repositories)
    if binding.status == "revoked":
        raise TermFlowError("binding_revoked", 409, "The Agent Binding is revoked.")
    profile = await repositories.agent_profiles.get_by_id(binding.profile_id)
    catalog = _provider_catalog(request)
    if profile is None:
        raise TermFlowError("profile_not_found", 404, "The Agent Profile does not exist.")
    try:
        config, _ = canonicalize_profile_config(profile.config)
        entry = catalog.resolve(config)
        expected_fingerprint = catalog.disclosure_fingerprint(config)
    except (TermFlowError, ValueError, TypeError):
        raise TermFlowError(
            "unknown_provider",
            422,
            "The Agent Profile provider or model is unavailable.",
        ) from None
    if body.disclosure_fingerprint != expected_fingerprint:
        raise TermFlowError(
            "binding_disclosure_stale",
            409,
            "The provider disclosure fingerprint is stale.",
        )
    accepted_at = datetime.now(UTC)
    actor_kind = auth.credential_kind
    actor_ref = auth.actor_ref
    async with sessions() as session:
        current = await session.scalar(
            select(AgentProviderDisclosureAcceptance).where(
                AgentProviderDisclosureAcceptance.binding_id == binding_id,
                AgentProviderDisclosureAcceptance.disclosure_fingerprint == expected_fingerprint,
                AgentProviderDisclosureAcceptance.revoked_at.is_(None),
            )
        )
        if current is None:
            await session.execute(
                update(AgentProviderDisclosureAcceptance)
                .where(
                    AgentProviderDisclosureAcceptance.binding_id == binding_id,
                    AgentProviderDisclosureAcceptance.revoked_at.is_(None),
                    AgentProviderDisclosureAcceptance.disclosure_fingerprint
                    != expected_fingerprint,
                )
                .values(revoked_at=accepted_at)
            )
            session.add(
                AgentProviderDisclosureAcceptance(
                    binding_id=binding_id,
                    disclosure_fingerprint=expected_fingerprint,
                    provider_id=entry.provider_id,
                    model_id=config.model_id,
                    endpoint_origin=entry.endpoint_origin,
                    region=entry.region,
                    retention_terms=entry.retention_terms,
                    retention_version=entry.retention_version,
                    no_training=entry.no_training,
                    policy_version=entry.policy_version,
                    accepted_at=accepted_at,
                    accepted_auth_epoch=auth.auth_epoch,
                    actor_kind=actor_kind,
                    actor_ref=actor_ref,
                )
            )
            await session.commit()
    try:
        await _fence_binding(request, binding_id, rotate_epoch=False)
        if binding.status == "enabled":
            await _reconcile_binding(request, binding_id)
    except Exception:
        logger.exception("Agent disclosure reconcile failed")
    current_disclosure = await _current_disclosure(
        binding,
        profile,
        catalog,
        repositories,
    )
    if current_disclosure is None:
        raise TermFlowError("disclosure_unavailable", 503, "Disclosure state is unavailable.")
    return current_disclosure


@router.put("/bindings/{binding_id}/write-policy", response_model=AgentBindingResponse)
async def set_agent_binding_write_policy(
    binding_id: UUID,
    request: AgentWritePolicyRequest,
    auth: Annotated[AdminAuthContext, Depends(require_fresh_admin)],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AgentBindingResponse:
    """Switch one Binding between manual and auto write approval.

    Sensitive enough to require recent re-authentication: auto mode removes
    the human gate for every allowlisted write on the binding.
    """

    del auth
    await _require_binding(binding_id, repositories)
    async with sessions() as session:
        result = await session.execute(
            update(AgentBinding)
            .where(AgentBinding.id == binding_id)
            .values(write_policy=request.write_policy, updated_at=datetime.now(UTC))
            .returning(AgentBinding)
        )
        binding = result.scalar_one_or_none()
        await session.commit()
    if binding is None:
        raise TermFlowError("binding_not_found", 404, "The Agent Binding does not exist.")
    return _binding_response(binding)


@router.patch("/bindings/{binding_id}", response_model=AgentBindingResponse)
async def update_agent_binding(
    binding_id: UUID,
    request: AgentBindingUpdateRequest,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AgentBindingResponse:
    await _require_binding(binding_id, repositories)

    binding: AgentBinding | None = None
    if request.status is not None:
        if request.status not in _BINDING_STATUSES:
            raise TermFlowError(
                "invalid_binding_status",
                422,
                "Unknown binding status.",
            )
        binding = await repositories.agent_bindings.set_status(
            binding_id,
            request.status,
            advance_runtime_epoch=request.status in ("revoked", "disabled"),
        )
        if request.status in ("revoked", "disabled"):
            # A revoked/disabled binding must not keep pending or approved
            # approvals alive (spec §5): every request of the binding is
            # revoked so no waiter can ever execute a reviewed write.
            # NOTE: the binding status is already committed at this point.  A
            # revocation failure still fails the call with 409, but the status
            # change stays.  That is safe (not a security gap): CommandService
            # rechecks the binding state before every execution, so a write
            # can never run under a revoked binding even when this sweep
            # missed its approvals.
            shared = getattr(http_request.app.state, "approval_policy", None)
            policy = shared or ApprovalPolicy(repositories, sessions)
            try:
                revoked_count = await policy.revoke_for_binding(binding_id, actor="admin")
            except Exception as exc:
                raise TermFlowError(
                    "binding_approval_revoke_failed",
                    409,
                    "The binding status changed but its approvals could not be revoked.",
                ) from exc
            if revoked_count:
                logger.info("revoked %s approvals of binding %s", revoked_count, binding_id)
            try:
                # The status transaction already advanced the desired epoch;
                # fence with rotation disabled so old mappings/streams are
                # removed exactly once before this compatibility response.
                await _fence_binding(http_request, binding_id, rotate_epoch=False)
            except Exception as exc:
                raise TermFlowError(
                    "runtime_fence_failed",
                    503,
                    "The Agent runtime could not be fenced; retry the operation.",
                ) from exc
    if binding is None:
        raise TermFlowError(
            "invalid_request",
            422,
            "Provide desired status; runtime changes use the runtime endpoint.",
        )
    return _binding_response(binding)


@router.delete("/bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_binding(
    binding_id: UUID,
    http_request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> Response:
    """Delete a binding through the durable tombstone/cleanup path.

    Plan §15/§17 delete contract: the binding's active runs are cancelled
    first, a durable cleanup tombstone is created BEFORE the row deletion,
    and every backend session of the binding's conversations is proven
    deleted before any row goes away.  When the backend deletion cannot be
    proven, the rows stay and the request fails with ``deletion_pending``
    while the tombstone keeps retrying.
    """
    from termflow_control_plane.api.agent_conversations import get_agent_runtime_registry
    from termflow_control_plane.plugins.agent_broker.plugin import (
        BackendRuntimeUnavailableError,
        delete_binding_backend_sessions,
        record_cleanup_failure,
    )

    if await repositories.agent_bindings.get_by_id(binding_id) is None:
        existing = await existing_deletion_response(
            http_request, repositories, "binding", binding_id
        )
        if existing is not None:
            return existing
    await _require_binding(binding_id, repositories)
    registry = get_agent_runtime_registry(http_request)

    # 1) Cancel the binding's active runs before any deletion (plan §17).
    for run in await repositories.agent_runs.list_active_for_binding(binding_id):
        await repositories.agent_runs.set_state(run.id, "cancelled", expected_state=run.run_state)

    # 2) Durable cleanup tombstone BEFORE the row deletion (plan §15), so a
    #    crash mid-delete leaves a retryable job instead of orphaned rows.
    job = await create_deletion_manifest(repositories, target_kind="binding", target_id=binding_id)

    # 3) Prove every backend session of the binding's conversations is gone
    #    before deleting the rows (an OpenCode session is never orphaned).
    try:
        failures = await delete_binding_backend_sessions(repositories, registry, binding_id)
    except BackendRuntimeUnavailableError as exc:
        await record_cleanup_failure(job, repositories, str(exc))
        return pending_response(http_request, job.id)
    if failures:
        await record_cleanup_failure(job, repositories, "; ".join(failures))
        return pending_response(http_request, job.id)

    # 4) Delete the binding row (cascading its conversations and events).
    await repositories.agent_bindings.delete(binding_id)
    await repositories.cleanup_jobs.confirm_pending_internal(job.id)
    await repositories.cleanup_jobs.complete(job.id)
    return await existing_deletion_response(
        http_request, repositories, "binding", binding_id
    ) or Response(status_code=204)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@router.post(
    "/tokens",
    response_model=AgentTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_token(
    request: AgentTokenCreateRequest,
    response: Response,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentTokenCreatedResponse:
    binding = await _require_binding(request.binding_id, repositories)
    expiry_epoch = int(request.expires_at.timestamp())
    if expiry_epoch <= int(datetime.now(UTC).timestamp()):
        raise TermFlowError(
            "invalid_expiry",
            422,
            "expires_at must be in the future.",
        )
    raw_token = issue_token()
    token = await repositories.agent_tokens.create(
        binding_id=binding.id,
        token_hash=hash_token(raw_token),
        scopes=tuple(request.scopes),
        expiry_epoch=expiry_epoch,
        binding_epoch=binding.runtime_epoch or 1,
    )
    # Secrets must never be cached by intermediaries or browsers.
    response.headers["Cache-Control"] = "no-store"
    return AgentTokenCreatedResponse(
        token_id=token.id,
        binding_id=token.binding_id,
        scopes=list(request.scopes),
        expires_at=datetime.fromtimestamp(token.expiry_epoch, tz=UTC),
        raw_token=raw_token,
        created_at=token.created_at,
    )


@router.get("/tokens", response_model=AgentTokenListResponse)
async def list_agent_tokens(
    binding_id: Annotated[UUID, Query()],
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
) -> AgentTokenListResponse:
    await _require_binding(binding_id, repositories)
    tokens = await repositories.agent_tokens.list_for_binding(binding_id)
    return AgentTokenListResponse(tokens=[_token_response(token) for token in tokens])


@router.post("/tokens/{token_id}/revoke", response_model=AgentTokenResponse)
async def revoke_agent_token(
    token_id: UUID,
    response: Response,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AgentTokenResponse:
    token = await _get_agent_token(sessions, token_id)
    if token is None:
        raise TermFlowError("token_not_found", 404, "The Agent Token does not exist.")
    await _revoke_agent_token(sessions, token_id)
    revoked = await _get_agent_token(sessions, token_id)
    assert revoked is not None
    response.headers["Cache-Control"] = "no-store"
    return _token_response(revoked)
