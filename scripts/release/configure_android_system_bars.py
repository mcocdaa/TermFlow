#!/usr/bin/env python3
"""Configure status-bar behavior in a generated Tauri Android Activity."""

from __future__ import annotations

import argparse
from pathlib import Path

_MARKER = "// TERMFLOW_ANDROID_SYSTEM_BARS"
_IMPORT_MARKER = "import android.os.Bundle\n"
_CLASS_MARKER = """class MainActivity : TauriActivity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    enableEdgeToEdge()
    super.onCreate(savedInstanceState)
  }
}
"""
_IMPORTS = """import android.content.res.Configuration
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
"""
_CONFIGURED_CLASS = """class MainActivity : TauriActivity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    enableEdgeToEdge()
    super.onCreate(savedInstanceState)
    updateSystemBars()
  }

  override fun onConfigurationChanged(newConfig: Configuration) {
    super.onConfigurationChanged(newConfig)
    updateSystemBars()
  }

  private fun updateSystemBars() {
    val controller = WindowInsetsControllerCompat(window, window.decorView)
    if (resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE) {
      controller.systemBarsBehavior =
        WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
      controller.hide(WindowInsetsCompat.Type.statusBars())
    } else {
      controller.show(WindowInsetsCompat.Type.statusBars())
    }
  }
}
"""
_CONFIGURED_MARKERS = (
    "import android.content.res.Configuration\n",
    "import androidx.core.view.WindowInsetsCompat\n",
    "import androidx.core.view.WindowInsetsControllerCompat\n",
    "override fun onConfigurationChanged(newConfig: Configuration)",
    "BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE",
    "controller.hide(WindowInsetsCompat.Type.statusBars())",
    "controller.show(WindowInsetsCompat.Type.statusBars())",
)


def _require_once(source: str, marker: str) -> None:
    if source.count(marker) != 1:
        raise ValueError("unsupported Tauri Android activity template")


def configure_activity(source: str) -> str:
    """Return one known generated Activity with TermFlow system-bar behavior."""

    _require_once(source, "class MainActivity")
    if _MARKER in source:
        _require_once(source, _MARKER)
        for marker in _CONFIGURED_MARKERS:
            _require_once(source, marker)
        return source

    _require_once(source, _IMPORT_MARKER)
    _require_once(source, _CLASS_MARKER)
    source = source.replace(_IMPORT_MARKER, f"{_IMPORT_MARKER}{_IMPORTS}", 1)
    return source.replace(
        _CLASS_MARKER,
        f"{_MARKER}\n{_CONFIGURED_CLASS}",
        1,
    )


_MANIFEST_ICON = 'android:icon="@mipmap/ic_launcher"'
_MANIFEST_ROUND_ICON = 'android:roundIcon="@mipmap/ic_launcher_round"'
_KEEP_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<resources xmlns:tools="http://schemas.android.com/tools"\n'
    '    tools:keep="@mipmap/ic_launcher,@mipmap/ic_launcher_round,'
    '@mipmap/ic_launcher_foreground,@color/ic_launcher_background" />\n'
)


def configure_manifest_round_icon(manifest_source: str) -> str:
    """Ensure AndroidManifest.xml references @mipmap/ic_launcher_round so AGP 9 R8 keeps it."""

    if _MANIFEST_ROUND_ICON in manifest_source:
        return manifest_source
    if _MANIFEST_ICON not in manifest_source:
        raise ValueError("unsupported Tauri AndroidManifest template")
    return manifest_source.replace(
        _MANIFEST_ICON,
        f"{_MANIFEST_ICON}\n        {_MANIFEST_ROUND_ICON}",
        1,
    )


def preserve_launcher_resources(activity_path: Path) -> None:
    """Wire android:roundIcon in AndroidManifest.xml and write res/raw/keep.xml."""

    main_dir = next((p for p in activity_path.parents if p.name == "main"), None)
    if main_dir is None:
        return
    manifest_path = main_dir / "AndroidManifest.xml"
    if manifest_path.is_file():
        original_manifest = manifest_path.read_text()
        updated_manifest = configure_manifest_round_icon(original_manifest)
        if updated_manifest != original_manifest:
            manifest_path.write_text(updated_manifest)
    res_dir = main_dir / "res"
    if res_dir.is_dir() or manifest_path.is_file():
        keep_path = res_dir / "raw" / "keep.xml"
        keep_path.parent.mkdir(parents=True, exist_ok=True)
        keep_path.write_text(_KEEP_XML)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activity", type=Path, required=True)
    args = parser.parse_args()
    original = args.activity.read_text()
    configured = configure_activity(original)
    if configured != original:
        args.activity.write_text(configured)
    preserve_launcher_resources(args.activity)
    print(f"configured Android system bars: {args.activity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
