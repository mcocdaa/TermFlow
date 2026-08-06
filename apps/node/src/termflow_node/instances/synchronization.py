"""Explicit Control Plane synchronization for local Instance metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from termflow_protocol import InstanceListResponse

from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.control_plane_client import ControlPlaneClient
from termflow_node.diagnostics import probe_instance_health

from .models import LocalInstance, RemoteInstanceStatus
from .store import InstanceStore


class OwnedInstanceClient(Protocol):
    async def list_owned_instances(
        self,
        installation: InstallationConfig,
    ) -> InstanceListResponse: ...


@dataclass(frozen=True, slots=True)
class SyncResult:
    remote_deleted: list[UUID]
    updated: list[UUID]
    error: str | None = None

    def summary(self) -> str:
        if self.error is not None:
            return f"Sync failed: {self.error}"
        return f"Synced {len(self.updated)} instances; {len(self.remote_deleted)} removed remotely"


@dataclass(frozen=True, slots=True)
class PruneCandidate:
    instance: LocalInstance
    tmux_alive: bool
    bridge_alive: bool


class InstanceSynchronizer:
    def __init__(
        self,
        store: InstanceStore,
        control_plane: OwnedInstanceClient,
        installation: InstallationConfig,
        *,
        health_probe: Callable[[LocalInstance], tuple[bool, bool]] = probe_instance_health,
    ) -> None:
        self._store = store
        self._control_plane = control_plane
        self._installation = installation
        self._health_probe = health_probe

    @classmethod
    def from_defaults(cls) -> InstanceSynchronizer:
        return cls(
            InstanceStore.default(),
            ControlPlaneClient(),
            ConfigStore.default().load(),
        )

    async def sync(self) -> SyncResult:
        observed_at = datetime.now(UTC)
        records = self._store.list().instances
        try:
            response = await self._control_plane.list_owned_instances(self._installation)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            for record in records:
                self._store.save(
                    record.model_copy(
                        update={
                            "last_synced_at": observed_at,
                            "last_sync_error": message,
                        }
                    )
                )
            return SyncResult(remote_deleted=[], updated=[], error=message)

        remote = {item.instance_id: item for item in response.instances}
        remote_deleted: list[UUID] = []
        updated: list[UUID] = []
        for record in records:
            remote_record = remote.get(record.instance_id)
            if remote_record is None:
                status = RemoteInstanceStatus.REMOTE_DELETED
                remote_deleted.append(record.instance_id)
            elif remote_record.online:
                status = RemoteInstanceStatus.ONLINE
            else:
                status = RemoteInstanceStatus.OFFLINE
            self._store.save(
                record.model_copy(
                    update={
                        "remote_status": status,
                        "last_synced_at": observed_at,
                        "last_sync_error": None,
                    }
                )
            )
            updated.append(record.instance_id)
        return SyncResult(remote_deleted=remote_deleted, updated=updated)

    def prune_candidates(self) -> list[PruneCandidate]:
        candidates: list[PruneCandidate] = []
        for record in self._store.list().instances:
            if record.remote_status is not RemoteInstanceStatus.REMOTE_DELETED:
                continue
            tmux_alive, bridge_alive = self._health_probe(record)
            if not tmux_alive and not bridge_alive:
                candidates.append(
                    PruneCandidate(
                        instance=record,
                        tmux_alive=tmux_alive,
                        bridge_alive=bridge_alive,
                    )
                )
        return candidates

    def print_candidates(self, candidates: list[PruneCandidate]) -> None:
        for candidate in candidates:
            print(
                f"{candidate.instance.instance_id} {candidate.instance.name} "
                f"remote={candidate.instance.remote_status} "
                f"tmux={'up' if candidate.tmux_alive else 'down'} "
                f"bridge={'up' if candidate.bridge_alive else 'down'}"
            )

    def remove_candidates(self, candidates: list[PruneCandidate]) -> list[UUID]:
        removed: list[UUID] = []
        for candidate in candidates:
            self._store.remove(candidate.instance.instance_id)
            removed.append(candidate.instance.instance_id)
        return removed
