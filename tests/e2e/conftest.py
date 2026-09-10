from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pexpect
import pytest
from sqlalchemy import text
from termflow_control_plane.persistence.database import Database
from termflow_control_plane.persistence.repositories import RepositoryBundle, digest_secret
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
        configured_node = os.environ.get("TERMFLOW_NODE_EXECUTABLE")
        self.node_executable = (
            Path(configured_node).resolve()
            if configured_node
            else self.repo / ".venv/bin/termflow"
        )
        if not self.node_executable.is_file() or not os.access(
            self.node_executable, os.X_OK
        ):
            raise RuntimeError(
                f"Invalid TERMFLOW_NODE_EXECUTABLE: {self.node_executable}"
            )
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.admin_token = "e2e-admin-token-that-is-long-enough"
        self._totp_master_key = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        self.database_path = root / "control-plane.db"
        self.control_log_path = root / "control-plane.log"
        self.control_process: subprocess.Popen[bytes] | None = None
        self.control_environment: dict[str, str] = {}
        self.control_start_count = 0
        self.control_process_ids: list[int] = []
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

    @property
    def control_process_id(self) -> int | None:
        process = self.control_process
        return process.pid if process is not None and process.poll() is None else None

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
        environment.update(self.control_environment)
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
        self.control_start_count += 1
        self.control_process_ids.append(self.control_process.pid)
        log.close()
        deadline = time.monotonic() + 30
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
                str(self.node_executable),
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
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert enrollment_token not in result.stdout + result.stderr

    def new_and_detach(self, name: str) -> LocalInstance:
        before = {instance.instance_id for instance in self.instance_store.list().instances}
        child = pexpect.spawn(
            str(self.node_executable),
            ["new", "--name", name],
            env=self.node_env,
            timeout=5,
            encoding=None,
        )
        self.children.append(child)
        deadline = time.monotonic() + 30
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
        deadline = time.monotonic() + 30
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
            [str(self.node_executable), *arguments],
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


