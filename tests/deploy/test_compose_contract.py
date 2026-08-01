from pathlib import Path

import yaml


def test_compose_is_single_worker_and_persists_only_metadata() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    service = compose["services"]["control-plane"]
    assert "--workers" not in " ".join(service["command"])
    assert service["volumes"] == ["termflow-data:/app/data"]
    assert service["healthcheck"]["test"][-1].endswith("/healthz")
    assert list(compose["services"]) == ["control-plane"]
