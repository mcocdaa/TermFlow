# Tauri Android Status Bar Orientation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Make every Tauri Android page keep the status bar visible in portrait, hide it transiently by swipe in landscape, and keep the mobile layout stable while the transient bar is visible.

**Architecture:** A fail-closed Python configurator patches Tauri's generated Android Activity after every android init, because gen/android is regenerated in CI. Tauri startup labels Android in the DOM; shared CSS applies top safe-area spacing in portrait and locks the Android landscape top inset to zero so a transient system bar overlays rather than reflows the WebView.

**Tech Stack:** Python 3.12 + pytest, Kotlin/AndroidX WindowInsetsControllerCompat, Tauri 2.11, TypeScript/Vitest, Vue CSS, GitHub Actions.

---

## File structure

- Create: scripts/release/configure_android_system_bars.py — deterministic, idempotent Android Activity template configurator.
- Create: tests/release/test_configure_android_system_bars.py — configurator red/green and template-drift tests.
- Create: apps/clients/tauri/src/tauriPlatformAttribute.ts — Tauri-only DOM platform marker, independently testable.
- Create: apps/clients/tauri/src/tauriPlatformAttribute.test.ts — Android and non-Android DOM marker tests.
- Modify: apps/clients/tauri/src/main.ts — set the marker before Vue renders.
- Modify: packages/client-ui/src/styles/app.css — normal and bare-page safe-area padding plus Android landscape no-reflow variable.
- Modify: packages/client-ui/src/styles/terminal-responsive.css — terminal titlebar top safe-area padding.
- Modify: packages/client-ui/src/test/responsive-contract.test.ts — source-level safe-area and platform scoping contract.
- Create: tests/release/test_ci_android_system_bars_contract.py — CI debug APK initialization ordering contract.
- Modify: tests/release/test_packaging_workflow_contract.py — reusable Android APK initialization ordering contract.
- Modify: .github/workflows/ci.yml — configure generated Activity before debug APK build.
- Modify: .github/workflows/tauri-packages.yml — configure generated Activity before debug, signed candidate, and release APK builds.

### Task 1: Fail-closed Android Activity configurator

**Files:**
- Create: scripts/release/configure_android_system_bars.py
- Test: tests/release/test_configure_android_system_bars.py

- [ ] **Step 1: Write the failing Activity-configurator tests**

Create tests/release/test_configure_android_system_bars.py with the exact minimal Tauri template and assertions:

~~~python
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
        "package io.termflow.client\\n",
        TAURI_ACTIVITY.replace("import android.os.Bundle\\n", ""),
        TAURI_ACTIVITY.replace("class MainActivity", "class MainActivity\\nclass MainActivity"),
    ],
)
def test_rejects_unknown_or_ambiguous_tauri_activity_template(source: str) -> None:
    with pytest.raises(ValueError, match="unsupported Tauri Android activity template"):
        configure_activity(source)
~~~

- [ ] **Step 2: Run the test and verify it fails because the module does not exist**

Run:

~~~bash
uv run --no-cache --frozen pytest -q tests/release/test_configure_android_system_bars.py
~~~

Expected: collection fails with ModuleNotFoundError for scripts.release.configure_android_system_bars.

- [ ] **Step 3: Implement the smallest idempotent configurator**

Create scripts/release/configure_android_system_bars.py. Use a single marker and exact Tauri template fragments; do not patch an unrecognized Activity:

~~~python
#!/usr/bin/env python3
"""Configure status-bar behavior in a generated Tauri Android Activity."""

from __future__ import annotations

import argparse
from pathlib import Path

_MARKER = "// TERMFLOW_ANDROID_SYSTEM_BARS"
_IMPORT_MARKER = "import android.os.Bundle\\n"
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


def _require_once(source: str, marker: str) -> None:
    if source.count(marker) != 1:
        raise ValueError("unsupported Tauri Android activity template")


