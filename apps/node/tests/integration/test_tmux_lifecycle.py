import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from termflow_node.instances.manager import InstanceManager
from termflow_node.instances.models import InstanceLifecycle, LocalInstance
from termflow_node.instances.store import InstanceStore
from termflow_node.tmux.runner import TmuxRunner

pytestmark = pytest.mark.tmux


def test_private_tmux_server_survives_without_attached_client(tmp_path) -> None:
    socket_path = (tmp_path / "private" / "instance.sock").absolute()
    socket_path.parent.mkdir(mode=0o700)
    runner = TmuxRunner(socket_path)
    runner.create_session("main", "termflow-test")
    try:
        assert runner.is_alive()
        assert runner.list_pane_ids() == ["%0"]
        assert socket_path.exists()
        assert os.stat(socket_path).st_uid == os.getuid()
    finally:
        runner.kill_server()
    assert not runner.is_alive()


def _prepare_socket_path(self: InstanceManager, instance_id: UUID) -> Path:
    return Path("/tmp") / f"tf-serve-{instance_id.hex}.sock"


def test_recover_rebuilds_tmux_keeping_the_same_identity(tmp_path, monkeypatch) -> None:
    store = InstanceStore(tmp_path / "instances")
    manager = InstanceManager(
        store,
        bridge_launcher=lambda instance: 1_000_000,
        runner_factory=TmuxRunner,
    )
    monkeypatch.setattr(InstanceManager, "_prepare_socket_path", _prepare_socket_path)
    created, _ = manager.create("alpha")
    runner = TmuxRunner(created.socket_path)
    runner.kill_server()

    recovered = manager.recover(created.instance_id)

    try:
        assert recovered.instance_id == created.instance_id
        assert recovered.instance_token == created.instance_token
        assert recovered.lifecycle is InstanceLifecycle.RUNNING
        assert TmuxRunner(recovered.socket_path).is_alive(recovered.session_id)
        assert len(store.list().instances) == 1
    finally:
        TmuxRunner(recovered.socket_path).kill_server()


def test_serve_runs_without_tty_and_stops_cleanly(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    (config_dir / "termflow").mkdir(parents=True)
    installation_id = uuid4()
    config_path = config_dir / "termflow" / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "server_url": "http://127.0.0.1:1",
                "installation_id": str(installation_id),
                "installation_token": "dummy-token",
                "allow_insecure_http": True,
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(config_dir),
        "XDG_STATE_HOME": str(state_dir),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "termflow_node", "serve", "--name", "demo"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    record: LocalInstance | None = None
    runner: TmuxRunner | None = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            listing = InstanceStore(state_dir / "termflow" / "instances").list()
            if listing.instances and listing.instances[0].bridge_pid is not None:
                record = listing.instances[0]
                break
            time.sleep(0.2)
        assert record is not None, "serve did not create a running Instance"
        runner = TmuxRunner(record.socket_path)
        assert runner.is_alive(record.session_id)
        assert runner.list_clients(record.session_id) == []
        assert InstanceManager.bridge_is_alive(record)

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0

        stored = InstanceStore(state_dir / "termflow" / "instances").load(record.instance_id)
        assert stored.lifecycle is InstanceLifecycle.STOPPED
        assert stored.bridge_pid is None
        assert not runner.is_alive(record.session_id)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if runner is not None:
            runner.kill_server()
