# Test Suite Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate TermFlow's existing tests so every long-lived contract has one owner and repeated Control Plane setup is reusable, without adding product behavior or test cases.

**Architecture:** Keep behavior tests at their owning layer and remove only duplicated static assertions. Add test-only provisioning factories in the Control Plane `conftest.py`; production code remains untouched. Existing tests are the refactoring safety net, so each task records collection/pass results before and after the edit.

**Tech Stack:** Pytest 9, FastAPI TestClient, Vitest 3, Vue Test Utils, Playwright contracts, GitHub Actions YAML.

---

## File map

- `tests/release/test_packaging_workflow_contract.py`: sole owner of package workflow structure and artifact policy.
- `tests/release/test_check_version.py`: version checker behavior only.
- `tests/deploy/test_compose_contract.py`: Compose, image and local delivery contracts only.
- `tests/test_repository_contract.py`: delete after its weak checks are covered by owning suites.
- `tests/test_client_workspace_contract.py`: sole cross-workspace static privacy and dependency boundary.
- `apps/clients/web/src/test/privacy-contract.test.ts`: dynamic browser terminal-output privacy only.
- `apps/control-plane/tests/conftest.py`: reusable Computer and Term provisioning records/factories.
- `apps/control-plane/tests/test_computers_api.py`, `test_dashboard_api.py`, `test_events_websocket.py`, `test_instance_api.py`, `test_terminal_websocket.py`, and `test_terms_api.py`: consume shared provisioning factories where enrollment/register is only setup.
- `tests/e2e/test_unified_auth.py`: classify existing real-process tests as E2E.
- `packages/client-ui/src/test/responsive-contract.test.ts`: retain only cross-implementation responsive invariants.
- `tests/docs/test_documentation_contract.py`: retain discoverability, obsolete-setting and current operator-command contracts.

### Task 1: Record the immutable baseline and classify existing E2E tests

**Files:**
- Modify: `tests/e2e/test_unified_auth.py:1-15`
- Test: `tests/e2e/test_unified_auth.py`

- [ ] **Step 1: Record current collection and code-size baselines**

Run:

```bash
.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider --disable-warnings
npm run test:run
find apps packages tests -type f \( -name 'test_*.py' -o -name '*.test.ts' -o -name '*.spec.ts' \) -print0 | xargs -0 wc -l | tail -1
```

Expected: Python collects 640 tests, Vitest reports 222 passing tests, and test code totals about 20,454 lines. If environment-specific tests cannot execute, retain the collection result and record the exact limitation.

- [ ] **Step 2: Add the missing module marker without adding a test**

Add below the third-party imports:

```python
import pytest

pytestmark = pytest.mark.e2e
```

- [ ] **Step 3: Verify marker selection**

Run:

```bash
.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider --disable-warnings -m e2e
.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider --disable-warnings -m 'not e2e and not tmux'
```

Expected: E2E selection grows from 8 to 11; the fast selection loses exactly the three real-process unified-auth tests; total collection remains 640.

### Task 2: Establish one owner for release and repository contracts

**Files:**
- Modify: `tests/release/test_packaging_workflow_contract.py:129-196`
- Modify: `tests/release/test_check_version.py:1-113`
- Modify: `tests/deploy/test_compose_contract.py:126-294`
- Delete: `tests/test_repository_contract.py`
- Test: `tests/release/test_packaging_workflow_contract.py`
- Test: `tests/release/test_check_version.py`
- Test: `tests/deploy/test_compose_contract.py`

- [ ] **Step 1: Prove the owning release contract already covers the shared behavior**

Run:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_check_version.py \
  tests/deploy/test_compose_contract.py \
  tests/test_repository_contract.py
