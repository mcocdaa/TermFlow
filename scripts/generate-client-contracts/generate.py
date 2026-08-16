#!/usr/bin/env python3
"""Render the browser-visible TypeScript contract from public Pydantic models."""

from __future__ import annotations

import argparse
import json
import sys
import types
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, TypeAliasType, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel
from termflow_control_plane.api.agent_admin import (
    AgentBindingCreateRequest,
    AgentBindingListResponse,
    AgentBindingResponse,
    AgentBindingUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileListResponse,
    AgentProfileResponse,
    AgentProfileUpdateRequest,
    AgentTokenCreatedResponse,
    AgentTokenCreateRequest,
    AgentTokenListResponse,
    AgentTokenResponse,
)
from termflow_control_plane.api.agent_approvals import (
    ApprovalBindingInfo,
    ApprovalDecisionRequest,
    ApprovalDetailResponse,
    ApprovalListResponse,
    ApprovalResponse,
)
from termflow_control_plane.api.agent_capabilities import AgentCapabilitiesResponse
from termflow_control_plane.api.agent_conversations import (
    AgentCancelRequest,
    AgentCancelResponse,
    AgentConversationBindingInfo,
    AgentConversationCreateRequest,
    AgentConversationDetailResponse,
    AgentConversationListResponse,
    AgentConversationResponse,
    AgentEventListResponse,
    AgentEventResponse,
    AgentMessageListResponse,
    AgentMessageResponse,
    AgentSubmitMessageRequest,
    AgentSubmitMessageResponse,
)
from termflow_protocol import (
    PROTOCOL_VERSION,
    AgentEvent,
    AgentInput,
    BackendStateChangedEvent,
    BackendStateChangedPayload,
    BrowserSessionChallengeResponse,
    BrowserSessionDeleteResponse,
    BrowserSessionResponse,
    CliTokenResponse,
    ComputerListResponse,
    ComputerSummary,
    DashboardMetrics,
    DashboardResponse,
    EnrollmentCreateResponse,
    ErrorDetail,
    ErrorEnvelope,
    MessageCompletedEvent,
    MessageCompletedPayload,
    MessageDeltaEvent,
    MessageDeltaPayload,
    NativeClientDeleteResponse,
    NativeClientListResponse,
    NativeClientResponse,
    NativeClientUpdateRequest,
    OAuthAuthorizationDecisionResponse,
    OAuthAuthorizationPreviewResponse,
    OAuthAuthorizationRequest,
    OAuthDeviceCodeRequest,
    OAuthDeviceCodeResponse,
    OAuthDeviceTokenError,
    OAuthDeviceTokenErrorCode,
    OAuthMetadataResponse,
    OAuthPublicJwk,
    OAuthRevokeResponse,
    OAuthScope,
    OAuthTokenResponse,
    PaneSnapshot,
    PermissionRequestedEvent,
    PermissionRequestedPayload,
    PermissionResolvedInput,
    PermissionResolvedPayload,
    RunCompletedEvent,
    RunCompletedPayload,
    RunFailedEvent,
    RunFailedPayload,
    RunStartedEvent,
    RunStartedPayload,
    SystemNotificationInput,
    SystemNotificationPayload,
    TerminalAction,
    TerminalActionResultFrame,
    TerminalBinding,
    TerminalBindingSnapshotFrame,
    TerminalClosedFrame,
    TerminalCloseReason,
    TerminalErrorFrame,
    TerminalReadyFrame,
    TerminalSizeFrame,
    TermSummary,
    TimerTriggeredInput,
    TimerTriggeredPayload,
    ToolCompletedEvent,
    ToolCompletedPayload,
    ToolStartedEvent,
    ToolStartedPayload,
    TopologyResponse,
    TopologySnapshot,
    TotpSetupResponse,
    TotpStatusResponse,
    UserMessageInput,
    UserMessagePayload,
    WatchTriggeredInput,
    WatchTriggeredPayload,
    WindowSnapshot,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "packages/client-contracts/src/generated.ts"

# AgentInput/AgentEvent are Annotated discriminated unions rather than PEP 695
# aliases; wrap them so the existing alias renderer emits them by name.
AgentInputUnion: TypeAliasType = TypeAliasType("AgentInput", AgentInput)
AgentEventUnion: TypeAliasType = TypeAliasType("AgentEvent", AgentEvent)

TYPE_ALIASES: tuple[TypeAliasType, ...] = (
    OAuthScope,
    OAuthDeviceTokenErrorCode,
    TerminalAction,
    TerminalCloseReason,
    AgentInputUnion,
    AgentEventUnion,
)
CONSTANTS: tuple[tuple[str, object], ...] = (("PROTOCOL_VERSION", PROTOCOL_VERSION),)
MODELS: tuple[type[BaseModel], ...] = (
    ErrorDetail,
    ErrorEnvelope,
    BrowserSessionResponse,
    BrowserSessionDeleteResponse,
    BrowserSessionChallengeResponse,
    TotpStatusResponse,
    TotpSetupResponse,
    OAuthPublicJwk,
    OAuthMetadataResponse,
    OAuthAuthorizationRequest,
    OAuthDeviceCodeRequest,
    OAuthDeviceCodeResponse,
    OAuthDeviceTokenError,
    OAuthAuthorizationPreviewResponse,
    OAuthAuthorizationDecisionResponse,
    OAuthTokenResponse,
    OAuthRevokeResponse,
    CliTokenResponse,
    NativeClientResponse,
    NativeClientListResponse,
    NativeClientUpdateRequest,
    NativeClientDeleteResponse,
    DashboardMetrics,
    TermSummary,
    ComputerSummary,
    ComputerListResponse,
    DashboardResponse,
    EnrollmentCreateResponse,
    PaneSnapshot,
    WindowSnapshot,
    TopologySnapshot,
    TopologyResponse,
    TerminalBinding,
    TerminalReadyFrame,
    TerminalSizeFrame,
    TerminalBindingSnapshotFrame,
    TerminalErrorFrame,
    TerminalClosedFrame,
    TerminalActionResultFrame,
    AgentCapabilitiesResponse,
    AgentConversationCreateRequest,
    AgentConversationResponse,
    AgentConversationListResponse,
    AgentConversationBindingInfo,
    AgentConversationDetailResponse,
    AgentMessageResponse,
    AgentMessageListResponse,
    AgentSubmitMessageRequest,
    AgentSubmitMessageResponse,
    AgentCancelRequest,
    AgentCancelResponse,
    AgentEventResponse,
    AgentEventListResponse,
    ApprovalResponse,
    ApprovalBindingInfo,
    ApprovalDetailResponse,
    ApprovalListResponse,
    ApprovalDecisionRequest,
    AgentProfileCreateRequest,
    AgentProfileUpdateRequest,
    AgentProfileResponse,
    AgentProfileListResponse,
    AgentBindingCreateRequest,
    AgentBindingUpdateRequest,
    AgentBindingResponse,
    AgentBindingListResponse,
    AgentTokenCreateRequest,
    AgentTokenCreatedResponse,
    AgentTokenResponse,
    AgentTokenListResponse,
    UserMessagePayload,
    WatchTriggeredPayload,
    TimerTriggeredPayload,
    PermissionResolvedPayload,
    SystemNotificationPayload,
    UserMessageInput,
    WatchTriggeredInput,
    TimerTriggeredInput,
    PermissionResolvedInput,
    SystemNotificationInput,
    RunStartedPayload,
    MessageDeltaPayload,
    MessageCompletedPayload,
    ToolStartedPayload,
    ToolCompletedPayload,
    PermissionRequestedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    BackendStateChangedPayload,
    RunStartedEvent,
    MessageDeltaEvent,
    MessageCompletedEvent,
    ToolStartedEvent,
    ToolCompletedEvent,
    PermissionRequestedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    BackendStateChangedEvent,
)
EXPORTED_ALIAS_IDS = {id(alias): alias.__name__ for alias in TYPE_ALIASES}


def _literal(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    raise TypeError(f"unsupported Literal value: {value!r}")


def _render_type(annotation: object) -> str:
    exported_alias = EXPORTED_ALIAS_IDS.get(id(annotation))
    if exported_alias is not None:
        return exported_alias
    if isinstance(annotation, TypeAliasType):
        return _render_type(annotation.__value__)
    if annotation in (str, UUID, datetime):
        return "string"
    if annotation in (int, float):
        return "number"
    if annotation is bool:
        return "boolean"
    if annotation is type(None):
        return "null"

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return _render_type(arguments[0])
    if origin is Literal:
        return " | ".join(_literal(value) for value in arguments)
    if origin is list:
        item_type = _render_type(arguments[0])
        if " | " in item_type:
            item_type = f"({item_type})"
        return f"{item_type}[]"
    if origin in (types.UnionType, Union):
        return " | ".join(_render_type(argument) for argument in arguments)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return " | ".join(_literal(member.value) for member in annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__
    raise TypeError(f"unsupported contract annotation: {annotation!r}")


def render() -> str:
    lines = [
        "/* This file is generated by scripts/generate-client-contracts/generate.py. */",
        "/* Do not edit it by hand. */",
        "",
    ]
    for alias in TYPE_ALIASES:
        lines.append(f"export type {alias.__name__} = {_render_type(alias.__value__)}")
    for name, value in CONSTANTS:
        lines.append(f"export const {name} = {_literal(value)} as const")
    lines.append("")
    for model in MODELS:
        lines.append(f"export interface {model.__name__} {{")
        for name, field in model.model_fields.items():
            lines.append(f"  {name}: {_render_type(field.annotation)}")
        lines.extend(("}", ""))
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    output: Path = arguments.output
    rendered = render()
    if arguments.check:
        if not output.is_file() or output.read_text() != rendered:
            print(f"generated client contract is stale: {output}", file=sys.stderr)
            return 1
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
