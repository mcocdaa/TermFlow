# tmux Default Vertical Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make A report tmux's default `C-b "` top/bottom split binding so C can display it without hardcoding shortcuts.

**Architecture:** Keep B and C unchanged. Extend A's semantic parsing of the live `tmux list-keys -T prefix` output so a flagless `split-window` uses tmux's default vertical direction while explicit `-h` and `-v` remain supported.

**Tech Stack:** Python 3.12, pytest, tmux 3.2+, existing `TerminalBindingsPayload` protocol.

---

### Task 1: Reproduce the default binding parser failure

**Files:**
- Modify: `apps/node/tests/test_tmux_bindings.py`

- [ ] **Step 1: Change the fixture to tmux's real default output**

```python
stdout=(
    'bind-key -T prefix \\" split-window\n'
    "bind-key -T prefix \\% split-window -h\n"
    "bind-key -T prefix c new-window\n"
)
```

- [ ] **Step 2: Require the default top/bottom key**

```python
assert by_action["split_left_right"].key == "C-a %"
assert by_action["split_top_bottom"].key == 'C-a "'
```

Add a second reader fixture containing an explicit vertical flag and retain this assertion:

```python
assert explicit_by_action["split_top_bottom"].key == "C-a -"
```

- [ ] **Step 3: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=apps/node/src:packages/protocol/src /home/mcocdaa/AI_CODE/TermFlow/.venv/bin/pytest apps/node/tests/test_tmux_bindings.py -q
```

Expected: FAIL because `split_top_bottom.key` is `None`.

### Task 2: Recognize tmux's default direction

**Files:**
- Modify: `apps/node/src/termflow_node/tmux/bindings.py`
- Test: `apps/node/tests/test_tmux_bindings.py`

- [ ] **Step 1: Implement the minimal semantic rule**

```python
if executable == "split-window":
    if "-h" in arguments:
        return "split_left_right"
    return "split_top_bottom"
```

This preserves explicit `-v` and tmux's flagless default as top/bottom.

- [ ] **Step 2: Run the focused test and verify GREEN**

Run the Task 1 pytest command.

Expected: `1 passed`.

- [ ] **Step 3: Run the Node test suite**

```bash
PYTHONPATH=apps/node/src:packages/protocol/src /home/mcocdaa/AI_CODE/TermFlow/.venv/bin/pytest apps/node/tests -q
```

Expected: all Node tests pass.

- [ ] **Step 4: Verify against the two existing private tmux sockets**

Instantiate `TmuxRunner` and `TmuxBindingReader` for each `/run/user/1000/termflow/*.sock`; assert both snapshots contain:

```python
{"split_left_right": "C-b %", "split_top_bottom": 'C-b "'}
```

- [ ] **Step 5: Commit**

```bash
git add apps/node/src/termflow_node/tmux/bindings.py apps/node/tests/test_tmux_bindings.py
git commit -m "fix(node): detect tmux default vertical split binding"
```

### Task 3: Deliver the A fix

**Files:**
- Verify: `apps/node/src/termflow_node/tmux/bindings.py`
- Verify: `apps/node/tests/test_tmux_bindings.py`

- [ ] **Step 1: Run repository lint and type checks for A**

```bash
PYTHONPATH=apps/node/src:packages/protocol/src /home/mcocdaa/AI_CODE/TermFlow/.venv/bin/ruff check apps/node/src apps/node/tests
PYTHONPATH=apps/node/src:packages/protocol/src /home/mcocdaa/AI_CODE/TermFlow/.venv/bin/mypy apps/node/src
```

- [ ] **Step 2: Merge to `main` and rerun the focused regression**

Expected: the focused binding test passes on the merge commit.

- [ ] **Step 3: Restart each existing A bridge without killing its tmux session**

Use the supported TermFlow reconnect/restart path for the two known instance IDs. Do not run `termflow kill` and do not delete either private tmux socket.

- [ ] **Step 4: Verify C receives the corrected live snapshot**

Open each existing Term through B and inspect the `terminal.binding_snapshot`; require `split_top_bottom.key` to be `C-b "`, then hover “上下切分 Pane” in C and confirm the `<code>` hint displays that value.