def configure_activity(source: str) -> str:
    """Return one known generated Activity with TermFlow system-bar behavior."""

    if _MARKER in source:
        if source.count(_MARKER) != 1:
            raise ValueError("unsupported Tauri Android activity template")
        return source
    _require_once(source, _IMPORT_MARKER)
    _require_once(source, _CLASS_MARKER)
    source = source.replace(_IMPORT_MARKER, f"{_IMPORT_MARKER}{_IMPORTS}", 1)
    return source.replace(
        _CLASS_MARKER,
        f"{_MARKER}\\n{_CONFIGURED_CLASS}",
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
~~~

Do not call hide(systemBars()) and do not add Activity padding. Only statusBars() is in scope.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run:

~~~bash
uv run --no-cache --frozen pytest -q tests/release/test_configure_android_system_bars.py
~~~

Expected: all configurator tests pass.

- [ ] **Step 5: Commit the configurator**

~~~bash
git add scripts/release/configure_android_system_bars.py tests/release/test_configure_android_system_bars.py
git commit -m "feat(android): configure orientation-aware status bars"
~~~

### Task 2: Mark Android in the Tauri DOM

**Files:**
- Create: apps/clients/tauri/src/tauriPlatformAttribute.ts
- Create: apps/clients/tauri/src/tauriPlatformAttribute.test.ts
- Modify: apps/clients/tauri/src/main.ts

- [ ] **Step 1: Write the failing DOM-marker test**

Create apps/clients/tauri/src/tauriPlatformAttribute.test.ts:

~~~ts
import { describe, expect, it } from 'vitest'
import { setTauriPlatformAttribute } from './tauriPlatformAttribute'

describe('setTauriPlatformAttribute', () => {
  it('marks Android and removes stale platform markers elsewhere', () => {
    const root = document.documentElement

    setTauriPlatformAttribute(root, 'android')
    expect(root.dataset.tauriPlatform).toBe('android')

    setTauriPlatformAttribute(root, 'windows')
    expect(root.dataset.tauriPlatform).toBeUndefined()
  })
})
~~~

- [ ] **Step 2: Run it and verify it fails because the helper does not exist**

Run:

~~~bash
npm run test:run --workspace @termflow/tauri-client -- src/tauriPlatformAttribute.test.ts
~~~

Expected: Vitest fails to resolve ./tauriPlatformAttribute.

- [ ] **Step 3: Add the platform helper and call it before Vue mount**

Create apps/clients/tauri/src/tauriPlatformAttribute.ts:

~~~ts
export function setTauriPlatformAttribute(root: HTMLElement, currentPlatform: string): void {
  if (currentPlatform === 'android') root.dataset.tauriPlatform = 'android'
  else delete root.dataset.tauriPlatform
}
~~~

Modify apps/clients/tauri/src/main.ts:

~~~ts
import { platform } from '@tauri-apps/plugin-os'
import { setTauriPlatformAttribute } from './tauriPlatformAttribute'

async function start() {
  setTauriPlatformAttribute(document.documentElement, platform())
  const runtime = await createTauriRuntime()
  // retain the existing router, theme, and Vue mount sequence
}
~~~

The helper deliberately has no shared-UI dependency and the marker is only installed by the Tauri entry point; Web and iOS browser clients never acquire it.

- [ ] **Step 4: Run the focused test and Tauri typecheck**

Run:

~~~bash
npm run test:run --workspace @termflow/tauri-client -- src/tauriPlatformAttribute.test.ts
npm run typecheck --workspace @termflow/tauri-client
~~~

Expected: the marker test passes and Vue/TypeScript reports no errors.

- [ ] **Step 5: Commit the Android DOM marker**

~~~bash
git add apps/clients/tauri/src/main.ts apps/clients/tauri/src/tauriPlatformAttribute.ts apps/clients/tauri/src/tauriPlatformAttribute.test.ts
git commit -m "feat(android): mark Tauri Android document root"
~~~

### Task 3: Apply portrait safe areas and Android-landscape stable layout

**Files:**
- Modify: packages/client-ui/src/styles/app.css
- Modify: packages/client-ui/src/styles/terminal-responsive.css
- Modify: packages/client-ui/src/test/responsive-contract.test.ts

- [ ] **Step 1: Extend the responsive source contract before changing CSS**

Add assertions to the existing safe-area test in packages/client-ui/src/test/responsive-contract.test.ts:

~~~ts
expect(appCss).toMatch(/@media \(pointer: coarse\)[\s\S]*\.app-header\s*\{[^}]*var\(--termflow-top-content-inset\)/)
expect(appCss).toMatch(/\.app-shell\.is-bare main\s*\{[^}]*var\(--termflow-top-content-inset\)/)
expect(css).toMatch(/\.terminal-titlebar\s*\{[^}]*var\(--termflow-top-content-inset\)/)
expect(appCss).toMatch(/@media \(pointer: coarse\) and \(orientation: landscape\)\s*\{\s*html\[data-tauri-platform='android'\]\s*\{[^}]*--termflow-top-content-inset: 0px;/s)
expect(appCss).toMatch(/\.app-header\s*\{[^}]*safe-area-inset-left[^}]*safe-area-inset-right/s)
~~~

Place the Android landscape assertion against the exact final nesting used in the stylesheet; do not weaken it into a generic safe-area-inset-top string assertion.

- [ ] **Step 2: Run the contract test and verify it fails for the missing top-inset variable**

Run:

~~~bash
npm run test:run --workspace @termflow/client-ui -- src/test/responsive-contract.test.ts
~~~

Expected: the new assertion fails because --termflow-top-content-inset is absent.

- [ ] **Step 3: Implement the minimal CSS rules**

At the end of the app layer in packages/client-ui/src/styles/app.css, add a coarse-pointer block after existing small-width overrides:

~~~css
@media (pointer: coarse) {
  html { --termflow-top-content-inset: env(safe-area-inset-top); }
  .app-header {
    padding-block-start: max(var(--space-3), var(--termflow-top-content-inset));
    padding-inline:
      max(var(--space-4), env(safe-area-inset-left))
      max(var(--space-4), env(safe-area-inset-right));
  }
  .app-shell.is-bare main {
    padding-block-start: max(clamp(var(--space-4), 3vw, 2.5rem), var(--termflow-top-content-inset));
    padding-inline:
      max(clamp(var(--space-4), 3vw, 2.5rem), env(safe-area-inset-left))
      max(clamp(var(--space-4), 3vw, 2.5rem), env(safe-area-inset-right));
  }
}

@media (pointer: coarse) and (orientation: landscape) {
  html[data-tauri-platform='android'] { --termflow-top-content-inset: 0px; }
}
~~~

In packages/client-ui/src/styles/terminal-responsive.css, extend the existing mobile .terminal-titlebar rule:

~~~css
.terminal-titlebar {
  z-index: 50;
  gap: var(--space-1);
  padding-block-start: max(var(--space-1), var(--termflow-top-content-inset, 0px));
  padding-inline:
    max(var(--space-2), env(safe-area-inset-left))
    max(var(--space-2), env(safe-area-inset-right));
}
~~~

Keep .mobile-keybar-shell bottom inset and all existing left/right inset rules. Do not set a fixed status-bar pixel value and do not change 100dvh declarations.

- [ ] **Step 4: Run the focused UI tests and typecheck**

Run:

~~~bash
npm run test:run --workspace @termflow/client-ui -- src/test/responsive-contract.test.ts
npm run typecheck --workspace @termflow/client-ui
~~~

Expected: the responsive contract and client-ui typecheck pass.

- [ ] **Step 5: Commit the safe-area layout**

~~~bash
git add packages/client-ui/src/styles/app.css packages/client-ui/src/styles/terminal-responsive.css packages/client-ui/src/test/responsive-contract.test.ts
git commit -m "fix(ui): respect Android status bar safe areas"
~~~

### Task 4: Run the configurator in both Android build paths

**Files:**
- Create: tests/release/test_ci_android_system_bars_contract.py
- Modify: tests/release/test_packaging_workflow_contract.py
- Modify: .github/workflows/ci.yml
- Modify: .github/workflows/tauri-packages.yml

- [ ] **Step 1: Write failing workflow-order tests**

Create tests/release/test_ci_android_system_bars_contract.py:

~~~python
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")


def test_unsigned_android_configures_system_bars_after_init_before_build() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["tauri-android-unsigned"]["steps"]
    init = next(index for index, step in enumerate(steps) if "android init --ci" in str(step.get("run", "")))
    system_bars = next(index for index, step in enumerate(steps) if "configure_android_system_bars.py" in str(step.get("run", "")))
    build = next(index for index, step in enumerate(steps) if "android build --debug --ci" in str(step.get("run", "")))

    assert init < system_bars < build
~~~

In test_android_release_is_signed_verified_and_iconized in tests/release/test_packaging_workflow_contract.py, add:

~~~python
system_bars = _step_index(steps, "configure_android_system_bars.py")
assert init < system_bars < icon < signing < release_build < resolve < verify < upload < cleanup
~~~

- [ ] **Step 2: Run the workflow tests and verify they fail because no step invokes the configurator**

Run:

~~~bash
uv run --no-cache --frozen pytest -q tests/release/test_ci_android_system_bars_contract.py tests/release/test_packaging_workflow_contract.py
~~~

Expected: both ordering tests fail while the existing workflow has no configure_android_system_bars.py step.

- [ ] **Step 3: Add the same post-init configuration step to both workflows**

In .github/workflows/ci.yml, immediately after the Android init step add:

~~~yaml
      - name: Configure Android orientation-aware system bars
        run: python scripts/release/configure_android_system_bars.py --activity apps/clients/tauri/src-tauri/gen/android/app/src/main/java/io/termflow/client/MainActivity.kt
~~~

In .github/workflows/tauri-packages.yml, add the identical step immediately after Generate the Android project and before Generate TermFlow launcher resources. It must be unconditional so debug APKs, signed candidates, and tag release APKs compile the same Activity.

- [ ] **Step 4: Run the workflow tests and generated-Activity compilation**

Run:

~~~bash
uv run --no-cache --frozen pytest -q tests/release/test_ci_android_system_bars_contract.py tests/release/test_packaging_workflow_contract.py
npm run tauri --workspace @termflow/tauri-client -- android init --ci
python scripts/release/configure_android_system_bars.py --activity apps/clients/tauri/src-tauri/gen/android/app/src/main/java/io/termflow/client/MainActivity.kt
cd apps/clients/tauri/src-tauri/gen/android && ./gradlew :app:compileDebugKotlin
~~~

Expected: workflow contracts pass; the generated Activity contains one marker; Kotlin compilation succeeds. If Gradle distribution download is blocked locally, record that environmental limit and rely on the unchanged CI Android debug build as the compilation gate.

- [ ] **Step 5: Commit workflow integration**

~~~bash
git add .github/workflows/ci.yml .github/workflows/tauri-packages.yml tests/release/test_ci_android_system_bars_contract.py tests/release/test_packaging_workflow_contract.py
git commit -m "build(android): configure orientation-aware system bars"
~~~

### Task 5: Final verification and Android-device acceptance

**Files:**
- Verify: all files changed in Tasks 1–4

- [ ] **Step 1: Run static and focused regression suites**

Run:

~~~bash
uv run --no-cache --frozen pytest -q tests/release/test_configure_android_system_bars.py tests/release/test_ci_android_system_bars_contract.py tests/release/test_packaging_workflow_contract.py
npm run test:run --workspace @termflow/client-ui -- src/test/responsive-contract.test.ts
npm run test:run --workspace @termflow/tauri-client -- src/tauriPlatformAttribute.test.ts
npm run typecheck --workspace @termflow/client-ui
npm run typecheck --workspace @termflow/tauri-client
git diff --check
~~~

Expected: every command exits 0 and git diff --check reports no whitespace errors.

- [ ] **Step 2: Run the repository aggregate checks**

Run:

~~~bash
uv run --all-packages ruff check .
npm run test:run
npm run typecheck
~~~

Expected: lint, workspace tests, and workspace typechecks exit 0.

- [ ] **Step 3: Verify a generated Activity is exactly configured once**

Run:

~~~bash
npm run tauri --workspace @termflow/tauri-client -- android init --ci
python scripts/release/configure_android_system_bars.py --activity apps/clients/tauri/src-tauri/gen/android/app/src/main/java/io/termflow/client/MainActivity.kt
python scripts/release/configure_android_system_bars.py --activity apps/clients/tauri/src-tauri/gen/android/app/src/main/java/io/termflow/client/MainActivity.kt
rg -n "TERMFLOW_ANDROID_SYSTEM_BARS|BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE|statusBars" apps/clients/tauri/src-tauri/gen/android/app/src/main/java/io/termflow/client/MainActivity.kt
~~~

Expected: exactly one marker, a landscape hide(statusBars) branch, and a portrait show(statusBars) branch.

- [ ] **Step 4: Perform physical Android acceptance before release**

Use a top-round-hole Android device. Check portrait status bar visibility and top control reachability; rotate to landscape and verify the system status bar hides for every Tauri page; transiently reveal it with a system-bar-edge swipe; compare element geometry/screenshots before, during, and after the overlay; verify the page has no layout movement and side cutouts do not cover controls. Return to portrait and verify status-bar restoration.

- [ ] **Step 5: Commit any acceptance-only test changes and report limits**

If Step 4 adds no tracked source or test changes, do not create an empty commit. Report separately:

- local static and Kotlin compile evidence;
- CI Android debug/signed candidate evidence;
- physical-device acceptance evidence;
- any unavailable Gradle/network/device limitation.
