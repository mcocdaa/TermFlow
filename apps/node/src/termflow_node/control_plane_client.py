"""Narrow bootstrap HTTP client for B."""

from __future__ import annotations

import platform
import socket
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr
from termflow_protocol import (
    InstallationEnrollResponse,
    InstanceListResponse,
    InstanceRegisterResponse,
)

from termflow_node import __version__
from termflow_node.config.models import InstallationConfig
from termflow_node.instances.models import LocalInstance
from termflow_node.instances.store import InstanceStore


class InsecureServerUrl(ValueError):
    pass


def validate_server_url(server_url: str) -> str:
    parsed = urlsplit(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InsecureServerUrl("Server URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InsecureServerUrl("Server URL cannot contain credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise InsecureServerUrl("Public TermFlow servers require HTTPS")
    return server_url.rstrip("/")


class ControlPlaneClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def enroll(
        self,
        server_url: str,
        enrollment_token: str,
    ) -> InstallationEnrollResponse:
        base_url = validate_server_url(server_url)
        async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/api/v1/installations/enroll",
                json={
                    "enrollment_token": enrollment_token,
                    "hostname": socket.gethostname(),
                    "platform": platform.system(),
                    "client_version": __version__,
                },
            )
            response.raise_for_status()
            return InstallationEnrollResponse.model_validate(response.json())

    async def register_instance(
        self,
        installation: InstallationConfig,
        instance: LocalInstance,
        store: InstanceStore,
    ) -> LocalInstance:
        base_url = validate_server_url(str(installation.server_url))
        async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/api/v1/instances/register",
                headers={
                    "Authorization": (
                        "Bearer " + installation.installation_token.get_secret_value()
                    )
                },
                json={"instance_id": str(instance.instance_id), "name": instance.name},
            )
            response.raise_for_status()
            registration = InstanceRegisterResponse.model_validate(response.json())
        if registration.instance_id != instance.instance_id:
            raise ValueError("Control Plane returned a different Instance ID")
        updated = instance.model_copy(
            update={"instance_token": SecretStr(registration.instance_token)}
        )
        store.save(updated)
        return updated

    async def list_owned_instances(
        self,
        installation: InstallationConfig,
    ) -> InstanceListResponse:
        base_url = validate_server_url(str(installation.server_url))
        async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
            response = await client.get(
                f"{base_url}/api/v1/instances/mine",
                headers={
                    "Authorization": (
                        "Bearer " + installation.installation_token.get_secret_value()
                    )
                },
            )
            response.raise_for_status()
            return InstanceListResponse.model_validate(response.json())

    async def probe_health(self, server_url: str) -> tuple[bool, str]:
        base_url = validate_server_url(server_url)
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=3.0) as client:
                response = await client.get(f"{base_url}/healthz")
        except httpx.HTTPError as exc:
            return False, str(exc) or type(exc).__name__
        if response.is_success:
            return True, "reachable"
        return False, f"HTTP {response.status_code}"
