"""Atomic orchestration for one tmux Instance lifecycle."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from platformdirs import user_runtime_path

from termflow_node.tmux.runner import TmuxRunner

from .models import InstanceLifecycle, LocalInstance, RemoteAccessState
from .store import InstanceListResult, InstanceStore


class BridgeLauncher(Protocol):
    def __call__(self, instance: LocalInstance) -> int: ...


class BridgeStartError(RuntimeError):
    pass


class InstanceResolutionError(LookupError):
    pass


class AmbiguousInstance(InstanceResolutionError):
    pass


def launch_bridge(instance: LocalInstance, *, log_path: Path | None = None) -> int:
    active_log_path = log_path or (
        InstanceStore.default().instance_dir(instance.instance_id) / "bridge.log"
    )
    descriptor = os.open(active_log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab", closefd=True) as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "termflow_node",
                "_bridge",
                "--instance-id",
                str(instance.instance_id),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(0.1)
    if process.poll() is not None:
        raise BridgeStartError(
            f"Bridge process for Instance {instance.instance_id} exited during startup"
        )
    return process.pid


class InstanceManager:
    def __init__(
        self,
        store: InstanceStore,
        *,
        bridge_launcher: BridgeLauncher | None = None,
        runner_factory: Callable[[Path], TmuxRunner] | None = None,
    ) -> None:
        self._store = store
        self._bridge_launcher = bridge_launcher or self._launch_bridge
        self._runner_factory = runner_factory or TmuxRunner

    def _launch_bridge(self, instance: LocalInstance) -> int:
        return launch_bridge(
            instance,
            log_path=self._store.instance_dir(instance.instance_id) / "bridge.log",
        )

    @staticmethod
    def _prepare_socket_path(instance_id: UUID) -> Path:
        preferred_root = user_runtime_path("termflow")
        candidate = preferred_root / f"{instance_id.hex}.sock"
        if len(os.fsencode(candidate.absolute())) > 100:
            preferred_root = Path(tempfile.gettempdir()) / f"termflow-{os.getuid()}"
            candidate = preferred_root / f"{instance_id.hex}.sock"
        preferred_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = preferred_root.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise PermissionError(f"Insecure TermFlow runtime directory: {preferred_root}")
        preferred_root.chmod(0o700)
        return candidate.absolute()

    def create(self, name: str) -> tuple[LocalInstance, list[str]]:
        instance_id = uuid4()
        record = LocalInstance(
            instance_id=instance_id,
            name=name,
            session_name=name,
            socket_path=self._prepare_socket_path(instance_id),
            created_at=datetime.now(UTC),
            lifecycle=InstanceLifecycle.STARTING,
        )
        self._store.save(record)
        runner = self._runner_factory(record.socket_path)
        try:
            runner.create_session(name, name)
            identity = runner.session_identity(name)
            identified = record.model_copy(
                update={
                    "schema_version": 3,
                    "session_id": identity.session_id,
                    "session_name": identity.session_name,
                    "name": identity.session_name,
                }
            )
            self._store.save(identified)
            bridge_pid = self._bridge_launcher(identified)
            running = identified.model_copy(
                update={
                    "bridge_pid": bridge_pid,
                    "lifecycle": InstanceLifecycle.RUNNING,
                }
            )
            self._store.save(running)
            return running, runner.attach_argv(identity.session_id)
        except BaseException:
            runner.kill_server()
            if record.socket_path.exists():
                record.socket_path.unlink()
            self._store.remove_new(instance_id)
            raise

    def current(self, instance_id: UUID) -> LocalInstance:
        record = self._store.load(instance_id)
        runner = self._runner_factory(record.socket_path)
        target = record.session_id if record.schema_version in {2, 3} else None
        identity = runner.session_identity(target)
        resolved_name = identity.session_name
        if record.schema_version == 1 and identity.session_name == "main":
            resolved_name = record.name
            if identity.session_name != resolved_name:
                runner.rename_session(identity.session_id, resolved_name)
        current = record.model_copy(
            update={
                "schema_version": 3,
                "session_id": identity.session_id,
                "session_name": resolved_name,
                "name": resolved_name,
            }
        )
        if current != record:
            self._store.save(current)
        return current

    def _current_if_available(self, record: LocalInstance) -> LocalInstance:
        try:
            return self.current(record.instance_id)
        except (OSError, RuntimeError, ValueError):
            return record

    def list_current(self) -> InstanceListResult:
        listing = self._store.list()
        return InstanceListResult(
            instances=[self._current_if_available(record) for record in listing.instances],
            diagnostics=listing.diagnostics,
        )

    def resolve(self, identifier: str) -> LocalInstance:
        try:
            instance_id = UUID(identifier)
        except ValueError:
            matches = [
                current
                for instance in self.list_current().instances
                if (current := self._current_if_available(instance)).name == identifier
            ]
            if not matches:
                raise InstanceResolutionError(f"No Instance named {identifier!r}") from None
            if len(matches) > 1:
                candidates = ", ".join(str(instance.instance_id) for instance in matches)
                raise AmbiguousInstance(
                    f"Instance name {identifier!r} is ambiguous; candidates: {candidates}"
                ) from None
            return matches[0]
        return self.current(instance_id)

    def attach(self, identifier: str) -> tuple[LocalInstance, list[str]]:
        record = self.resolve(identifier)
        self.require_running_tmux(record)
        record = self.start_bridge(record)
        if record.session_id is None:
            raise InstanceResolutionError(
                f"Instance {record.instance_id} tmux server is not running"
            )
        runner = self._runner_factory(record.socket_path)
        return record, runner.attach_argv(record.session_id)

    def require_running_tmux(self, record: LocalInstance) -> None:
        target = record.session_id
        if target is None or not self._runner_factory(record.socket_path).is_alive(target):
            raise InstanceResolutionError(
                f"Instance {record.instance_id} tmux server is not running"
            )

    def stop_bridge(self, record: LocalInstance) -> LocalInstance:
        pid = record.bridge_pid
        if pid is not None and self._is_expected_bridge(pid, record.instance_id):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self._is_expected_bridge(
                pid, record.instance_id
            ):
                time.sleep(0.05)
            if self._is_expected_bridge(pid, record.instance_id):
                raise BridgeStartError(
                    f"Bridge process for Instance {record.instance_id} did not stop"
                )
        stopped = record.model_copy(update={"bridge_pid": None})
        self._store.save(stopped)
        return stopped

    def start_bridge(self, record: LocalInstance) -> LocalInstance:
        if record.remote_access is RemoteAccessState.ACTIVATION_REQUIRED:
            return record
        if self.bridge_is_alive(record):
            return record
        started = record.model_copy(update={"bridge_pid": self._bridge_launcher(record)})
        self._store.save(started)
        return started

    def kill(self, instance_id: UUID) -> LocalInstance:
        record = self.current(instance_id)
        record = self.stop_bridge(record)
        if record.session_id is None:
            raise InstanceResolutionError(f"Instance {instance_id} has no stable tmux identity")
        self._runner_factory(record.socket_path).kill_session(record.session_id)
        stopped = record.model_copy(
            update={"bridge_pid": None, "lifecycle": InstanceLifecycle.STOPPED}
        )
        self._store.save(stopped)
        return stopped

    @staticmethod
    def _is_expected_bridge(pid: int, instance_id: UUID) -> bool:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        command = result.stdout
        return result.returncode == 0 and "termflow" in command and str(instance_id) in command

    @classmethod
    def bridge_is_alive(cls, record: LocalInstance) -> bool:
        return record.bridge_pid is not None and cls._is_expected_bridge(
            record.bridge_pid,
            record.instance_id,
        )
