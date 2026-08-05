"""Local TermFlow command line."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

import typer

from termflow_node import __version__
from termflow_node.config.models import InstallationConfig
from termflow_node.config.store import ConfigStore
from termflow_node.control_plane_client import ControlPlaneClient, validate_server_url
from termflow_node.diagnostics import probe_instance_health, run_diagnostics
from termflow_node.instances.activation import ActivationError, default_instance_activator
from termflow_node.instances.manager import InstanceManager
from termflow_node.instances.models import LocalInstance, RemoteAccessState
from termflow_node.instances.store import InstanceStore
from termflow_node.instances.synchronization import InstanceSynchronizer
from termflow_node.logging import configure_logging, log_event

app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    help="Run and manage local TermFlow Instances.",
)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", is_eager=True, help="Show the TermFlow version."),
    ] = False,
) -> None:
    """Run and manage local TermFlow Instances."""
    configure_logging()
    log_event("cli_started", command="version" if version else "termflow")
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def login(
    server: str = typer.Option(..., "--server", help="Control Plane base URL."),
    enrollment_token: str = typer.Option(
        ...,
        "--code",
        "--enrollment-token",
        prompt=True,
        hide_input=True,
        help="Single-use Computer registration code.",
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing Installation login."),
) -> None:
    """Enroll this computer with one Control Plane."""

    store = ConfigStore.default()
    if store.exists() and not force:
        raise typer.BadParameter("A login already exists; pass --force to replace it.")
    normalized_server = validate_server_url(server)
    try:
        response = asyncio.run(ControlPlaneClient().enroll(normalized_server, enrollment_token))
    except Exception:
        log_event(
            "enrollment_failed",
            server_host=urlsplit(normalized_server).hostname or "",
            status="error",
        )
        raise
    config = InstallationConfig.model_validate(
        {
            "server_url": normalized_server,
            "installation_id": response.installation_id,
            "installation_token": response.installation_token,
        }
    )
    store.save(config)
    log_event(
        "enrollment_succeeded",
        server_host=config.server_url.host,
        instance_id=str(config.installation_id),
        status="ok",
    )
    typer.echo(f"Installation {config.installation_id} enrolled at {config.server_url.host}")


def _status_payload(record: LocalInstance) -> dict[str, object]:
    tmux_alive, bridge_alive = probe_instance_health(record)
    return {
        "instance_id": str(record.instance_id),
        "name": record.name,
        "lifecycle": record.lifecycle,
        "remote_access": record.remote_access,
        "remote_status": record.remote_status,
        "last_synced_at": (
            record.last_synced_at.isoformat() if record.last_synced_at is not None else None
        ),
        "last_sync_error": record.last_sync_error,
        "tmux_alive": tmux_alive,
        "bridge_alive": bridge_alive,
        "socket_path": str(record.socket_path),
    }


def _status_line(payload: dict[str, object]) -> str:
    if not payload["tmux_alive"]:
        health = "tmux-down"
    elif payload["remote_access"] == RemoteAccessState.ACTIVATION_REQUIRED:
        health = "activation-required"
    elif payload["bridge_alive"]:
        health = "bridge-running"
    else:
        health = "bridge-down"
    return (
        f"{payload['instance_id']} {payload['name']} "
        f"{payload['lifecycle']} {health} "
        f"remote={str(payload['remote_status']).replace('_', '-')} "
        f"remote_access={payload['remote_access']}"
    )


@app.command()
def new(name: Annotated[str, typer.Option("--name", help="Local Instance name.")]) -> None:
    """Create one isolated tmux Instance and attach to it."""

    ConfigStore.default().load()
    _, argv = InstanceManager(InstanceStore.default()).create(name)
    os.execvp(argv[0], argv)


@app.command()
def attach(identifier: str) -> None:
    """Attach to an exact UUID or uniquely named existing Instance."""

    _, argv = InstanceManager(InstanceStore.default()).attach(identifier)
    os.execvp(argv[0], argv)


@app.command()
def activate(identifier: str) -> None:
    """Explicitly restore remote access for one locally running Term."""

    activator = default_instance_activator(InstanceStore.default())
    try:
        result = asyncio.run(activator.activate(identifier))
    except ActivationError as exc:
        log_event("activation_failed", operation="activate", status="error")
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if result.activated:
        log_event(
            "activation_succeeded",
            operation="activate",
            instance_id=str(result.instance.instance_id),
        )
        typer.echo(f"Activated {result.instance.instance_id}")
    else:
        log_event(
            "activation_succeeded",
            operation="activate",
            instance_id=str(result.instance.instance_id),
            status="already_active",
        )
        typer.echo(f"Remote access already active for {result.instance.instance_id}")


@app.command()
def sync() -> None:
    """Synchronize local Instance metadata with the Control Plane."""

    result = asyncio.run(InstanceSynchronizer.from_defaults().sync())
    typer.echo(result.summary())
    if result.error is not None:
        raise typer.Exit(code=1)


@app.command()
def prune(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Remove only stale local metadata after explicit confirmation."""

    synchronizer = InstanceSynchronizer.from_defaults()
    candidates = synchronizer.prune_candidates()
    if dry_run:
        synchronizer.print_candidates(candidates)
        return
    if not candidates:
        typer.echo("No stale instances to remove")
        return
    if not force and not typer.confirm(f"清理 {len(candidates)} 个失效实例？"):
        raise typer.Abort()
    removed = synchronizer.remove_candidates(candidates)
    typer.echo(f"Removed {len(removed)} stale instances")


