"""Server-owned provider catalog and canonical Agent Profile configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import ValidationError

from termflow_control_plane.agent_contracts import AgentProfileConfig
from termflow_control_plane.errors import TermFlowError

if TYPE_CHECKING:
    from termflow_control_plane.config import Settings


_INVALID_PROFILE_CONFIG = "The Agent Profile config is invalid."
_UNKNOWN_PROVIDER = "The Agent Profile provider or model is unavailable."
ALLOWED_PROVIDER_CREDENTIAL_SOURCES = frozenset(
    {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"}
)


def _validate_identifier(value: object, *, field_name: str, max_length: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
    ):
        raise ValueError(f"{field_name} must be a normalized non-empty string")


def _validate_disclosure_text(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a normalized non-empty string")


def _validate_endpoint_origin(value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("endpoint_origin must be a canonical absolute HTTP(S) origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(
            "endpoint_origin must be a canonical absolute HTTP(S) origin"
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint_origin must be a canonical absolute HTTP(S) origin")
    hostname = parsed.hostname
    canonical_host = f"[{hostname}]" if ":" in hostname else hostname
    canonical = f"{parsed.scheme}://{canonical_host}"
    if port is not None:
        canonical = f"{canonical}:{port}"
    if value != canonical:
        raise ValueError("endpoint_origin must be a canonical absolute HTTP(S) origin")


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    """Deployment-owned provider routing and disclosure metadata.

    ``credential_source`` is only the deployment source name.  Provider
    credentials are deliberately absent from this type.
    """

    provider_id: str
    model_ids: frozenset[str]
    endpoint_origin: str
    region: str
    retention_terms: str
    retention_version: str
    no_training: bool
    credential_source: str
    policy_version: str


def canonicalize_profile_config(value: object) -> tuple[AgentProfileConfig, str]:
    """Validate object/JSON input and return its stable persisted encoding."""

    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        parsed = AgentProfileConfig.model_validate(decoded, strict=True)
    except (json.JSONDecodeError, TypeError, ValidationError):
        raise TermFlowError(
            "invalid_profile_config",
            422,
            _INVALID_PROFILE_CONFIG,
        ) from None
    encoded = json.dumps(
        parsed.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return parsed, encoded


class ProviderCatalog:
    """Resolve Profile selections against deployment-owned provider policy."""

    def __init__(self, entries: Iterable[ProviderCatalogEntry]) -> None:
        materialized = tuple(entries)
        validated: dict[str, ProviderCatalogEntry] = {}
        for entry in materialized:
            if not isinstance(entry, ProviderCatalogEntry):
                raise ValueError("entry must be a ProviderCatalogEntry")
            _validate_identifier(
                entry.provider_id,
                field_name="provider_id",
                max_length=64,
            )
            if not isinstance(entry.model_ids, frozenset) or not entry.model_ids:
                raise ValueError("model_ids must be a non-empty frozenset")
            for model_id in entry.model_ids:
                _validate_identifier(
                    model_id,
                    field_name="model_ids",
                    max_length=128,
                )
            _validate_endpoint_origin(entry.endpoint_origin)
            for field_name in (
                "region",
                "retention_terms",
                "retention_version",
                "credential_source",
                "policy_version",
            ):
                _validate_disclosure_text(
                    getattr(entry, field_name),
                    field_name=field_name,
                )
            if entry.no_training is not True:
                raise ValueError("no_training must be true")
            if entry.credential_source not in ALLOWED_PROVIDER_CREDENTIAL_SOURCES:
                raise ValueError("credential_source is not allowed")
            if entry.provider_id in validated:
                raise ValueError("duplicate provider_id")
            validated[entry.provider_id] = entry
        self._entries = validated

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderCatalog:
        """Build the reference DeepSeek entry only with complete verified policy."""

        normalized_model_ids: list[str] = []
        for model_id in settings.agent_provider_deepseek_model_ids:
            normalized = model_id.strip()
            if not normalized or len(normalized) > 128:
                return cls(())
            normalized_model_ids.append(normalized)
        model_ids = frozenset(normalized_model_ids)
        region = (settings.agent_provider_deepseek_region or "").strip()
        retention_terms = (
            settings.agent_provider_deepseek_retention_terms or ""
        ).strip()
        retention_version = (
            settings.agent_provider_deepseek_retention_version or ""
        ).strip()
        credential_source = (
            settings.agent_provider_deepseek_credential_source or ""
        ).strip()
        policy_version = (
            settings.agent_provider_deepseek_policy_version or ""
        ).strip()
        if (
            not model_ids
            or not all(model_ids)
            or settings.agent_provider_deepseek_no_training is not True
            or not settings.agent_provider_deepseek_endpoint_origin
            or not region
            or not retention_terms
            or not retention_version
            or not policy_version
            or credential_source not in ALLOWED_PROVIDER_CREDENTIAL_SOURCES
        ):
            return cls(())
        return cls(
            (
                ProviderCatalogEntry(
                    provider_id="deepseek",
                    model_ids=model_ids,
                    endpoint_origin=settings.agent_provider_deepseek_endpoint_origin,
                    region=region,
                    retention_terms=retention_terms,
                    retention_version=retention_version,
                    no_training=True,
                    credential_source=credential_source,
                    policy_version=policy_version,
                ),
            )
        )

    def resolve(self, config: AgentProfileConfig) -> ProviderCatalogEntry:
        entry = self._entries.get(config.provider_id)
        if entry is None or config.model_id not in entry.model_ids:
            raise TermFlowError("unknown_provider", 422, _UNKNOWN_PROVIDER)
        return entry

    def default_config(self) -> AgentProfileConfig | None:
        """Select the same deterministic deployment preset for preview and setup."""
        if len(self._entries) != 1:
            return None
        entry = next(iter(self._entries.values()))
        return AgentProfileConfig(
            provider_id=entry.provider_id, model_id=sorted(entry.model_ids)[0]
        )

    def disclosure_fingerprint(self, config: AgentProfileConfig) -> str:
        entry = self.resolve(config)
        disclosure = {
            "credential_source": entry.credential_source,
            "endpoint_origin": entry.endpoint_origin,
            "model_id": config.model_id,
            "model_ids": sorted(entry.model_ids),
            "no_training": entry.no_training,
            "policy_version": entry.policy_version,
            "provider_id": entry.provider_id,
            "region": entry.region,
            "retention_terms": entry.retention_terms,
            "retention_version": entry.retention_version,
        }
        encoded = json.dumps(disclosure, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
