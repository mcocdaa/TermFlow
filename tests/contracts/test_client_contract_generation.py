import subprocess
import sys
from pathlib import Path

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
    assert "expires_at: string" in rendered
    assert "expires_at?: string" not in rendered
    assert "export type TerminalAction =" in rendered
    assert "gap?:" not in rendered

    checked = _run_generator("--check")
    assert checked.returncode == 0, checked.stderr
