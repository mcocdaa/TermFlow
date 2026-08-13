"""MCP tool handlers (plan §10, §12.1; tasks M2.3, M4.4, M5.2).

These pure handler functions implement the MCP tool surface:

- ``termflow_list_panes`` omits sensitive titles/cwd unless the pane is
  explicitly allowlisted in ``pane_policies``.
- ``termflow_pane_read`` rechecks the ``terminal.observe`` allowlist, then
  delegates to the ObservationService port
  (``agent.terminal_ports.TerminalObservationPort``) for pane existence,
  incarnation, and byte bounds.
- ``termflow_pane_send_text`` / ``termflow_pane_send_keys`` run through the
  approval-gated :class:`TerminalCommandPort` (``CommandService``): the
  handler enforces the write scope, the pane allowlist, and conversation
  ownership, and the service owns the persistent single-use approval flow.
- ``termflow_watch_create/list/get/cancel`` run through the
  :class:`~termflow_control_plane.plugins.agent_broker.agent.terminal_ports.ContinuationPort`
  with the same pane policy gate on creation.

Every failure is a :class:`TermFlowToolError` with a stable
``TermFlowErrorCode``; the MCP server adapter (M4/M5) translates it into a
structured MCP error.
"""

from __future__ import annotations

from uuid import UUID

from termflow_protocol.mcp import (
    ListPanesResult,
    PaneReadParams,
    PaneReadResult,
    PaneSendKeysParams,
    PaneSendKeysResult,
    PaneSendTextParams,
    PaneSendTextResult,
    PaneSummary,
    TermFlowErrorCode,
    WatchCancelParams,
    WatchCancelResult,
    WatchCreateParams,
    WatchCreateResult,
    WatchGetParams,
    WatchGetResult,
    WatchListParams,
    WatchListResult,
)

from termflow_control_plane.persistence.repositories import (
    PanePolicyRepository,
    RepositoryBundle,
)
from termflow_control_plane.plugins.agent_broker.agent.terminal_ports import (
    ContinuationPort,
    TermFlowToolError,
    TerminalCommandPort,
    TerminalObservationPort,
)
from termflow_control_plane.plugins.agent_broker.auth import (
    SCOPE_TERMINAL_OBSERVE,
    SCOPE_TERMINAL_WRITE,
    AgentTokenPrincipal,
)

#: Pane id row that expresses explicit all-panes observe consent (§10:
#: ``all_panes`` requires explicit Term/Binding consent, represented as an
#: allowed policy row on this sentinel id).
ALL_PANES_CONSENT_PANE = "*"


def _require_observe_scope(principal: AgentTokenPrincipal) -> None:
    """Enforce the token scope: reads and watch management need observe."""
    if SCOPE_TERMINAL_OBSERVE not in principal.scopes:
        raise TermFlowToolError(
            TermFlowErrorCode.POLICY_DENIED,
            "the agent token lacks the terminal.observe scope",
        )


def _require_write_scope(principal: AgentTokenPrincipal) -> None:
    """Enforce the token scope: writes need the terminal.write scope.

    The HTTP auth gate only requires ``terminal.observe``; the write tools
    enforce the write scope inside the handler so a token with observe-only
    scopes fails closed at the tool boundary (spec §6 double gate).
    """
    if SCOPE_TERMINAL_WRITE not in principal.scopes:
        raise TermFlowToolError(
            TermFlowErrorCode.POLICY_DENIED,
            "the agent token lacks the terminal.write scope",
        )


async def _require_owned_conversation(
    repositories: RepositoryBundle,
    principal: AgentTokenPrincipal,
    conversation_id: UUID,
) -> None:
    """The conversation must belong to the principal's binding (spec §1)."""
    conversation = await repositories.agent_conversations.get_by_id(conversation_id)
    if conversation is None or conversation.binding_id != principal.binding_id:
        raise TermFlowToolError(
            TermFlowErrorCode.POLICY_DENIED,
            "the conversation does not belong to this binding",
        )


async def _require_writable_pane(
    repositories: RepositoryBundle,
    principal: AgentTokenPrincipal,
    pane_id: str,
) -> None:
    """A pane that cannot be observed can never be written (spec §1)."""
    if not await pane_observe_allowed(
        repositories.pane_policies, principal.binding_id, pane_id
    ):
        raise TermFlowToolError(
            TermFlowErrorCode.POLICY_DENIED,
            f"the binding is not allowed to write pane {pane_id}",
        )


async def pane_observe_allowed(
    pane_policies: PanePolicyRepository,
    binding_id: UUID,
    pane_id: str,
) -> bool:
    """Exact pane allowlist check with the explicit ``all_panes`` consent.

    An explicit row for the pane wins (a deny row overrides the consent);
    otherwise the sentinel row decides; with no rows at all the result is
    deny by default.
    """
    explicit = await pane_policies.pane_allowed(binding_id, pane_id)
    if explicit is not None:
        return explicit
    consent = await pane_policies.pane_allowed(binding_id, ALL_PANES_CONSENT_PANE)
    if consent is not None:
        return consent
    return False


