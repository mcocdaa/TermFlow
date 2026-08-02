"""Resolve the TOTP encryption key without exposing key material."""

from __future__ import annotations

import base64
import os
import stat
import time
from pathlib import Path
from secrets import token_bytes

from pydantic import SecretStr

from termflow_control_plane.config import Settings, _decode_master_key

_KEY_BYTES = 32
_READ_RETRIES = 100
_READ_RETRY_SECONDS = 0.01


def resolve_totp_master_key(settings: Settings) -> bytes | None:
    """Prefer explicit operator input, otherwise create the single-node key once."""

    explicit = settings.totp_master_key_bytes
    if explicit is not None:
        return explicit
    if settings.totp_auto_master_key_file is None:
        return None
    return _read_or_create_key_file(settings.totp_auto_master_key_file)


def _read_or_create_key_file(path: Path) -> bytes:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = token_bytes(_KEY_BYTES)
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_existing_key_file(path)

    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("TOTP master key file write did not complete")
            offset += written
        if offset != len(encoded):
            raise OSError("TOTP master key file write did not complete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return raw


def _read_existing_key_file(path: Path) -> bytes:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("automatic TOTP master key file must be private")

    for attempt in range(_READ_RETRIES):
        try:
            encoded = path.read_text(encoding="ascii").strip()
            return _decode_master_key(SecretStr(encoded))
        except (OSError, UnicodeError, ValueError) as exc:
            if attempt + 1 == _READ_RETRIES:
                raise ValueError("automatic TOTP master key file is invalid") from exc
            time.sleep(_READ_RETRY_SECONDS)
    raise RuntimeError("unreachable TOTP master key read state")
