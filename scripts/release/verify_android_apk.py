#!/usr/bin/env python3
"""Fail closed when an Android APK does not match the TermFlow release contract."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

DENSITIES = ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi")
LAUNCHER_NAMES = (
    "ic_launcher.png",
    "ic_launcher_round.png",
    "ic_launcher_foreground.png",
)
KNOWN_TEMPLATE_LAUNCHER_SHA256 = frozenset(
    {
        "75322a261ba38a23a25647af0d1298f204f3b3fafd317b8122a1b9a1f38284ff",
        "2425d59d27578f75ca97d31d9ae8385898badce3d6a1774bfc2f0fd191dc12c7",
        "320e552422179b81dae014ee6cc00561bd6e7455767b28f5518b8862a8c7987c",
        "7a9ae0632bfe5b28a1e6e9a7b38982fef62be07c95de46c26bd4f901ac6b9753",
        "44e5c3dc1dfb392f65e3dbcc9b986d30f10dd95b57e306657e56281b572fa684",
        "b1d19b8b78d0ed6903dd35b7640afba29b4cf02f3780e0d1cd46d9ebcbc93695",
        "0b250fc4451dfd1e5a41128234d93225726a2984448b0b966af25677b167d8de",
        "ab9397c9827aef4b3a1f1f917fc722d54abcf26488880c8bf9c724d1e59ab905",
        "dae1ff05b101efea50e4b622fe6a3af8ba8f761162fa7c4fd864adc7cb39eeac",
        "27cf0cdbc78bec8b9a14eaedb084c541a3c191fe5db89766e831fbfd21ce955d",
    }
)
_PACKAGE = re.compile(
    r"^package: name='(?P<name>[^']+)' versionCode='(?P<code>[0-9]+)' "
    r"versionName='(?P<version>[^']+)'(?:\s|$)"
)
_SIGNER = re.compile(r"^Signer #[0-9]+ certificate SHA-256 digest:\s*(?P<digest>[0-9A-Fa-f:]+)\s*$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True, slots=True)
class AndroidPackageMetadata:
    package_name: str
    version_name: str
    version_code: int


def parse_badging(output: str) -> AndroidPackageMetadata:
    matches = [match for line in output.splitlines() if (match := _PACKAGE.match(line))]
    if len(matches) != 1:
        raise ValueError("aapt output must contain exactly one package declaration")
    match = matches[0]
    return AndroidPackageMetadata(
        package_name=match.group("name"),
        version_name=match.group("version"),
        version_code=int(match.group("code")),
    )


def parse_signers(output: str) -> tuple[str, ...]:
    signers = tuple(
        match.group("digest").replace(":", "").upper()
        for line in output.splitlines()
        if (match := _SIGNER.match(line))
    )
    if len(signers) != 1:
        raise ValueError("apksigner output must contain exactly one signer")
    return signers


def _chunks(source: BinaryIO) -> list[tuple[bytes, bytes]]:
    if source.read(8) != _PNG_SIGNATURE:
        raise ValueError("not a PNG")
    result: list[tuple[bytes, bytes]] = []
    while True:
        raw_length = source.read(4)
        if len(raw_length) != 4:
            raise ValueError("truncated PNG")
        length = struct.unpack(">I", raw_length)[0]
        kind = source.read(4)
        data = source.read(length)
        checksum = source.read(4)
        if len(kind) != 4 or len(data) != length or len(checksum) != 4:
            raise ValueError("truncated PNG chunk")
        result.append((kind, data))
        if kind == b"IEND":
            return result


def _paeth(left: int, above: int, upper_left: int) -> int:
    prediction = left + above - upper_left
    left_distance = abs(prediction - left)
    above_distance = abs(prediction - above)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _png_pixels(data: bytes) -> bytes:
    """Return a compression/filter-independent digest input for common icon PNGs."""

    from io import BytesIO

    chunks = _chunks(BytesIO(data))
    headers = [value for kind, value in chunks if kind == b"IHDR"]
    if len(headers) != 1 or len(headers[0]) != 13:
        raise ValueError("PNG must contain one IHDR")
    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", headers[0]
    )
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if channels is None or bit_depth != 8 or compression != 0 or filtering != 0 or interlace != 0:
        raise ValueError("unsupported launcher PNG encoding")
    stride = width * channels
    inflated = zlib.decompress(b"".join(value for kind, value in chunks if kind == b"IDAT"))
    if len(inflated) != height * (stride + 1):
        raise ValueError("unexpected launcher PNG scanline size")
    previous = bytearray(stride)
    pixels = bytearray()
    offset = 0
    for _ in range(height):
        filter_type = inflated[offset]
        source = inflated[offset + 1 : offset + 1 + stride]
        offset += stride + 1
        current = bytearray(stride)
        for index, value in enumerate(source):
            left = current[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            predictor = {
                0: 0,
                1: left,
                2: above,
                3: (left + above) // 2,
                4: _paeth(left, above, upper_left),
            }.get(filter_type)
            if predictor is None:
                raise ValueError("unsupported launcher PNG filter")
            current[index] = (value + predictor) & 0xFF
        pixels.extend(current)
        previous = current
    return headers[0] + bytes(pixels)


def _same_image(expected: bytes, actual: bytes) -> bool:
    if expected == actual:
        return True
    try:
        return _png_pixels(expected) == _png_pixels(actual)
    except ValueError:
        return False


def verify_launcher_resources(apk: Path, generated_res: Path) -> None:
    adaptive = generated_res / "mipmap-anydpi-v26" / "ic_launcher.xml"
    if not adaptive.is_file():
        raise ValueError(f"missing generated launcher: {adaptive}")
    adaptive_source = adaptive.read_text()
    if "ic_launcher_foreground" not in adaptive_source:
        raise ValueError("adaptive launcher does not reference the foreground resource")
    if "ic_launcher_background" not in adaptive_source:
        raise ValueError("adaptive launcher does not reference the background resource")
    background = generated_res / "values" / "ic_launcher_background.xml"
    if not background.is_file():
        raise ValueError(f"missing generated launcher background: {background}")
    if "ic_launcher_background" not in background.read_text():
        raise ValueError("generated launcher background has an unexpected resource name")

    with zipfile.ZipFile(apk) as archive:
        names = set(archive.namelist())
        if "resources.arsc" not in names:
            raise ValueError("APK is missing the compiled Android resource table")
        archive_pngs = [
            (name, archive.read(name)) for name in names if name.endswith(".png")
        ]
        for apk_name, actual in archive_pngs:
            digest = hashlib.sha256(actual).hexdigest()
            if Path(apk_name).name in LAUNCHER_NAMES and digest in KNOWN_TEMPLATE_LAUNCHER_SHA256:
                raise ValueError(f"APK contains the known template launcher: {apk_name}")
        for density in DENSITIES:
            for launcher_name in LAUNCHER_NAMES:
                source = generated_res / f"mipmap-{density}" / launcher_name
                if not source.is_file():
                    raise ValueError(f"missing generated launcher: {source}")
                expected = source.read_bytes()
                match_index = next(
                    (
                        index
                        for index, (_, actual) in enumerate(archive_pngs)
                        if _same_image(expected, actual)
                    ),
                    None,
                )
                if match_index is None:
                    raise ValueError(
                        "APK is missing generated launcher image: "
                        f"{density}/{launcher_name}"
                    )
                archive_pngs.pop(match_index)


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else -1 for part in path.parent.name.split("."))


def find_android_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable:
        return executable
    candidates: list[Path] = []
    for variable in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        configured = os.environ.get(variable)
        if configured:
            candidates.extend((Path(configured) / "build-tools").glob(f"*/{name}"))
    if candidates:
        return str(max(candidates, key=_version_key))
    raise ValueError(f"Android build tool is unavailable: {name}")


def _run_tool(command: list[str]) -> str:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(f"Android build tool failed: {detail}")
    return result.stdout


def read_badging(apk: Path) -> AndroidPackageMetadata:
    return parse_badging(_run_tool([find_android_tool("aapt"), "dump", "badging", str(apk)]))


def read_signers(apk: Path) -> tuple[str, ...]:
    return parse_signers(
        _run_tool([find_android_tool("apksigner"), "verify", "--print-certs", str(apk)])
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--generated-res", type=Path, required=True)
    parser.add_argument("--expected-package", required=True)
    parser.add_argument("--expected-version-name", required=True)
    parser.add_argument("--expected-version-code", type=int, required=True)
    parser.add_argument("--expected-cert-sha256")
    return parser


def main() -> int:
    args = _parser().parse_args()
    metadata = read_badging(args.apk)
    expected = AndroidPackageMetadata(
        package_name=args.expected_package,
        version_name=args.expected_version_name,
        version_code=args.expected_version_code,
    )
    if metadata != expected:
        raise ValueError(f"APK identity mismatch: expected {expected}, found {metadata}")
    verify_launcher_resources(args.apk, args.generated_res)
    if args.expected_cert_sha256:
        expected_cert = args.expected_cert_sha256.replace(":", "").upper()
        signer = read_signers(args.apk)[0]
        if signer != expected_cert:
            raise ValueError(f"APK signer mismatch: expected {expected_cert}, found {signer}")
    print(
        f"verified Android APK: package={metadata.package_name} "
        f"versionName={metadata.version_name} versionCode={metadata.version_code}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
