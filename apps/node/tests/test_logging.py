import json
import os
from pathlib import Path

from termflow_node import logging as termflow_logging


def test_log_path_uses_platform_log_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(termflow_logging, "user_log_path", lambda _name: tmp_path / "termflow-log")

    path = termflow_logging.configure_logging()
    termflow_logging.log_event("metadata_success", issuer="https://relay.example.com")

    assert path == tmp_path / "termflow-log" / "termflow.log"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["component"] == "node"
    assert record["event"] == "metadata_success"
    assert record["issuer"] == "https://relay.example.com"
    assert record["timestamp"].endswith("Z")
    if os.name != "nt":
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_log_event_drops_secret_fields_and_terminal_content(tmp_path: Path) -> None:
    path = termflow_logging.configure_logging(tmp_path)
    termflow_logging.log_event(
        "bridge_started",
        installation_token="installation-secret",
        token="token-secret",
        text="terminal-body-secret",
        instance_id="instance-1",
    )

    rendered = path.read_text(encoding="utf-8")
    assert "installation-secret" not in rendered
    assert "token-secret" not in rendered
    assert "terminal-body-secret" not in rendered
    assert "instance-1" in rendered


def test_log_rotation_keeps_five_backups(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(termflow_logging, "LOG_MAX_BYTES", 180)
    path = termflow_logging.configure_logging(tmp_path)

    for index in range(30):
        termflow_logging.log_event("bridge_event", instance_id=f"instance-{index}")

    backups = sorted(tmp_path.glob("termflow.log.*"))
    assert len(backups) <= 5
    assert path.exists()
