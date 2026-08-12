from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from scripts.release.verify_android_apk import (
    AndroidPackageMetadata,
    parse_badging,
    parse_signers,
    verify_launcher_resources,
)

DENSITIES = ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi")
LAUNCHER_NAMES = (
    "ic_launcher.png",
    "ic_launcher_round.png",
    "ic_launcher_foreground.png",
)
KNOWN_TEMPLATE_HASH = "dae1ff05b101efea50e4b622fe6a3af8ba8f761162fa7c4fd864adc7cb39eeac"


def _write_resources(
    root: Path,
    *,
    template_at_xxxhdpi: bool = False,
    adaptive_background: bool = True,
) -> tuple[Path, Path]:
    generated = root / "generated-res"
    apk = root / "app.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        for density in DENSITIES:
            for name in LAUNCHER_NAMES:
                content = f"termflow:{density}:{name}".encode()
                if template_at_xxxhdpi and density == "xxxhdpi" and name == "ic_launcher.png":
                    content = b"known-template"
                source = generated / f"mipmap-{density}" / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(content)
                archive.writestr(f"res/mipmap-{density}-v4/{name}", content)
        adaptive = generated / "mipmap-anydpi-v26" / "ic_launcher.xml"
        adaptive.parent.mkdir(parents=True, exist_ok=True)
        background = (
            '<background android:drawable="@color/ic_launcher_background"/>'
            if adaptive_background
            else ""
        )
        adaptive.write_text(
            f"<adaptive-icon>{background}"
            '<foreground android:drawable="@mipmap/ic_launcher_foreground"/>'
            "</adaptive-icon>"
        )
        archive.writestr("res/mipmap-anydpi-v26/ic_launcher.xml", adaptive.read_bytes())
    return apk, generated


def test_accepts_complete_termflow_launcher_resources(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path)

    verify_launcher_resources(apk, generated)


def test_rejects_missing_launcher_density(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path)
    (generated / "mipmap-xhdpi" / "ic_launcher_round.png").unlink()

    with pytest.raises(ValueError, match="missing generated launcher"):
        verify_launcher_resources(apk, generated)


def test_rejects_adaptive_launcher_without_background(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path, adaptive_background=False)

    with pytest.raises(ValueError, match="background resource"):
        verify_launcher_resources(apk, generated)


def test_rejects_known_template_launcher_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apk, generated = _write_resources(tmp_path, template_at_xxxhdpi=True)
    monkeypatch.setattr(
        "scripts.release.verify_android_apk.KNOWN_TEMPLATE_LAUNCHER_SHA256",
        frozenset({hashlib.sha256(b"known-template").hexdigest()}),
    )

    with pytest.raises(ValueError, match="template launcher"):
        verify_launcher_resources(apk, generated)


def test_parses_package_and_signer_contract() -> None:
    badging = (
        "package: name='io.termflow.client' versionCode='10065' "
        "versionName='0.1.0-rc.5' compileSdkVersion='36'\n"
    )
    signer_output = "Signer #1 certificate SHA-256 digest: a1:b2:c3\n"

    assert parse_badging(badging) == AndroidPackageMetadata(
        package_name="io.termflow.client",
        version_name="0.1.0-rc.5",
        version_code=10065,
    )
    assert parse_signers(signer_output) == ("A1B2C3",)


def test_rejects_ambiguous_package_or_signer_output() -> None:
    package_line = "package: name='io.termflow.client' versionCode='10065' versionName='0.1.0-rc.5'"
    with pytest.raises(ValueError, match="exactly one package"):
        parse_badging(f"{package_line}\n{package_line}\n")
    with pytest.raises(ValueError, match="exactly one signer"):
        parse_signers(
            "Signer #1 certificate SHA-256 digest: aa\n"
            "Signer #2 certificate SHA-256 digest: bb\n"
        )


def test_known_template_hash_evidence_is_complete() -> None:
    assert len(KNOWN_TEMPLATE_HASH) == 64
