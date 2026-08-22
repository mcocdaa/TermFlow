from pathlib import Path

SCRIPT = Path("scripts/security/verify-public-edge.sh")
REQUIRED_CONTROLS = (
    "308",
    "Strict-Transport-Security",
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "/.well-known/oauth-authorization-server",
)


def test_public_edge_script_locks_every_live_check() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for control in REQUIRED_CONTROLS:
        assert control in source, f"missing live check: {control}"


def test_public_edge_script_refuses_redirects_and_requires_fail_fast_curl() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '--location' not in source
    assert "--fail" in source
