# CI tmux And Pytest Entrypoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept the supported `tmux 3.2a` release string and make repository-local release modules import reliably in CI without increasing the test count.

**Architecture:** Keep version validation inside `TmuxRunner`, widening only the official alphabetic suffix syntax while preserving the numeric minimum-version comparison. Run pytest through the selected workspace Python interpreter so the repository root participates in module resolution; reuse existing test coverage instead of creating workflow-specific tests.

**Tech Stack:** Python 3.12, pytest, uv workspace, GitHub Actions, Bash.

---

### Task 1: Accept official tmux alphabetic suffixes

**Files:**
- Modify: `apps/node/tests/test_tmux_runner.py:65-76`
- Modify: `apps/node/src/termflow_node/tmux/runner.py:44`

- [ ] **Step 1: Reuse an existing parameterized input as the regression case**

Replace the first existing input without adding a test or parameter row:

```python
    [
        ("version check: tmux 3.2a (Linux)\n", ""),
        ("", "tmux 3.4\n"),
    ],
```

- [ ] **Step 2: Run the existing test and verify the reused case fails**

Run:

```bash
.venv/bin/python -m pytest -q apps/node/tests/test_tmux_runner.py
```

Expected: one existing parameter row fails with `TmuxUnavailable` for `tmux 3.2a`; the test count remains 10.

- [ ] **Step 3: Make the minimal parser change**

Change the version expression to accept only optional ASCII letters after the minor number while retaining a boundary after the full version token:

```python
_VERSION = re.compile(r"\btmux\s+(\d+)\.(\d+)[a-z]*\b", re.IGNORECASE)
```

- [ ] **Step 4: Run the same existing test file**

Run:

```bash
.venv/bin/python -m pytest -q apps/node/tests/test_tmux_runner.py
```

Expected: `10 passed`; no test count increase.

### Task 2: Use the Python module entrypoint for pytest

**Files:**
- Modify: `.github/workflows/ci.yml:89-90`
- Modify: `scripts/verify.sh:19-20`

- [ ] **Step 1: Preserve the confirmed failing entrypoint evidence**

Run:

```bash
.venv/bin/pytest -q tests/release/test_build_version.py tests/release/test_check_version.py tests/release/test_version_materialization.py
```

Expected: collection fails with three `ModuleNotFoundError: No module named 'scripts'` errors.

- [ ] **Step 2: Update both existing verification entrypoints**

Use this command in the CI workflow and repository verification script:

```bash
uv run --all-packages python -m pytest -q
```

Keep CI's existing JUnit argument after `-q`:

```yaml
run: uv run --all-packages python -m pytest -q --junitxml=.ci/pytest.xml
```

- [ ] **Step 3: Verify the existing release suite through the module entrypoint**

Run:

```bash
.venv/bin/python -m pytest -q tests/release/test_build_version.py tests/release/test_check_version.py tests/release/test_version_materialization.py
```

Expected: `45 passed`; no new tests.

### Task 3: Verify and commit the combined repair

**Files:**
- Verify: `apps/node/src/termflow_node/tmux/runner.py`
- Verify: `apps/node/tests/test_tmux_runner.py`
- Verify: `.github/workflows/ci.yml`
- Verify: `scripts/verify.sh`

- [ ] **Step 1: Run the full existing Python suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: the current full suite passes with no added test count.

- [ ] **Step 2: Run static checks for the changed Python source**

Run:

```bash
.venv/bin/ruff check apps/node/src/termflow_node/tmux/runner.py apps/node/tests/test_tmux_runner.py
```

Expected: exit code 0.

- [ ] **Step 3: Review the final diff and whitespace**

Run:

```bash
git diff --check
git diff --stat
```

Expected: only the four implementation files and this plan differ after its documentation commit; no whitespace errors.

- [ ] **Step 4: Commit the implementation**

```bash
git add -- apps/node/src/termflow_node/tmux/runner.py \
  apps/node/tests/test_tmux_runner.py .github/workflows/ci.yml scripts/verify.sh
git commit -m "fix(ci): accept tmux 3.2a and preserve release imports"
```
