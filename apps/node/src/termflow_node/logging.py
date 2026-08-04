"""Privacy-preserving structured logging for the TermFlow node."""

from __future__ import annotations

import json
import logging as _logging
import os
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from platformdirs import user_log_path

LOG_FILENAME = "termflow.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

_LOGGER = _logging.getLogger("termflow.node")
_LOGGER.propagate = False
_CONFIGURED_PATH: Path | None = None

# Only these fields are allowed into the local diagnostic file. In particular,
# terminal data and credentials are intentionally not accepted.
_SAFE_FIELDS = {
    "component",
    "event",
    "issuer",
    "request_id",
    "error_code",
    "operation",
    "instance_id",
    "platform",
    "status",
    "duration_ms",
    "server_host",
    "command",
}
_SECRET_NAME = re.compile(
    r"token|secret|password|cookie|authorization|credential|totp|pkce|jwk|private",
    re.I,
)


def log_path(log_dir: Path | None = None) -> Path:
    """Return the canonical node log file path."""

    root = Path(log_dir) if log_dir is not None else Path(user_log_path("termflow"))
    return root / LOG_FILENAME


def configure_logging(log_dir: Path | None = None) -> Path:
    """Configure JSONL logging and return its path.

    Setup is deliberately best-effort for CLI startup: a read-only home or a
    locked-down service account must not prevent the node from running.
    """

    global _CONFIGURED_PATH
    path = log_path(log_dir)
    if _CONFIGURED_PATH == path and any(
        getattr(h, "_termflow_handler", False) for h in _LOGGER.handlers
    ):
        return path

    for handler in list(_LOGGER.handlers):
        if getattr(handler, "_termflow_handler", False):
            _LOGGER.removeHandler(handler)
            handler.close()

    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name != "nt":
            path.parent.chmod(0o700)
        handler = RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler._termflow_handler = True  # type: ignore[attr-defined]
        if os.name != "nt":
            path.chmod(0o600)
        _LOGGER.addHandler(handler)
        _LOGGER.setLevel(_logging.INFO)
        _CONFIGURED_PATH = path
    except OSError:
        _CONFIGURED_PATH = None
    return path


def _safe_value(name: str, value: Any) -> str | int | float | bool | None:
    if name not in _SAFE_FIELDS or _SECRET_NAME.search(name):
        return None
    if value is None:
        return None
    if isinstance(value, bool | int | float):
        return value
    text = str(value)
    if name in {"issuer"}:
        parsed = urlsplit(text)
        if parsed.scheme and parsed.netloc:
            text = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return text[:256]


def log_event(event: str, *, level: int = _logging.INFO, **fields: Any) -> None:
    """Write one sanitized JSONL event; logging failures are non-fatal."""

    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "component": "node",
        "event": event[:128],
    }
    for name, value in fields.items():
        safe = _safe_value(name, value)
        if safe is not None:
            payload[name] = safe
    try:
        _LOGGER.log(level, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except (OSError, ValueError, TypeError):
        pass
