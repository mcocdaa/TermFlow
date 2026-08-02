"""Authenticated encryption for the few authentication secrets B must recover."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes
    key_version: int
    aad_version: int = 1

    def __repr__(self) -> str:
        return (
            "EncryptedSecret(ciphertext=<redacted>, nonce=<redacted>, "
            f"key_version={self.key_version}, aad_version={self.aad_version})"
        )


class AesGcmSecretBox:
    """AES-256-GCM with purpose-bound, versioned associated data."""

    _AAD_NAMESPACE = b"termflow-auth-secret"
    _AAD_VERSION = 1

    def __init__(self, key: bytes, *, key_version: int = 1) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        if key_version < 1:
            raise ValueError("key_version must be positive")
        self._cipher = AESGCM(key)
        self._key_version = key_version

    def __repr__(self) -> str:
        return f"AesGcmSecretBox(key=<redacted>, key_version={self._key_version})"

    @classmethod
    def _associated_data(cls, purpose: str, aad_version: int) -> bytes:
        if not purpose or ":" in purpose:
            raise ValueError("purpose must be a non-empty stable identifier")
        return b":".join(
            (
                cls._AAD_NAMESPACE,
                f"v{aad_version}".encode("ascii"),
                purpose.encode("ascii"),
            )
        )

    def encrypt(self, plaintext: bytes, *, purpose: str) -> EncryptedSecret:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            plaintext,
            self._associated_data(purpose, self._AAD_VERSION),
        )
        return EncryptedSecret(
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=self._key_version,
            aad_version=self._AAD_VERSION,
        )

    def decrypt(self, encrypted: EncryptedSecret, *, purpose: str) -> bytes:
        if encrypted.key_version != self._key_version:
            raise ValueError("encrypted value uses an unavailable key version")
        if len(encrypted.nonce) != 12:
            raise ValueError("encrypted value has an invalid nonce")
        try:
            return self._cipher.decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                self._associated_data(purpose, encrypted.aad_version),
            )
        except InvalidTag as exc:
            raise ValueError("encrypted value failed authentication") from exc
