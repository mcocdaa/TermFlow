from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release.configure_android_system_bars import (
    configure_activity,
    configure_manifest_round_icon,
    preserve_launcher_resources,
)

TAURI_ACTIVITY = """package io.termflow.client

import android.os.Bundle
import androidx.activity.enableEdgeToEdge

class MainActivity : TauriActivity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    enableEdgeToEdge()
    super.onCreate(savedInstanceState)
  }
}
"""


def test_configures_orientation_aware_transient_status_bar_once() -> None:
    configured = configure_activity(TAURI_ACTIVITY)

    assert "import android.content.res.Configuration" in configured
    assert "import androidx.core.view.WindowInsetsCompat" in configured
    assert "import androidx.core.view.WindowInsetsControllerCompat" in configured
    assert "override fun onConfigurationChanged(newConfig: Configuration)" in configured
    assert "BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE" in configured
    assert "controller.hide(WindowInsetsCompat.Type.statusBars())" in configured
    assert "controller.show(WindowInsetsCompat.Type.statusBars())" in configured
    assert configure_activity(configured) == configured


def test_preserves_round_launcher_icon_in_manifest_and_keep_xml(tmp_path: Path) -> None:
    main_dir = tmp_path / "src" / "main"
    activity = main_dir / "java" / "io" / "termflow" / "client" / "MainActivity.kt"
    activity.parent.mkdir(parents=True, exist_ok=True)
    activity.write_text(TAURI_ACTIVITY)
    manifest = main_dir / "AndroidManifest.xml"
    manifest.write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <application\n'
        '        android:icon="@mipmap/ic_launcher"\n'
        '        android:label="@string/app_name">\n'
        '    </application>\n'
        '</manifest>\n'
    )

    preserve_launcher_resources(activity)

    updated = manifest.read_text()
    assert 'android:roundIcon="@mipmap/ic_launcher_round"' in updated
    assert configure_manifest_round_icon(updated) == updated
    keep_xml = main_dir / "res" / "raw" / "keep.xml"
    assert keep_xml.is_file()
    assert "@mipmap/ic_launcher_round" in keep_xml.read_text()


@pytest.mark.parametrize(
    "source",
    [
        "package io.termflow.client\n",
        TAURI_ACTIVITY.replace("import android.os.Bundle\n", ""),
        TAURI_ACTIVITY.replace(
            "class MainActivity : TauriActivity()",
            "class MainActivity : TauriActivity()\nclass MainActivity : TauriActivity()",
        ),
        f"{TAURI_ACTIVITY}\n// TERMFLOW_ANDROID_SYSTEM_BARS\n",
    ],
)
def test_rejects_unknown_or_ambiguous_tauri_activity_template(source: str) -> None:
    with pytest.raises(ValueError, match="unsupported Tauri Android activity template"):
        configure_activity(source)
