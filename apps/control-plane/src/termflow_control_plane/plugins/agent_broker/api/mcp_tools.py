"""Observe-only MCP tool handlers (plan §10, task M2.3).

These pure handler functions implement the observe-only tool surface:

- ``termflow_list_panes`` omits sensitive titles/cwd unless the pane is
  explicitly allowlisted in ``pane_policies``.
- ``termflow_pane_read`` rechecks the ``terminal.observe`` allowlist, then
  delegates to the ObservationService port
  (``agent.terminal_ports.TerminalObservationPort``) for pane existence,
  incarnation, and byte bounds.
- ``termflow_watch_create/list/get/cancel`` run through the
  :class:`~termflow_control_plane.plugins.agent_broker.agent.terminal_ports.ContinuationPort`
  with the same pane policy gate on creation.
- The write tools are intentionally unavailable in this milestone: the
  handlers raise ``policy_denied`` for every token, observe-only or not
  (writes land with M5).

Every failure is a :class:`TermFlowToolError` with a stable
``TermFlowErrorCode``; the MCP server adapter (M4/M5) translates it into a
structured MCP error.
"""

from __future__ import annotations

from typing import NoReturn
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


async def _deny_write(principal: AgentTokenPrincipal, tool_name: str) -> NoReturn:
    """Write tools are unavailable in the observe-only milestone (plan §12).

    Observe-only tokens are denied for lack of the ``terminal.write`` scope;
    even a write-scoped token cannot write yet because the approval-gated
    command path lands with M5.  Both cases fail closed with
    ``policy_denied`` so the client never falls through to a write.
    """
    if SCOPE_TERMINAL_WRITE not in principal.scopes:
        raise TermFlowToolError(
            TermFlowErrorCode.POLICY_DENIED,
            f"{tool_name} requires the terminal.write scope, which "
            "observe-only tokens do not carry",
        )
    raise TermFlowToolError(
        TermFlowErrorCode.POLICY_DENIED,
        f"{tool_name} is not available yet: the approval-gated pane write "
        "path lands with M5",
    )


async def handle_pane_send_text(
    principal: AgentTokenPrincipal,
    params: PaneSendTextParams,
    *,
    repositories: RepositoryBundle,
) -> PaneSendTextResult:
    """Unavailable in the observe-only milestone; writes land with M5."""
    await _deny_write(principal, "termflow_pane_send_text")


async def handle_pane_send_keys(
    principal: AgentTokenPrincipal,
    params: PaneSendKeysParams,
    *,
    repositories: RepositoryBundle,
) -> PaneSendKeysResult:
    """Unavailable in the observe-only milestone; writes land with M5."""
    await _deny_write(principal, "termflow_pane_send_keys")


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