class FakeAgentRuntime:
    """Tiny, mutable OpenCode HTTP contract used only by deterministic E2E."""

    def __init__(self) -> None:
        self.healthy = True
        self.mcp_connected = True
        self.delete_available = True
        self.session_create_calls = 0
        self.prompt_calls = 0
        self.delete_calls = 0
        self.health_calls = 0
        self.mcp_calls = 0
        self.sse_connections = 0
        self.reconcile_calls = 0
        self.reconcile_available = True
        self._sessions: dict[str, list[dict[str, object]]] = {}
        self._events: list[str] = []
        self._event_counter = 0
        self._next_prompt_mode = "completed"
        self._disconnect_after_events: int | None = None
        self._forced_disconnects_remaining = 0
        self._started = False
        self._stopping = False
        self._condition = threading.Condition()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_type())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="termflow-fake-agent-runtime",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            self._server.server_close()
            return
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def set_health(self, *, healthy: bool, mcp_connected: bool | None = None) -> None:
        self.healthy = healthy
        if mcp_connected is not None:
            self.mcp_connected = mcp_connected

    def disconnect_next_sse_after(self, *, event_count: int) -> None:
        if event_count < 1:
            raise ValueError("event_count must be positive")
        with self._condition:
            self._disconnect_after_events = event_count
            self._forced_disconnects_remaining = 1

    def emit_tool_start_only_on_next_prompt(self) -> None:
        with self._condition:
            self._next_prompt_mode = "tool_start_only"

    def set_reconcile_available(self, available: bool) -> None:
        self.reconcile_available = available

    def _next_event(
        self,
        event_type: str,
        properties: dict[str, object],
    ) -> dict[str, object]:
        self._event_counter += 1
        return {
            "directory": "/workspace",
            "payload": {
                "id": f"evt_fixture_{self._event_counter}",
                "type": event_type,
                "properties": properties,
            },
        }

    def _emit_completed_turn(self, session_id: str) -> None:
        message_id = f"msg_fixture_{self.prompt_calls}"
        text = "fixture-result-1"
        self._sessions.setdefault(session_id, []).append(
            {"id": message_id, "role": "assistant"}
        )
        events = (
            self._next_event(
                "session.status",
                {"sessionID": session_id, "status": {"type": "busy"}},
            ),
            self._next_event(
                "message.part.updated",
                {
                    "sessionID": session_id,
                    "part": {
                        "id": f"prt_fixture_{self.prompt_calls}",
                        "messageID": message_id,
                        "sessionID": session_id,
                        "type": "text",
                        "text": text,
                    },
                },
            ),
            self._next_event(
                "message.updated",
                {
                    "sessionID": session_id,
                    "info": {
                        "id": message_id,
                        "role": "assistant",
                        "time": {"created": 1, "completed": 2},
                    },
                },
            ),
            self._next_event("session.idle", {"sessionID": session_id}),
        )
        with self._condition:
            self._events.extend(
                json.dumps(event, separators=(",", ":")) for event in events
            )
            self._condition.notify_all()

    def _emit_tool_start_only(self, session_id: str) -> None:
        message_id = f"msg_fixture_{self.prompt_calls}"
        events = (
            self._next_event(
                "session.status",
                {"sessionID": session_id, "status": {"type": "busy"}},
            ),
            self._next_event(
                "message.part.updated",
                {
                    "sessionID": session_id,
                    "part": {
                        "id": f"prt_tool_fixture_{self.prompt_calls}",
                        "messageID": message_id,
                        "sessionID": session_id,
                        "type": "tool",
                        "callID": f"call_fixture_{self.prompt_calls}",
                        "tool": "termflow_terminal_input",
                        "state": {"status": "running"},
                    },
                },
            ),
        )
        with self._condition:
            self._events.extend(
                json.dumps(event, separators=(",", ":")) for event in events
            )
            self._condition.notify_all()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *args: object) -> None:
                del args

            def _json(self, status: int, payload: object) -> None:
                encoded = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length", "0"))
                return self.rfile.read(length) if length else b""

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/global/health":
                    runtime.health_calls += 1
                    self._json(200, {"healthy": runtime.healthy, "version": "fixture"})
                    return
                if path == "/mcp":
                    runtime.mcp_calls += 1
                    status = "connected" if runtime.mcp_connected else "failed"
                    self._json(200, {"termflow": {"status": status}})
                    return
                if path == "/global/event":
                    self._stream_events()
                    return
                if path.startswith("/session/") and path.endswith("/message"):
                    runtime.reconcile_calls += 1
                    session_id = path.split("/")[2]
                    if not runtime.reconcile_available:
                        self._json(503, {"error": "fixture_reconcile_unavailable"})
                        return
                    if session_id not in runtime._sessions:
                        self._json(404, {"error": "not_found"})
                    else:
                        self._json(200, runtime._sessions[session_id])
                    return
                self._json(404, {"error": "not_found"})

            def _stream_events(self) -> None:
                runtime.sse_connections += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                index = 0
                sent_on_connection = 0
                try:
                    while True:
                        with runtime._condition:
                            while (
                                index >= len(runtime._events)
                                and not runtime._stopping
                            ):
                                runtime._condition.wait(timeout=0.2)
                            if runtime._stopping:
                                return
                            pending = runtime._events[index:]
                            index = len(runtime._events)
                        for event in pending:
                            self.wfile.write(f"data: {event}\n\n".encode())
                            self.wfile.flush()
                            sent_on_connection += 1
                            with runtime._condition:
                                force_disconnect = (
                                    runtime._forced_disconnects_remaining > 0
                                    and runtime._disconnect_after_events is not None
                                    and sent_on_connection
                                    >= runtime._disconnect_after_events
                                )
                                if force_disconnect:
                                    runtime._forced_disconnects_remaining -= 1
                                    runtime._disconnect_after_events = None
                            if force_disconnect:
                                self.close_connection = True
                                return
                except (BrokenPipeError, ConnectionResetError):
                    return

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                self._read_body()
                if path == "/session":
                    runtime.session_create_calls += 1
                    session_id = f"ses_fixture_{runtime.session_create_calls}"
                    runtime._sessions[session_id] = []
                    self._json(200, {"id": session_id})
                    return
                if path.startswith("/session/") and path.endswith("/prompt_async"):
                    runtime.prompt_calls += 1
                    session_id = path.split("/")[2]
                    if session_id not in runtime._sessions:
                        self._json(404, {"error": "not_found"})
                        return
                    self._empty(204)
                    with runtime._condition:
                        prompt_mode = runtime._next_prompt_mode
                        runtime._next_prompt_mode = "completed"
                    if prompt_mode == "tool_start_only":
                        runtime._emit_tool_start_only(session_id)
                    else:
                        runtime._emit_completed_turn(session_id)
                    return
                if path.startswith("/session/") and (
                    path.endswith("/abort") or "/permissions/" in path
                ):
                    self._json(200, {"ok": True})
                    return
                self._json(404, {"error": "not_found"})

            def do_DELETE(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path.startswith("/session/"):
                    runtime.delete_calls += 1
                    session_id = path.split("/")[2]
                    if not runtime.delete_available:
                        self._json(503, {"error": "fixture_unavailable"})
                        return
                    runtime._sessions.pop(session_id, None)
                    self._empty(204)
                    return
                self._json(404, {"error": "not_found"})

        return Handler


@dataclass(slots=True)
class AgentProductSystem:
    system: TermFlowSystem
    runtime: FakeAgentRuntime
    mcp_token: str = field(repr=False)
    cleanup_helper_token: str = field(repr=False)


@dataclass(slots=True)
class AgentMigrationFailureSystem:
    system: TermFlowSystem
    inbox_id: UUID


async def _prepare_agent_migration_failure(database_path: Path) -> UUID:
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    try:
        initialized = await database.initialize()
        assert initialized.agent_ready
        repositories = RepositoryBundle(database.session_factory)
        installation = await repositories.installations.create(
            digest_secret("migration-failure-installation")
        )
        term = await repositories.instances.register_or_rotate(
            uuid4(),
            installation.id,
            "migration-failure-term",
            digest_secret("migration-failure-term-token"),
        )
        profile = await repositories.agent_profiles.create(
            display_name="migration-failure-profile",
            backend_kind="opencode",
            config='{"model_id":"deepseek-v4-flash","provider_id":"deepseek"}',
        )
        binding = await repositories.agent_bindings.create(
            profile_id=profile.id,
            term_id=term.id,
        )
        conversation = await repositories.agent_conversations.create(
            binding_id=binding.id,
            title="must remain unclaimed",
        )
        inbox = await repositories.agent_inbox.enqueue(
            conversation_id=conversation.id,
            kind="user_message",
            actor_id="migration-fixture",
            actor_kind="user_session",
            idempotency_key="migration-failure-pending-inbox",
            payload_digest="d" * 64,
            source="user",
        )
        # The current revision remains 0012, but strict Agent-head validation
        # now fails on an extra Agent-only column.  Core 0005 validation does
        # not inspect this table, which exercises the real two-stage boundary.
        async with database.engine.begin() as connection:
            await connection.execute(
                text("ALTER TABLE agent_profiles ADD COLUMN injected_failure TEXT")
            )
        return inbox.id
    finally:
        await database.dispose()


@pytest.fixture
def agent_product_system(tmp_path: Path) -> Iterator[AgentProductSystem]:
    runtime = FakeAgentRuntime()
    system = TermFlowSystem(tmp_path)
    mcp_token = secrets.token_urlsafe(32)
    cleanup_helper_token = secrets.token_urlsafe(32)
    # This is synthetic catalog metadata for an isolated fake-provider test.
    # It is deliberately not evidence about DeepSeek's real retention/training
    # policy and is never reused by the stable/live deployment.
    system.control_environment.update(
        {
            "TERMFLOW_AGENT_OPENCODE_BASE_URL": runtime.base_url,
            "TERMFLOW_AGENT_OPENCODE_DIRECTORY": "/workspace",
            "TERMFLOW_AGENT_OPENCODE_MCP_TOKEN": mcp_token,
            "TERMFLOW_AGENT_CLEANUP_HELPER_TOKEN": cleanup_helper_token,
            "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_ENDPOINT_ORIGIN": (
                "https://api.deepseek.com"
            ),
            "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_MODEL_IDS": "deepseek-v4-flash",
            "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_REGION": "fixture-only",
            "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_TERMS": "fixture-only",
            "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_RETENTION_VERSION": "fixture-only-v1",
            "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_NO_TRAINING": "true",
            "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_CREDENTIAL_SOURCE": "DEEPSEEK_API_KEY",
            "TERMFLOW_AGENT_PROVIDER_DEEPSEEK_POLICY_VERSION": "fixture-only-v1",
            "TERMFLOW_AGENT_RUNTIME_HEALTH_TICK_SECONDS": "0.2",
            "TERMFLOW_AGENT_PIPELINE_RECONCILE_ATTEMPTS": "2",
        }
    )
    try:
        system.start_control_plane()
        yield AgentProductSystem(
            system=system,
            runtime=runtime,
            mcp_token=mcp_token,
            cleanup_helper_token=cleanup_helper_token,
        )
    finally:
        system.cleanup()
        runtime.stop()


@pytest.fixture
def agent_migration_failure_system(
    tmp_path: Path,
) -> Iterator[AgentMigrationFailureSystem]:
    system = TermFlowSystem(tmp_path)
    inbox_id = asyncio.run(_prepare_agent_migration_failure(system.database_path))
    try:
        system.start_control_plane()
        yield AgentMigrationFailureSystem(system=system, inbox_id=inbox_id)
    finally:
        system.cleanup()


@pytest.fixture
def termflow_system(tmp_path) -> TermFlowSystem:
    system = TermFlowSystem(tmp_path)
    system.start_control_plane()
    try:
        yield system
    finally:
        system.cleanup()
