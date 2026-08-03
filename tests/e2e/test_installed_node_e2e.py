from __future__ import annotations

import os

import pytest


@pytest.mark.e2e
@pytest.mark.tmux
@pytest.mark.skipif(
    "TERMFLOW_NODE_EXECUTABLE" not in os.environ,
    reason="requires an installed TermFlow node executable",
)
def test_installed_node_creates_an_online_term(termflow_system) -> None:
    termflow_system.login(termflow_system.create_enrollment())
    instance = termflow_system.new_and_detach("installed-bundle")

    assert termflow_system.wait_until_online(instance.instance_id)