```

Expected: all existing tests pass before consolidation.

- [ ] **Step 2: Strengthen existing client-workflow owner without creating a test**

Extend `test_client_workflow_is_manual_and_reusable` with the durable structured checks:

```python
assert workflow["permissions"] == {"contents": "read"}
assert set(workflow["jobs"]) == {
    "validate-version",
    "windows-nsis",
    "linux-packages",
    "macos-packages",
    "android-debug-apk",
    "ios-simulator-app",
}
```

Extend `test_client_artifact_names_are_manual_by_default_and_tagged_when_called` with durable packaging commands and publication exclusions:

```python
for required in (
    "--bundles nsis",
    "--bundles deb,appimage",
    "--bundles app,dmg",
    "android build --debug --ci --target aarch64 --apk",
    "ios build --debug --ci --target aarch64-sim --no-sign",
    "actions/upload-artifact@v4",
):
    assert required in text
for forbidden in ("contents: write", "gh release", "softprops/action-gh-release"):
    assert forbidden not in text
```

- [ ] **Step 3: Remove duplicate owners**

Delete `test_native_package_workflow_is_manual_and_reusable_without_publish_permissions` and its now-unused `yaml` import from `test_check_version.py`. Delete `test_tauri_packages_are_manual_and_reusable_native_artifacts` from `test_compose_contract.py`. Delete `tests/test_repository_contract.py` because CI, release and Compose tests already execute or parse those entry points.

- [ ] **Step 4: Verify contract consolidation**

Run the Task 2 Step 1 command without the deleted file.

Expected: all remaining tests pass and Python collection decreases by exactly four tests.

### Task 3: Keep static privacy ownership in one workspace contract

**Files:**
- Modify: `apps/clients/web/src/test/privacy-contract.test.ts:1-73`
- Test: `tests/test_client_workspace_contract.py`
- Test: `apps/clients/web/src/test/privacy-contract.test.ts`

- [ ] **Step 1: Verify both current owners are green**

Run:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_client_workspace_contract.py
npm run test:run --workspace @termflow/web-client
```

Expected: six Python workspace contracts and 20 Web Vitest tests pass.

- [ ] **Step 2: Remove duplicated Web source scans**

Delete the `productionFiles`, `clientProductionFiles`, and `relativeToWorkspace` helpers and the first two static `it(...)` blocks. Reduce imports to:

```typescript
import { describe, expect, it, vi } from 'vitest'
import { TerminalSession, type TerminalSessionCallbacks } from '@termflow/client-core'
import { createBrowserTerminalTransport } from '../adapters/browserTerminalTransport'
```

Keep the dynamic `keeps terminal output out of storage, URL, console, and telemetry-shaped globals` test unchanged.

- [ ] **Step 3: Verify the sole static owner and dynamic browser behavior**

Run the Step 1 commands again.

Expected: all six Python workspace contracts pass; Web Vitest decreases from 20 to 18 tests with dynamic privacy behavior still passing.

### Task 4: Reuse Control Plane Computer and Term setup

**Files:**
- Modify: `apps/control-plane/tests/conftest.py:1-30`
- Modify: `apps/control-plane/tests/test_computers_api.py`
- Modify: `apps/control-plane/tests/test_dashboard_api.py`
- Modify: `apps/control-plane/tests/test_events_websocket.py`
- Modify: `apps/control-plane/tests/test_instance_api.py`
- Modify: `apps/control-plane/tests/test_terminal_websocket.py`
- Modify: `apps/control-plane/tests/test_terms_api.py`

- [ ] **Step 1: Run the existing consumer tests before refactoring**

Run:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  apps/control-plane/tests/test_computers_api.py \
  apps/control-plane/tests/test_dashboard_api.py \
  apps/control-plane/tests/test_events_websocket.py \
  apps/control-plane/tests/test_instance_api.py \
  apps/control-plane/tests/test_terminal_websocket.py \
  apps/control-plane/tests/test_terms_api.py
```

Expected: all tests pass. If the managed sandbox stalls in repeated `Database.initialize()`, run each file independently with a 90-second timeout and record the limitation rather than changing production database code.

- [ ] **Step 2: Add typed test-only records and factories**

Add to `conftest.py`:

```python
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

