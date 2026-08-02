#!/usr/bin/env python3
"""Create one disposable Computer and Term for the browser acceptance run."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pexpect  # type: ignore[import-untyped]
from termflow_node.config.store import ConfigStore
from termflow_node.instances.store import InstanceStore
from termflow_node.tmux.runner import TmuxRunner


def main() -> None:
    base_url = os.environ["TERMFLOW_E2E_BASE_URL"]
    admin_token = os.environ["TERMFLOW_E2E_ADMIN_TOKEN"]
    repo = Path(__file__).resolve().parents[1]
    enrollment = httpx.post(
        f"{base_url}/api/v1/enrollment-tokens",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=3,
    )
    enrollment.raise_for_status()
    code = enrollment.json()["token"]
    login = subprocess.run(
        [
            str(repo / ".venv/bin/termflow"),
            "login",
            "--server",
            base_url,
            "--code",
            code,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if login.returncode != 0:
        raise RuntimeError("disposable Computer login failed")

    store = InstanceStore.default()
    before = {instance.instance_id for instance in store.list().instances}
    child = pexpect.spawn(
        str(repo / ".venv/bin/termflow"),
        ["new", "--name", "resume-terminal"],
        timeout=8,
        encoding=None,
        dimensions=(60, 200),
    )
    deadline = time.monotonic() + 8
    instance = None
    while time.monotonic() < deadline:
        candidates = [item for item in store.list().instances if item.instance_id not in before]
        if candidates and candidates[0].lifecycle == "running":
            instance = candidates[0]
            break
        if not child.isalive():
            raise RuntimeError("disposable Term exited before registration")
        time.sleep(0.05)
    if instance is None:
        child.close(force=True)
        raise TimeoutError("disposable Term did not become ready")
    child.send(b"\x02d")
    child.expect(pexpect.EOF, timeout=5)
    tmux = TmuxRunner(instance.socket_path)
    tmux.run_command("resize-window", "-x", "240", "-y", "80")
    tmux.run_command("set-option", "-g", "mouse", "on")

    offline_ids: dict[str, str] = {}
    installation = ConfigStore.default().load()
    for project in ("desktop", "mobile-portrait", "mobile-landscape"):
        offline_id = uuid4()
        registration = httpx.post(
            f"{base_url}/api/v1/instances/register",
            headers={
                "Authorization": "Bearer "
                + installation.installation_token.get_secret_value()
            },
            json={"instance_id": str(offline_id), "name": f"offline-{project}"},
            timeout=3,
        )
        registration.raise_for_status()
        offline_ids[project] = str(offline_id)

    print(
        json.dumps(
            {
                "online_term_id": str(instance.instance_id),
                "offline_term_ids": offline_ids,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
