"""Explicit remote-access activation transaction for one local Instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from termflow_node.config.models import InstallationConfig

from .manager import InstanceManager, InstanceResolutionError
from .models import LocalInstance, RemoteAccessState
from .store import InstanceStore


class ConfigLoader(Protocol):
    def load(self) -> InstallationConfig: ...


class ActivationManager(Protocol):
    def resolve(self, identifier: str) -> LocalInstance: ...

    def require_running_tmux(self, record: LocalInstance) -> None: ...

    def stop_bridge(self, record: LocalInstance) -> LocalInstance: ...

    def start_bridge(self, record: LocalInstance) -> LocalInstance: ...


class RegistrationClient(Protocol):
    async def register_instance(
        self,
        installation: InstallationConfig,
        instance: LocalInstance,
        store: InstanceStore,
    ) -> LocalInstance: ...


@dataclass(frozen=True, slots=True)
class ActivationResult:
    instance: LocalInstance
    activated: bool


class ActivationError(RuntimeError):
    pass


class InstanceActivator:
    def __init__(
        self,
        *,
        config_store: ConfigLoader,
        instance_store: InstanceStore,
        manager: ActivationManager,
        control_plane: RegistrationClient,
    ) -> None:
        self._config_store = config_store
        self._instance_store = instance_store
        self._manager = manager
        self._control_plane = control_plane

    async def activate(self, identifier: str) -> ActivationResult:
        try:
            record = self._manager.resolve(identifier)
        except InstanceResolutionError as exc:
            raise ActivationError(str(exc)) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise ActivationError(
                "Remote activation failed; local tmux was not changed."
            ) from exc

        if record.remote_access is RemoteAccessState.ACTIVE:
            return ActivationResult(record, False)

        try:
            installation = self._config_store.load()
            self._manager.require_running_tmux(record)
            required = self._manager.stop_bridge(record).model_copy(
                update={
                    "schema_version": 3,
                    "remote_access": RemoteAccessState.ACTIVATION_REQUIRED,
                    "instance_token": None,
                    "bridge_pid": None,
                }
            )
            self._instance_store.save(required)
            registered = await self._control_plane.register_instance(
                installation,
                required,
                self._instance_store,
            )
            active = registered.model_copy(
                update={
                    "schema_version": 3,
                    "remote_access": RemoteAccessState.ACTIVE,
                }
            )
            self._instance_store.save(active)
            try:
                started = self._manager.start_bridge(active)
            except (OSError, RuntimeError, ValueError) as exc:
                rollback = active.model_copy(
                    update={
                        "remote_access": RemoteAccessState.ACTIVATION_REQUIRED,
                        "instance_token": None,
                        "bridge_pid": None,
                    }
                )
                self._instance_store.save(rollback)
                raise ActivationError(
                    "Bridge failed to start after registration."
                ) from exc
            return ActivationResult(started, True)
        except ActivationError:
            raise
        except (
            httpx.HTTPError,
            InstanceResolutionError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            raise ActivationError(
                "Remote activation failed; local tmux was not changed."
            ) from exc


def default_instance_activator(store: InstanceStore) -> InstanceActivator:
    """Compose production dependencies without exposing them to the CLI parser."""

    from termflow_node.config.store import ConfigStore
    from termflow_node.control_plane_client import ControlPlaneClient

    return InstanceActivator(
        config_store=ConfigStore.default(),
        instance_store=store,
        manager=InstanceManager(store),
        control_plane=ControlPlaneClient(),
    )