@dataclass(frozen=True, slots=True)
class ProvisionedComputer:
    installation_id: UUID
    installation_token: str
    response: dict[str, object]

@dataclass(frozen=True, slots=True)
class ProvisionedTerm:
    computer: ProvisionedComputer
    instance_id: UUID
    instance_token: str
    response: dict[str, object]
```

Add `provision_computer` and `provision_term` fixtures returning callables. Each factory must call `raise_for_status()` after enrollment-token creation, installation enrollment, and instance registration. `provision_term` accepts optional `computer`, `instance_id`, `name`, `hostname`, `platform`, and `client_version` arguments so tests can create either a complete default chain or additional Terms for an existing Computer.

Use this complete test-only implementation:

```python
@pytest.fixture
def provision_computer(
    client: TestClient,
    admin_headers: dict[str, str],
) -> Callable[..., ProvisionedComputer]:
    def provision(
        *,
        hostname: str | None = None,
        display_name: str | None = None,
        platform: str | None = None,
        client_version: str | None = None,
    ) -> ProvisionedComputer:
        enrollment = client.post(
            "/api/v1/enrollment-tokens",
            headers=admin_headers,
            json={"display_name": display_name} if display_name is not None else None,
        )
        enrollment.raise_for_status()
        install_payload: dict[str, object] = {
            "enrollment_token": enrollment.json()["token"]
        }
        for key, value in (
            ("hostname", hostname),
            ("platform", platform),
            ("client_version", client_version),
        ):
            if value is not None:
                install_payload[key] = value
        installed = client.post("/api/v1/installations/enroll", json=install_payload)
        installed.raise_for_status()
        response = installed.json()
        return ProvisionedComputer(
            installation_id=UUID(response["installation_id"]),
            installation_token=response["installation_token"],
            response=response,
        )

    return provision


@pytest.fixture
def provision_term(
    client: TestClient,
    provision_computer: Callable[..., ProvisionedComputer],
) -> Callable[..., ProvisionedTerm]:
    def provision(
        *,
        computer: ProvisionedComputer | None = None,
        instance_id: UUID | None = None,
        name: str = "term",
        hostname: str | None = None,
        platform: str | None = None,
        client_version: str | None = None,
    ) -> ProvisionedTerm:
        owner = computer or provision_computer(
            hostname=hostname,
            platform=platform,
            client_version=client_version,
        )
        term_id = instance_id or uuid4()
        registered = client.post(
            "/api/v1/instances/register",
            headers={"Authorization": f"Bearer {owner.installation_token}"},
            json={"instance_id": str(term_id), "name": name},
        )
        registered.raise_for_status()
        response = registered.json()
        return ProvisionedTerm(
            computer=owner,
            instance_id=term_id,
            instance_token=response["instance_token"],
            response=response,
        )

    return provision
```

- [ ] **Step 3: Replace module-local setup helpers**

For each listed consumer file, remove only helpers whose purpose is the standard enrollment/register chain. Inject `provision_computer` or `provision_term` into existing test functions and read identifiers/tokens from the returned records. Keep raw requests in tests that intentionally exercise malformed enrollment, credential rejection, duplicate registration, or token lifecycle behavior.

- [ ] **Step 4: Verify unchanged behavior and collection**

Run the Step 1 command again, followed by:

```bash
.venv/bin/ruff check apps/control-plane/tests/conftest.py apps/control-plane/tests
.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider --disable-warnings apps/control-plane/tests
```

Expected: consumer behavior passes, Ruff passes, and Control Plane still collects 222 tests.

### Task 5: Compress CSS and documentation source-string contracts

**Files:**
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts:42-131`
- Modify: `tests/docs/test_documentation_contract.py:1-228`

- [ ] **Step 1: Verify current owning suites**

