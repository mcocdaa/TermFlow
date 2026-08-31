"""Validated local Installation identity."""

from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, SecretStr


class InstallationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_url: AnyHttpUrl
    installation_id: UUID
    installation_token: SecretStr
    allow_insecure_http: bool = False
