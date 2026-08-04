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


def test_checked_in_client_contracts_match_python_models(tmp_path: Path) -> None:
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
    assert 'grant_types_supported: ("authorization_code" | "refresh_token" | "urn:ietf:params:oauth:grant-type:device_code")[]' in rendered
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

    checked = _run_generator("--check")
    assert checked.returncode == 0, checked.stderr


def test_annotation_renderer_supports_string_and_number_enums() -> None:
    render_type = run_path(str(GENERATOR))["_render_type"]

    class ExampleString(Enum):
        FIRST = "first"
        SECOND = "second"

    class ExampleNumber(Enum):
        FIRST = 1
        SECOND = 2

    assert render_type(ExampleString) == '"first" | "second"'
    assert render_type(ExampleNumber) == "1 | 2"