@app.command("list")
def list_instances(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """List local Instances without contacting the Control Plane."""

    store = InstanceStore.default()
    payloads = [
        _status_payload(record) for record in InstanceManager(store).list_current().instances
    ]
    if json_output:
        typer.echo(json.dumps(payloads, separators=(",", ":")))
        return
    for payload in payloads:
        typer.echo(_status_line(payload))


@app.command()
def status(
    identifier: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one local Instance status."""

    record = InstanceManager(InstanceStore.default()).resolve(identifier)
    payload = _status_payload(record)
    typer.echo(json.dumps(payload, separators=(",", ":")) if json_output else _status_line(payload))


@app.command()
def kill(identifier: str) -> None:
    """Stop only the selected Bridge and private tmux server."""

    manager = InstanceManager(InstanceStore.default())
    record = manager.resolve(identifier)
    stopped = manager.kill(record.instance_id)
    typer.echo(f"Stopped {stopped.instance_id}")


@app.command()
def doctor(
    repair: Annotated[
        bool,
        typer.Option("--repair", help="Repair known permissions/Bridge."),
    ] = False,
) -> None:
    """Inspect local requirements and per-Instance health."""

    checks = run_diagnostics(
        ConfigStore.default(),
        InstanceStore.default(),
        repair=repair,
        check_control_plane=True,
    )
    for check in checks:
        typer.echo(f"{'ok' if check.ok else 'error'} {check.name}: {check.detail}")
    if any(not check.ok for check in checks):
        raise typer.Exit(code=1)


async def _run_bridge(instance_id: UUID) -> None:
    from termflow_node.bridge.buffer import OutputBuffers
    from termflow_node.bridge.input_handler import AsyncTmuxInput, InputHandler
    from termflow_node.bridge.runtime import BridgeRuntime
    from termflow_node.bridge.terminal_manager import TerminalManager
    from termflow_node.bridge.transport import BridgeTransport
    from termflow_node.tmux.actions import TermRenamer, TmuxActionExecutor
    from termflow_node.tmux.bindings import TmuxBindingReader
    from termflow_node.tmux.client_size import TerminalSize
    from termflow_node.tmux.control_client import TmuxControlClient
    from termflow_node.tmux.runner import TmuxRunner
    from termflow_node.tmux.topology import TopologyReader

    log_event("bridge_started", instance_id=str(instance_id), status="starting")
    installation = ConfigStore.default().load()
    store = InstanceStore.default()
    instance = InstanceManager(store).current(instance_id)
    runner = TmuxRunner(instance.socket_path)
    if instance.session_id is None:
        raise RuntimeError("Instance has no stable tmux Session ID")
    topology = TopologyReader(runner, instance.session_id)
    control = TmuxControlClient(instance.socket_path, instance.session_id)
    buffers = OutputBuffers(max_bytes_per_pane=1024 * 1024)
    input_handler = InputHandler(
        topology_provider=topology.read,
        sender=AsyncTmuxInput(runner),
    )
    transport = BridgeTransport(
        installation=installation,
        instance=instance,
        store=store,
        control_plane=ControlPlaneClient(),
        topology_provider=topology.read,
    )
    initial_topology = topology.read()
    initial_panes = [pane for window in initial_topology.windows for pane in window.panes]
    creation_size = TerminalSize(
        rows=max((pane.top + pane.height for pane in initial_panes), default=23) + 1,
        cols=max((pane.left + pane.width for pane in initial_panes), default=80),
    )
    terminal_manager = TerminalManager(
        instance_id=instance_id,
        socket_path=instance.socket_path,
        session_id=instance.session_id,
        runner=runner,
        topology_provider=topology.read,
        publish=transport.enqueue_nowait,
        action_executor=TmuxActionExecutor(
            runner,
            instance.session_id,
            topology_provider=topology.read,
        ),
        binding_reader=TmuxBindingReader(runner, instance.session_id),
        renamer=TermRenamer(
            runner=runner,
            store=store,
            instance_id=instance_id,
            topology_provider=topology.read,
        ),
        creation_size=creation_size,
    )
    transport.set_connection_listener(terminal_manager)
    runtime = BridgeRuntime(
        instance_id=instance_id,
        control=control,
        topology_provider=topology.read,
        transport=transport,
        buffers=buffers,
        input_handler=input_handler,
        terminal_manager=terminal_manager,
    )
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, shutdown.set)
    try:
        await runtime.run(shutdown)
    finally:
        log_event("bridge_stopped", instance_id=str(instance_id), status="stopped")


@app.command("_bridge", hidden=True)
def bridge_process(instance_id: Annotated[UUID, typer.Option("--instance-id")]) -> None:
    """Run the private Bridge process for one explicit Instance."""

    asyncio.run(_run_bridge(instance_id))
