"""Atomic, private storage for per-Instance metadata."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from platformdirs import user_state_path

from .models import LocalInstance


class InstanceNotFound(FileNotFoundError):
    pass


class InsecureInstanceMetadata(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class InstanceListResult:
    instances: list[LocalInstance]
    diagnostics: list[Path]


class InstanceStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def default(cls) -> InstanceStore:
        return cls(user_state_path("termflow") / "instances")

    def instance_dir(self, instance_id: UUID) -> Path:
        return self.root / str(instance_id)

    def metadata_path(self, instance_id: UUID) -> Path:
        return self.instance_dir(instance_id) / "metadata.json"

    def save(self, instance: LocalInstance) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        directory = self.instance_dir(instance.instance_id)
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        token = (
            instance.instance_token.get_secret_value()
            if instance.instance_token is not None
            else None
        )
        serialized_version = 3 if instance.session_id is not None else instance.schema_version
        payload = json.dumps(
            {
                "schema_version": serialized_version,
                "instance_id": str(instance.instance_id),
                "name": instance.name,
                "session_id": instance.session_id,
                "session_name": instance.session_name,
                "socket_path": str(instance.socket_path),
                "created_at": instance.created_at.isoformat(),
                "bridge_pid": instance.bridge_pid,
                "instance_token": token,
                "lifecycle": instance.lifecycle,
                "remote_access": instance.remote_access.value,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        path = self.metadata_path(instance.instance_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self, instance_id: UUID) -> LocalInstance:
        path = self.metadata_path(instance_id)
        try:
            metadata = path.stat()
        except FileNotFoundError as exc:
            raise InstanceNotFound(str(instance_id)) from exc
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise InsecureInstanceMetadata(str(path))
        return LocalInstance.model_validate_json(path.read_bytes())

    def list(self) -> InstanceListResult:
        if not self.root.is_dir():
            return InstanceListResult([], [])
        instances: list[LocalInstance] = []
        diagnostics: list[Path] = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            path = directory / "metadata.json"
            try:
                instances.append(self.load(UUID(directory.name)))
            except (ValueError, OSError):
                diagnostics.append(path)
        return InstanceListResult(instances, diagnostics)

    def remove_new(self, instance_id: UUID) -> None:
        """Remove only files created for one not-yet-published Instance."""

        directory = self.instance_dir(instance_id)
        path = self.metadata_path(instance_id)
        if path.exists():
            path.unlink()
        if directory.exists():
            directory.rmdir()
