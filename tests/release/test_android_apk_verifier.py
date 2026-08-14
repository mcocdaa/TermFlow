from __future__ import annotations

import hashlib
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.release.verify_android_apk import (
    AndroidPackageMetadata,
    parse_badging,
    parse_signers,
    read_signers,
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
    compiled_adaptive: bool = True,
    compiled_adaptive_as_drawables: bool = False,
    compiled_resource_table: bool = True,
    compiler_chosen_launcher_paths: bool = False,
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
                archive_name = (
                    f"res/drawable-nodpi-v4/{density}-{name}"
                    if compiler_chosen_launcher_paths
                    else f"res/mipmap-{density}-v4/{name}"
                )
                archive.writestr(archive_name, content)
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
        background_source = generated / "values" / "ic_launcher_background.xml"
        background_source.parent.mkdir(parents=True, exist_ok=True)
        background_source.write_text(
            '<resources><color name="ic_launcher_background">#ffffff</color></resources>'
        )
        if compiled_adaptive_as_drawables:
            archive.writestr(
                "res/drawable-v24/ic_launcher_foreground.xml",
                b"compiled adaptive foreground",
            )
            archive.writestr(
                "res/drawable/ic_launcher_background.xml",
                b"compiled adaptive background",
            )
        elif compiled_adaptive:
            archive.writestr(
                "res/mipmap-anydpi-v26/ic_launcher.xml", adaptive.read_bytes()
            )
        if compiled_resource_table:
            archive.writestr("resources.arsc", b"unrelated compiled resources")
    return apk, generated


def test_accepts_complete_termflow_launcher_resources(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path)

    verify_launcher_resources(apk, generated)


def test_accepts_launcher_images_at_compiler_chosen_paths(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path, compiler_chosen_launcher_paths=True)

    verify_launcher_resources(apk, generated)


def test_accepts_adaptive_launcher_compiled_as_drawable_resources(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path, compiled_adaptive_as_drawables=True)

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


def test_accepts_adaptive_launcher_compiled_only_in_resource_table(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path, compiled_adaptive=False)

    verify_launcher_resources(apk, generated)


def test_rejects_apk_without_compiled_resource_table(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path, compiled_resource_table=False)

    with pytest.raises(ValueError, match="compiled Android resource table"):
        verify_launcher_resources(apk, generated)


def test_rejects_missing_generated_background_resource(tmp_path: Path) -> None:
    apk, generated = _write_resources(tmp_path)
    (generated / "values" / "ic_launcher_background.xml").unlink()

    with pytest.raises(ValueError, match="missing generated launcher background"):
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


def test_deduplicates_repeated_signer_digest() -> None:
    signer_output = (
        "Signer #1 certificate SHA-256 digest: a1:b2:c3\n"
        "Signer #1 certificate SHA-256 digest: a1:b2:c3\n"
    )

    assert parse_signers(signer_output) == ("A1B2C3",)


def test_parses_indented_signer_digest() -> None:
    signer_output = "  Signer #1 certificate SHA-256 digest: a1:b2:c3\n"

    assert parse_signers(signer_output) == ("A1B2C3",)


def test_parses_v31_signer_digest() -> None:
    signer_output = (
        "Signer (minSdkVersion=33, maxSdkVersion=2147483647) "
        "certificate SHA-256 digest: a1:b2:c3\n"
    )

    assert parse_signers(signer_output) == ("A1B2C3",)


def test_reads_signer_certificate_written_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="Signer #1 certificate SHA-256 digest: a1:b2:c3\n",
        )

    monkeypatch.setattr(
        "scripts.release.verify_android_apk.find_android_tool",
        lambda _: "apksigner",
    )
    monkeypatch.setattr("scripts.release.verify_android_apk.subprocess.run", fake_run)

    assert read_signers(tmp_path / "app.apk") == ("A1B2C3",)


def test_rejects_ambiguous_package_or_signer_output() -> None:
    package_line = "package: name='io.termflow.client' versionCode='10065' versionName='0.1.0-rc.5'"
    with pytest.raises(ValueError, match="exactly one package"):
        parse_badging(f"{package_line}\n{package_line}\n")
    with pytest.raises(
        ValueError,
        match="found 2 unique certificate fingerprints: AA, BB",
    ):
        parse_signers(
            "Signer #1 certificate SHA-256 digest: aa\n"
            "Signer #2 certificate SHA-256 digest: bb\n"
        )


def test_reports_unparseable_signer_output() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "found 0 unique certificate fingerprints; signer-related output: "
            "Signer #1 certificate SHA-256 digest: not-a-digest"
        ),
    ):
        parse_signers("Signer #1 certificate SHA-256 digest: not-a-digest\n")


def test_known_template_hash_evidence_is_complete() -> None:
    assert len(KNOWN_TEMPLATE_HASH) == 64
