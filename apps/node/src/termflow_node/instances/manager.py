"""Atomic orchestration for one tmux Instance lifecycle."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from platformdirs import user_runtime_path

from termflow_node.tmux.runner import TmuxRunner

from .models import InstanceLifecycle, LocalInstance
from .store import InstanceStore


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
    ) -> None:
        self._store = store
        self._bridge_launcher = bridge_launcher or self._launch_bridge

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
            socket_path=self._prepare_socket_path(instance_id),
            created_at=datetime.now(UTC),
            lifecycle=InstanceLifecycle.STARTING,
        )
        self._store.save(record)
        runner = TmuxRunner(record.socket_path)
        try:
            runner.create_session(record.session_name, name)
            bridge_pid = self._bridge_launcher(record)
            running = record.model_copy(
                update={
                    "bridge_pid": bridge_pid,
                    "lifecycle": InstanceLifecycle.RUNNING,
                }
            )
            self._store.save(running)
            return running, runner.attach_argv(running.session_name)
        except BaseException:
            runner.kill_server()
            if record.socket_path.exists():
                record.socket_path.unlink()
            self._store.remove_new(instance_id)
            raise

    def resolve(self, identifier: str) -> LocalInstance:
        try:
            instance_id = UUID(identifier)
        except ValueError:
            matches = [
                instance for instance in self._store.list().instances if instance.name == identifier
            ]
            if not matches:
                raise InstanceResolutionError(f"No Instance named {identifier!r}") from None
            if len(matches) > 1:
                candidates = ", ".join(str(instance.instance_id) for instance in matches)
                raise AmbiguousInstance(
                    f"Instance name {identifier!r} is ambiguous; candidates: {candidates}"
                ) from None
            return matches[0]
        return self._store.load(instance_id)

    def attach(self, identifier: str) -> tuple[LocalInstance, list[str]]:
        record = self.resolve(identifier)
        runner = TmuxRunner(record.socket_path)
        if not runner.is_alive(record.session_name):
            raise InstanceResolutionError(
                f"Instance {record.instance_id} tmux server is not running"
            )
        if record.bridge_pid is None or not self._is_expected_bridge(
            record.bridge_pid,
            record.instance_id,
        ):
            record = record.model_copy(update={"bridge_pid": self._bridge_launcher(record)})
            self._store.save(record)
        return record, runner.attach_argv(record.session_name)

    def kill(self, instance_id: UUID) -> LocalInstance:
        record = self._store.load(instance_id)
        if record.bridge_pid is not None and self._is_expected_bridge(
            record.bridge_pid,
            instance_id,
        ):
            os.kill(record.bridge_pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self._is_expected_bridge(
                record.bridge_pid,
                instance_id,
            ):
                time.sleep(0.05)
        TmuxRunner(record.socket_path).kill_server()
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