async def _pane_title_visible(
    pane_policies: PanePolicyRepository,
    binding_id: UUID,
    pane_id: str,
) -> bool:
    # Titles are sensitive and are only shown for explicitly allowlisted
    # panes; the all-panes consent never widens title exposure (§9.3).
    return await pane_policies.pane_allowed(binding_id, pane_id) is True


async def handle_list_panes(
    principal: AgentTokenPrincipal,
    *,
    observation: TerminalObservationPort,
    repositories: RepositoryBundle,
) -> ListPanesResult:
    """List the Term's panes, omitting sensitive titles unless allowed."""
    _require_observe_scope(principal)
    panes = await observation.list_panes(principal.instance_id)
    summaries: list[PaneSummary] = []
    for pane in panes:
        title = (
            pane.title
            if await _pane_title_visible(
                repositories.pane_policies, principal.binding_id, pane.pane_id
            )
            else None
        )
        summaries.append(
            PaneSummary(
                pane_id=pane.pane_id,
                index=pane.index,
                title=title,
                active=pane.active,
                dead=pane.dead,
            )
        )
    return ListPanesResult(instance_id=principal.instance_id, panes=summaries)


async def handle_pane_read(
    principal: AgentTokenPrincipal,
    params: PaneReadParams,
    *,
    observation: TerminalObservationPort,
    repositories: RepositoryBundle,
) -> PaneReadResult:
    """Read one pane after the observe policy and pane identity checks."""
    _require_observe_scope(principal)
    if not await pane_observe_allowed(
        repositories.pane_policies, principal.binding_id, params.pane_id
    ):
        raise TermFlowToolError(
            TermFlowErrorCode.POLICY_DENIED,
            f"the binding is not allowed to observe pane {params.pane_id}",
        )
    return await observation.read_pane(principal.instance_id, params)


async def handle_pane_send_text(
    principal: AgentTokenPrincipal,
    params: PaneSendTextParams,
    *,
    commands: TerminalCommandPort,
    repositories: RepositoryBundle,
    tool_call_id: str,
) -> PaneSendTextResult:
    """Send literal text to a pane through the approval flow (spec §1)."""
    _require_write_scope(principal)
    await _require_writable_pane(repositories, principal, params.pane_id)
    await _require_owned_conversation(repositories, principal, params.conversation_id)
    return await commands.send_text(principal, params, tool_call_id=tool_call_id)


async def handle_pane_send_keys(
    principal: AgentTokenPrincipal,
    params: PaneSendKeysParams,
    *,
    commands: TerminalCommandPort,
    repositories: RepositoryBundle,
    tool_call_id: str,
) -> PaneSendKeysResult:
    """Send a named-key sequence to a pane through the approval flow (spec §1)."""
    _require_write_scope(principal)
    await _require_writable_pane(repositories, principal, params.pane_id)
    await _require_owned_conversation(repositories, principal, params.conversation_id)
    return await commands.send_keys(principal, params, tool_call_id=tool_call_id)


async def handle_watch_create(
    principal: AgentTokenPrincipal,
    params: WatchCreateParams,
    *,
    continuation: ContinuationPort,
    repositories: RepositoryBundle,
) -> WatchCreateResult:
    """Create a watch after the observe policy and conversation checks."""
    _require_observe_scope(principal)
    if not await pane_observe_allowed(
        repositories.pane_policies, principal.binding_id, params.pane_id
    ):
        raise TermFlowToolError(
            TermFlowErrorCode.POLICY_DENIED,
            f"the binding is not allowed to watch pane {params.pane_id}",
        )
    conversation = await repositories.agent_conversations.get_by_id(
        params.conversation_id
    )
    if conversation is None or conversation.binding_id != principal.binding_id:
        raise TermFlowToolError(
            TermFlowErrorCode.POLICY_DENIED,
            "the conversation does not belong to this binding",
        )
    return await continuation.create_watch(principal.binding_id, params)


async def handle_watch_list(
    principal: AgentTokenPrincipal,
    params: WatchListParams,
    *,
    continuation: ContinuationPort,
    repositories: RepositoryBundle,
) -> WatchListResult:
    """List the binding's watches; the binding scope is inherent."""
    _require_observe_scope(principal)
    return await continuation.list_watches(principal.binding_id, params)


async def handle_watch_get(
    principal: AgentTokenPrincipal,
    params: WatchGetParams,
    *,
    continuation: ContinuationPort,
    repositories: RepositoryBundle,
) -> WatchGetResult:
    """Fetch one watch; other bindings' watches are ``watch_not_found``."""
    _require_observe_scope(principal)
    return await continuation.get_watch(principal.binding_id, params)


async def handle_watch_cancel(
    principal: AgentTokenPrincipal,
    params: WatchCancelParams,
    *,
    continuation: ContinuationPort,
    repositories: RepositoryBundle,
) -> WatchCancelResult:
    """Cancel one watch; other bindings' watches are ``watch_not_found``."""
    _require_observe_scope(principal)
    return await continuation.cancel_watch(principal.binding_id, params)
