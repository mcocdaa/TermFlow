from __future__ import annotations

import pytest

from scripts.release.configure_android_system_bars import configure_activity

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
