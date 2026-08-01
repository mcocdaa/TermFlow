from pathlib import Path


def test_v1_repository_has_required_delivery_artifacts() -> None:
    required = [
        "uv.lock",
        "deploy/Dockerfile.control-plane",
        "deploy/compose.yaml",
        "docs/architecture.md",
        "docs/protocol.md",
        "docs/security.md",
        "docs/api-examples.md",
        "docs/troubleshooting.md",
        ".github/workflows/ci.yml",
        "scripts/verify.sh",
    ]
    assert [path for path in required if not Path(path).is_file()] == []


def test_verify_script_is_non_destructive_and_runs_all_gates() -> None:
    script = Path("scripts/verify.sh").read_text()
    for required in ("pytest -q", "ruff check .", "mypy", "docker compose"):
        assert required in script
    for forbidden in ("rm -", "docker stop", "kill-server"):
        assert forbidden not in script
