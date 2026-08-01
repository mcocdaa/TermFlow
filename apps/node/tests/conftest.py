import re
import shutil
import subprocess

import pytest


@pytest.fixture(autouse=True)
def require_supported_tmux(request) -> None:
    if request.node.get_closest_marker("tmux") is None:
        return
    executable = shutil.which("tmux")
    assert executable is not None, "tmux is required for tests marked tmux"
    result = subprocess.run(
        [executable, "-V"],
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.match(r"tmux (\d+)\.(\d+)", result.stdout)
    assert match is not None and (int(match.group(1)), int(match.group(2))) >= (3, 2)
