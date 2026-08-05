"""Read-only local health probes with narrowly scoped permission repair."""

from __future__ import annotations

import asyncio
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.control_plane_client import (
    ControlPlaneClient,
    InsecureServerUrl,
    validate_server_url,
)
from termflow_node.instances.manager import InstanceManager, launch_bridge
from termflow_node.instances.models import LocalInstance, RemoteAccessState
from termflow_node.instances.store import InstanceStore
from termflow_node.tmux.runner import TmuxRunner


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str


def probe_instance_health(record: LocalInstance) -> tuple[bool, bool]:
    try:
        tmux_alive = record.socket_path.exists() and TmuxRunner(record.socket_path).is_alive(
            record.session_id or record.session_name
        )
    except (OSError, RuntimeError, ValueError):
        tmux_alive = False
    return tmux_alive, InstanceManager.bridge_is_alive(record)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def probe_control_plane_health(config: InstallationConfig) -> tuple[bool, str]:
    return asyncio.run(ControlPlaneClient().probe_health(str(config.server_url)))


def run_diagnostics(
    config_store: ConfigStore,
    instance_store: InstanceStore,
    *,
    repair: bool,
    check_control_plane: bool = False,
) -> list[DiagnosticCheck]:
    checks = [
        DiagnosticCheck(
            "python",
            sys.version_info >= (3, 12),
            f"Python {sys.version_info.major}.{sys.version_info.minor}",
        )
    ]
    try:
        TmuxRunner(Path("/tmp/termflow-doctor-version.sock"))
        checks.append(DiagnosticCheck("tmux", True, "tmux 3.2+ available"))
    except (OSError, RuntimeError, ValueError) as exc:
        checks.append(DiagnosticCheck("tmux", False, str(exc)))

    if not config_store.path.exists():
        checks.append(DiagnosticCheck("login", False, "Installation login is missing"))
    else:
        if repair:
            config_store.path.parent.chmod(0o700)
            config_store.path.chmod(0o600)
        permissions_ok = _mode(config_store.path) == 0o600
        checks.append(
            DiagnosticCheck(
                "config_permissions",
                permissions_ok,
                (
                    "0600"
                    if permissions_ok
                    else f"expected 0600, found {_mode(config_store.path):04o}"
                ),
            )
        )
        try:
            config = config_store.load()
            validate_server_url(str(config.server_url))
            checks.append(DiagnosticCheck("server_url", True, "TLS policy accepted"))
            if check_control_plane:
                reachable, detail = probe_control_plane_health(config)
                checks.append(DiagnosticCheck("control_plane", reachable, detail))
        except (OSError, ValueError, InsecureServerUrl) as exc:
            checks.append(DiagnosticCheck("server_url", False, str(exc)))

    listing = instance_store.list()
    for diagnostic in listing.diagnostics:
        checks.append(DiagnosticCheck("instance_metadata", False, str(diagnostic)))
    for record in listing.instances:
        tmux_alive, bridge_alive = probe_instance_health(record)
        if (
            repair
            and tmux_alive
            and not bridge_alive
            and record.remote_access is RemoteAccessState.ACTIVE
        ):
            try:
                pid = launch_bridge(
                    record,
                    log_path=instance_store.instance_dir(record.instance_id) / "bridge.log",
                )
                record = record.model_copy(update={"bridge_pid": pid})
                instance_store.save(record)
                bridge_alive = True
            except RuntimeError:
                bridge_alive = False
        checks.append(
            DiagnosticCheck(
                f"instance:{record.instance_id}",
                tmux_alive and bridge_alive,
                (
                    f"tmux={'up' if tmux_alive else 'down'} "
                    f"bridge={'up' if bridge_alive else 'down'} "
                    f"remote={record.remote_status.value.replace('_', '-')} "
                    f"remote_access={record.remote_access}"
                    + (
                        f" last_sync_error={record.last_sync_error}"
                        if record.last_sync_error is not None
                        else ""
                    )
                ),
            )
        )
    return checks
