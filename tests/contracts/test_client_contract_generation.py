import subprocess
import sys
from enum import Enum
from pathlib import Path
from runpy import run_path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts/generate-client-contracts/generate.py"
GENERATED = ROOT / "packages/client-contracts/src/generated.ts"


def _run_generator(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_checked_in_client_contracts_match_python_models_and_render_enums(
    tmp_path: Path,
) -> None:
    rendered_path = tmp_path / "generated.ts"
    result = _run_generator("--output", str(rendered_path))
    assert result.returncode == 0, result.stderr

    rendered = rendered_path.read_text()
    assert rendered == GENERATED.read_text()
    assert "export interface BrowserSessionResponse" in rendered
    assert "export interface BrowserSessionDeleteResponse" in rendered
    assert "export interface BrowserSessionChallengeResponse" in rendered
    assert "export interface TotpStatusResponse" in rendered
    assert "configured: boolean" in rendered
    assert "export interface TotpSetupResponse" in rendered
    assert "export interface OAuthMetadataResponse" in rendered
    assert "export interface OAuthAuthorizationPreviewResponse" in rendered
    assert "export interface OAuthAuthorizationDecisionResponse" in rendered
    assert "export interface OAuthTokenResponse" in rendered
    assert "export interface OAuthRevokeResponse" in rendered
    assert "export interface CliTokenResponse" in rendered
    assert "export interface NativeClientResponse" in rendered
    assert "export interface NativeClientListResponse" in rendered
    assert "export interface NativeClientDeleteResponse" in rendered
    assert (
        'grant_types_supported: ("authorization_code" | "refresh_token" | '
        '"urn:ietf:params:oauth:grant-type:device_code")[]'
    ) in rendered
    assert "export interface ErrorEnvelope" in rendered
    assert "server_url: string" in rendered
    assert "login_command: string" in rendered
    assert "export interface ErrorDetail" in rendered
    assert "export const PROTOCOL_VERSION = 1 as const" in rendered
    assert "expires_at: string" in rendered
    assert "expires_at?: string" not in rendered
    assert "export type TerminalAction =" in rendered
    assert "gap?:" not in rendered
    assert "admin_token" not in rendered
    assert "totp_code" not in rendered
    assert "code_verifier" not in rendered
    assert "refresh_token: string" in rendered
    assert "export interface TotpSetupRequest" not in rendered
    assert "export interface OAuthTokenRequest" not in rendered

    # Agent capability discovery.
    assert "export interface AgentCapabilitiesResponse" in rendered
    assert "agent_broker_enabled: boolean" in rendered

    # Agent conversation, message, and history-event API types.
    assert "export interface AgentConversationCreateRequest" in rendered
    assert "export interface AgentConversationResponse" in rendered
    assert "export interface AgentConversationListResponse" in rendered
    assert "export interface AgentConversationBindingInfo" in rendered
    assert "export interface AgentConversationDetailResponse" in rendered
    assert "binding: AgentConversationBindingInfo" in rendered
    assert "export interface AgentMessageResponse" in rendered
    assert "export interface AgentMessageListResponse" in rendered
    assert "export interface AgentEventResponse" in rendered
    assert "export interface AgentEventListResponse" in rendered
    assert "next_cursor: number | null" in rendered

    # Agent admin profile, binding, and token API types.
    assert "export interface AgentProfileCreateRequest" in rendered
    assert "export interface AgentProfileResponse" in rendered
    assert "export interface AgentProfileListResponse" in rendered
    assert "export interface AgentBindingResponse" in rendered
    assert "export interface AgentBindingListResponse" in rendered
    assert "export interface AgentTokenCreateRequest" in rendered
    assert "export interface AgentTokenCreatedResponse" in rendered
    assert "raw_token: string" in rendered
    assert "export interface AgentTokenResponse" in rendered
    assert "export interface AgentTokenListResponse" in rendered

    # Canonical Agent Input/Event wire models and their kind unions.
    assert "export interface UserMessageInput" in rendered
    assert "export interface WatchTriggeredInput" in rendered
    assert "export interface TimerTriggeredInput" in rendered
    assert "export interface PermissionResolvedInput" in rendered
    assert "export interface SystemNotificationInput" in rendered
    assert "export interface RunStartedEvent" in rendered
    assert "export interface MessageDeltaEvent" in rendered
    assert "export interface MessageCompletedEvent" in rendered
    assert "export interface ToolStartedEvent" in rendered
    assert "export interface ToolCompletedEvent" in rendered
    assert "export interface PermissionRequestedEvent" in rendered
    assert "export interface RunCompletedEvent" in rendered
    assert "export interface RunFailedEvent" in rendered
    assert "export interface BackendStateChangedEvent" in rendered
    assert (
        "export type AgentInput = UserMessageInput | WatchTriggeredInput | "
        "TimerTriggeredInput | PermissionResolvedInput | SystemNotificationInput"
    ) in rendered
    assert (
        "export type AgentEvent = RunStartedEvent | MessageDeltaEvent | "
        "MessageCompletedEvent | ToolStartedEvent | ToolCompletedEvent | "
        "PermissionRequestedEvent | RunCompletedEvent | RunFailedEvent | "
        "BackendStateChangedEvent"
    ) in rendered
    assert 'kind: "user_message"' in rendered
    assert 'kind: "backend_state_changed"' in rendered
    assert 'delivery_state: "pending" | "delivered" | "failed" | "dead_lettered"' in rendered
    assert 'status: "success" | "error" | "cancelled" | "unknown"' in rendered
    assert (
        'state: "connecting" | "ready" | "unavailable" | "context_lost" | "reconciling" | "closed"'
    ) in rendered
    assert (
        'actor_kind: "user_session" | "client" | "watch_engine" | "timer" | "backend" | "system"'
    ) in rendered

    checked = _run_generator("--check")
    assert checked.returncode == 0, checked.stderr

    render_type = run_path(str(GENERATOR))["_render_type"]

    class ExampleString(Enum):
        FIRST = "first"
        SECOND = "second"

    class ExampleNumber(Enum):
        FIRST = 1
        SECOND = 2

    assert render_type(ExampleString) == '"first" | "second"'
    assert render_type(ExampleNumber) == "1 | 2"
