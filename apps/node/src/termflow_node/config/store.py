"""Atomic, owner-only persistence for Installation credentials."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from platformdirs import user_config_path

from .models import InstallationConfig


class ConfigNotFound(FileNotFoundError):
    pass


class InsecureConfigError(PermissionError):
    pass


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def default(cls) -> ConfigStore:
        return cls(user_config_path("termflow") / "config.json")

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, config: InstallationConfig) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        payload = json.dumps(
            {
                "server_url": str(config.server_url),
                "installation_id": str(config.installation_id),
                "installation_token": config.installation_token.get_secret_value(),
                "allow_insecure_http": config.allow_insecure_http,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self) -> InstallationConfig:
        try:
            metadata = self.path.stat()
        except FileNotFoundError as exc:
            raise ConfigNotFound(str(self.path)) from exc
        if metadata.st_uid != os.getuid():
            raise InsecureConfigError("Configuration is not owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise InsecureConfigError("Configuration permissions must not allow group or other")
        return InstallationConfig.model_validate_json(self.path.read_bytes())
