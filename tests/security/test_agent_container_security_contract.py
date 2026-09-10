"""Static contract for the v0.2 agent-container evidence gates."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_agent_container_security_script_is_present_and_audits_exact_controls() -> None:
    script = REPOSITORY_ROOT / "scripts" / "security" / "verify-agent-containers.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    content = script.read_text(encoding="utf-8")
    for marker in (
        "opencode-agent",
        "opencode-init",
        "NetworkMode",
        "CapDrop",
        "ReadonlyRootfs",
        "@sha256:",
        "no-new-privileges",
    ):
        assert marker in content


def test_opt_in_real_container_e2e_module_is_present() -> None:
    module = REPOSITORY_ROOT / "tests" / "e2e" / "test_agent_opencode_container.py"
    assert module.is_file()
    content = module.read_text(encoding="utf-8")
    assert "TERMFLOW_E2E_OPENCODE" in content
    assert "global/health" in content
    assert "session" in content
