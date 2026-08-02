from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pexpect
import pytest
from termflow_node.instances.models import LocalInstance
from termflow_node.instances.store import InstanceStore
from websockets.sync.client import connect


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass(slots=True)
class EventCursor:
    connection: object

    def wait_for_output(self, expected: bytes, timeout: float = 5) -> tuple[dict, bytes] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            raw = self.connection.recv(timeout=remaining)
            message = json.loads(raw)
            if message["type"] != "pane.output":
                continue
            data = base64.b64decode(message["payload"]["data_base64"], validate=True)
            if expected in data:
                return message, data
        return None

    def wait_for_bytes(self, expected: bytes, timeout: float = 5) -> bool:
        return self.wait_for_output(expected, timeout) is not None


class TermFlowSystem:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = Path.cwd()
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.admin_token = "e2e-admin-token-that-is-long-enough"
        self._totp_master_key = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        self.database_path = root / "control-plane.db"
        self.control_log_path = root / "control-plane.log"
        self.control_process: subprocess.Popen[bytes] | None = None
        self.children: list[pexpect.spawn] = []
        self.instances: list[LocalInstance] = []
        self.node_env = os.environ.copy()
        self.node_env.update(
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            }
        )
        (root / "runtime").mkdir(mode=0o700)
        self.instance_store = InstanceStore(root / "state" / "termflow" / "instances")

    @property
    def admin_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}

    def start_control_plane(self) -> None:
        if self.control_process is not None and self.control_process.poll() is None:
            return
        environment = os.environ.copy()
        environment.update(
            {
                "TERMFLOW_ADMIN_TOKEN": self.admin_token,
                "TERMFLOW_DATABASE_URL": f"sqlite+aiosqlite:///{self.database_path}",
                "TERMFLOW_ALLOW_INSECURE_LOOPBACK": "true",
                "TERMFLOW_PUBLIC_BASE_URL": self.base_url,
                "TERMFLOW_TRUSTED_WEB_ORIGINS": self.base_url,
                "TERMFLOW_TOTP_MASTER_KEY": self._totp_master_key,
            }
        )
        log = self.control_log_path.open("ab")
        self.control_process = subprocess.Popen(
            [
                str(self.repo / ".venv/bin/termflow-control"),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.control_process.poll() is not None:
                raise RuntimeError(self.control_log_path.read_text(errors="replace"))
            try:
                response = httpx.get(f"{self.base_url}/healthz", timeout=0.2)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise TimeoutError("Control Plane did not become healthy")

    def stop_control_plane(self) -> None:
        process = self.control_process
        self.control_process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    def create_enrollment(self) -> str:
        response = httpx.post(
            f"{self.base_url}/api/v1/enrollment-tokens",
            headers=self.admin_headers,
            timeout=2,
        )
        response.raise_for_status()
        return str(response.json()["token"])

    def login(self, enrollment_token: str) -> None:
        result = subprocess.run(
            [
                str(self.repo / ".venv/bin/termflow"),
                "login",
                "--server",
                self.base_url,
                "--enrollment-token",
                enrollment_token,
            ],
            env=self.node_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert enrollment_token not in result.stdout + result.stderr

    def new_and_detach(self, name: str) -> LocalInstance:
        before = {instance.instance_id for instance in self.instance_store.list().instances}
        child = pexpect.spawn(
            str(self.repo / ".venv/bin/termflow"),
            ["new", "--name", name],
            env=self.node_env,
            timeout=5,
            encoding=None,
        )
        self.children.append(child)
        deadline = time.monotonic() + 5
        record: LocalInstance | None = None
        while time.monotonic() < deadline:
            current = [
                instance
                for instance in self.instance_store.list().instances
                if instance.instance_id not in before
            ]
            if current and current[0].lifecycle == "running":
                record = current[0]
                break
            if not child.isalive():
                remainder = child.read().decode(errors="replace")
                raise RuntimeError(remainder or f"termflow new exited with {child.exitstatus}")
            time.sleep(0.05)
        if record is None:
            raise TimeoutError("termflow new did not publish Instance metadata")
        child.send(b"\x02d")
        child.expect(pexpect.EOF, timeout=5)
        self.instances.append(record)
        return record

    def wait_until_online(self, instance_id: UUID, timeout: float = 10) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = httpx.get(
                f"{self.base_url}/api/v1/instances",
                headers=self.admin_headers,
                timeout=1,
            )
            if response.status_code == 200 and any(
                item["instance_id"] == str(instance_id) and item["online"]
                for item in response.json()["instances"]
            ):
                return True
            time.sleep(0.05)
        return False

    def topology(self, instance_id: UUID) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while True:
            response = httpx.get(
                f"{self.base_url}/api/v1/instances/{instance_id}/topology",
                headers=self.admin_headers,
                timeout=2,
            )
            if response.status_code != 409 or time.monotonic() >= deadline:
                response.raise_for_status()
                return response.json()["topology"]
            time.sleep(0.05)

    def first_pane_id(self, instance_id: UUID) -> str:
        topology = self.topology(instance_id)
        return str(topology["windows"][0]["panes"][0]["pane_id"])

    def subscribe(
        self,
        instance_id: UUID,
        *,
        pane_id: str | None = None,
        stream_id: str | None = None,
        after_seq: int | None = None,
    ) -> EventCursor:
        query = f"instance_id={instance_id}"
        if pane_id is not None and stream_id is not None and after_seq is not None:
            query += (
                f"&pane_id={pane_id.replace('%', '%25')}"
                f"&stream_id={stream_id}&after_seq={after_seq}"
            )
        websocket_url = f"ws://127.0.0.1:{self.port}/api/v1/events?{query}"
        connection = connect(
            websocket_url,
            additional_headers=self.admin_headers,
            ping_interval=None,
            open_timeout=3,
        )
        return EventCursor(connection)

    def run_node(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.repo / ".venv/bin/termflow"), *arguments],
            env=self.node_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def send_text(
        self,
        instance_id: UUID,
        pane_id: str,
        text: str,
        *,
        submit: bool,
    ) -> dict[str, object]:
        encoded_pane = pane_id.replace("%", "%25")
        response = httpx.post(
            f"{self.base_url}/api/v1/instances/{instance_id}/panes/{encoded_pane}/input",
            headers={**self.admin_headers, "Idempotency-Key": str(uuid4())},
            json={"text": text, "submit": submit},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def local_tmux_is_alive(instance: LocalInstance) -> bool:
        result = subprocess.run(
            [
                "tmux",
                "-S",
                str(instance.socket_path),
                "has-session",
                "-t",
                instance.session_name,
            ],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def cleanup(self) -> None:
        for child in self.children:
            if child.isalive():
                child.close(force=True)
        for instance in self.instances:
            if instance.bridge_pid is not None:
                try:
                    os.kill(instance.bridge_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            subprocess.run(
                ["tmux", "-S", str(instance.socket_path), "kill-server"],
                capture_output=True,
                check=False,
            )
        self.stop_control_plane()


@pytest.fixture
def termflow_system(tmp_path) -> TermFlowSystem:
    system = TermFlowSystem(tmp_path)
    system.start_control_plane()
    try:
        yield system
    finally:
        system.cleanup()
