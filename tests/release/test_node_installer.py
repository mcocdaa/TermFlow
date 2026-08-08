from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
RENDERER = REPOSITORY_ROOT / "scripts/release/render_node_installer.py"


def _make_release_directory(root: Path, *, checksum: str | None = None) -> Path:
    release = root / "release"
    release.mkdir(parents=True)
    archive = release / "termflow-node-linux-x86_64.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        version = b"0.1.0\n"
        version_info = tarfile.TarInfo("termflow-node-linux-x86_64/VERSION")
        version_info.size = len(version)
        bundle.addfile(version_info, io.BytesIO(version))
        executable = b"#!/usr/bin/env bash\necho 0.1.0\n"
        executable_info = tarfile.TarInfo(
            "termflow-node-linux-x86_64/termflow/termflow"
        )
        executable_info.mode = 0o755
        executable_info.size = len(executable)
        bundle.addfile(executable_info, io.BytesIO(executable))
    digest = checksum or hashlib.sha256(archive.read_bytes()).hexdigest()
    (release / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
    return release


def _render_installer(
    tmp_path: Path,
    *,
    repository: str = "fork-owner/TermFlow",
) -> Path:
    installer = tmp_path / "install-termflow-node.sh"
    result = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "v0.1.0",
            str(installer),
            "--repository",
            repository,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return installer


def test_installer_defaults_to_the_rendered_repository(tmp_path: Path) -> None:
    installer = _render_installer(tmp_path, repository="fork-owner/TermFlow")

    assert (
        "https://github.com/fork-owner/TermFlow/releases/download/${TAG}"
        in installer.read_text()
    )
    assert "github.com/mcocdaa/TermFlow" not in installer.read_text()


def test_renderer_rejects_an_invalid_repository_slug(tmp_path: Path) -> None:
    installer = tmp_path / "install-termflow-node.sh"
    result = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "v0.1.0",
            str(installer),
            "--repository",
            "owner/repo/extra",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "owner/repository" in result.stderr
    assert not installer.exists()


def _fake_gh_directory(tmp_path: Path) -> Path:
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    shim = fake / "gh"
    shim.write_text("#!/usr/bin/env bash\nexit 0\n")
    shim.chmod(0o755)
    return fake


def _run_installer(
    release: Path,
    home: Path,
    installer: Path,
    *,
    extra_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "HOME": str(home),
        "TERMFLOW_RELEASE_BASE_URL": release.as_uri(),
    }
    if extra_path is not None:
        environment["PATH"] = f"{extra_path}:{environment['PATH']}"
    return subprocess.run(
        ["bash", str(installer)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_checks_checksum_and_updates_only_user_prefix(tmp_path: Path) -> None:
    release = _make_release_directory(tmp_path)
    home = tmp_path / "home"
    result = _run_installer(
        release,
        home,
        _render_installer(tmp_path),
        extra_path=_fake_gh_directory(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    installed = home / ".local/bin/termflow"
    assert installed.resolve().is_file()
    assert installed.resolve().read_text().startswith("#!/usr/bin/env bash")
    assert (home / ".local/opt/termflow-node/v0.1.0/VERSION").read_text() == "0.1.0\n"


def test_bad_checksum_preserves_existing_termflow(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old = home / ".local/opt/termflow-node/v0.0.9/termflow/termflow"
    old.parent.mkdir(parents=True)
    old.write_text("old termflow")
    old.chmod(0o755)
    bin_directory = home / ".local/bin"
    bin_directory.mkdir(parents=True)
    (bin_directory / "termflow").symlink_to(old)
    release = _make_release_directory(tmp_path / "bad", checksum="0" * 64)

    result = _run_installer(
        release,
        home,
        _render_installer(tmp_path),
        extra_path=_fake_gh_directory(tmp_path),
    )

    assert result.returncode != 0
    assert "checksum verification failed" in result.stderr
    assert (bin_directory / "termflow").resolve() == old