Run:

```bash
npm run test:run --workspace @termflow/client-ui
.venv/bin/python -m pytest -q -p no:cacheprovider tests/docs/test_documentation_contract.py
```

Expected: 119 client-ui Vitest tests and 11 documentation tests pass.

- [ ] **Step 2: Reduce responsive assertions to durable invariants**

Keep the existing viewport/component reachability test. In the CSS contract keep only flexible assertions for:

```typescript
expect(css).toContain('(pointer: coarse)')
expect(css).not.toContain('orientation:')
expect(css).toContain('safe-area-inset-bottom')
expect(css).toMatch(/\.terminal-frame,\s*\.mobile-keybar\s*\{\s*scrollbar-width: none;\s*\}/)
expect(css).toMatch(/\.terminal-frame::\-webkit-scrollbar,\s*\.mobile-keybar::\-webkit-scrollbar\s*\{\s*display: none;\s*\}/)
expect(css).toContain('overflow-x: auto;')
expect(css).toContain('touch-action: pan-x;')
expect(appCss).toMatch(/\.computer-table-head,\s*\.computer-table-row\s*\{[^}]*grid-template-columns:\s*repeat\(5,/)
expect(appCss).toMatch(/\.computer-delete-toast\s*\{[^}]*position:\s*fixed;/)
```

Remove exact TOTP, titlebar, terminal canvas, shell sizing and term-card CSS strings already covered by component or Playwright behavior tests. Do not create additional `it(...)` blocks.

- [ ] **Step 3: Reduce documentation tests to four durable contracts**

Keep and simplify existing functions so they cover:

1. V1 architecture/privacy boundaries.
2. README links to each current operator document.
3. Current docs exclude `TERMFLOW_IMAGE` and `TERMFLOW_TRUSTED_WEB_ORIGINS`.
4. README/operations/GitHub Actions docs include current install command, `docker compose ... up -d --build`, package workflow filenames, GitHub Release/GHCR distinction, `tmux 3.2`, and version precedence.

Delete exact assertions for theme names, prose wording, UI labels, platform marketing lists and repeated artifact descriptions. Reuse one existing test function for the combined operator command contract; do not add a fifth function.

- [ ] **Step 4: Verify compressed contracts**

Run the Step 1 commands again.

Expected: client-ui remains at 119 tests; documentation tests decrease from 11 to four; all pass.

### Task 6: Final layered verification and handoff

**Files:**
- Modify only files already listed if verification exposes a consolidation error.

- [ ] **Step 1: Verify collection and no test proliferation**

Run:

```bash
.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider --disable-warnings
.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider --disable-warnings -m e2e
npm run test:run
```

Expected: Python collects 629 tests, E2E collects 11, and Vitest reports 220 passing tests.

- [ ] **Step 2: Run stable Python layers**

Run:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider packages/protocol/tests
.venv/bin/python -m pytest -q -p no:cacheprovider apps/node/tests -m 'not tmux'
.venv/bin/python -m pytest -q -p no:cacheprovider tests/release tests/deploy tests/docs tests/tauri tests/contracts tests/test_client_workspace_contract.py
```

Expected: all selected tests pass. Run the Control Plane suite separately; if the managed sandbox repeats the previously observed startup stall, report the focused consumer results instead of claiming a complete Python pass.

- [ ] **Step 3: Run static validation**

Run:

```bash
.venv/bin/ruff check .
npm run typecheck
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 4: Measure the result**

Run:

```bash
find apps packages tests -type f \( -name 'test_*.py' -o -name '*.test.ts' -o -name '*.spec.ts' \) -print0 | xargs -0 wc -l | tail -1
rg -l 'read_text\(|readFileSync\(' tests apps packages -g 'test_*.py' -g '*.test.ts' | wc -l
git diff --stat HEAD~1
```

Expected: test code and source-scanning assertions decrease; no production source file appears in the implementation diff.
