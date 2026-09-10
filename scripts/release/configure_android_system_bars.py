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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activity", type=Path, required=True)
    args = parser.parse_args()
    original = args.activity.read_text()
    configured = configure_activity(original)
    if configured != original:
        args.activity.write_text(configured)
    print(f"configured Android system bars: {args.activity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
