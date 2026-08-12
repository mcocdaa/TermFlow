# Docker A Term Shell Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the tmux Term reached from Web C or `termflow attach` use Bash by default and allow Docker A operators to select POSIX sh with `TERMFLOW_SHELL=sh`.

**Architecture:** Keep the policy at the Docker A boundary: `deploy/entrypoint.node.sh` validates the two supported names and exports an absolute `SHELL` before `termflow serve` starts tmux. Leave the Python tmux lifecycle and all non-Docker Computer A behavior unchanged; prove the result by querying the actual tmux pane command in a built Node image.

**Tech Stack:** POSIX shell, Docker, tmux 3.2+, pytest contract tests, Markdown documentation.

---

## File map

- Modify `deploy/entrypoint.node.sh`: validate `TERMFLOW_SHELL` and export `/bin/bash` or `/bin/sh` before login and tmux startup.
- Modify `tests/deploy/test_compose_contract.py`: retain the entrypoint mapping, ordering, image-smoke, and README user-interface contracts.
- Modify `scripts/verify-node-image.sh`: verify the actual initial process in the Docker A tmux pane for default Bash and configured sh, plus rejection of invalid values.
- Modify `README.md`: document the Docker A environment variable, accepted values, default, and container-recreation semantics.

No Python application file, Compose service, `.env.example`, or Web C component changes.

### Task 1: Entrypoint shell selection

**Files:**
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `deploy/entrypoint.node.sh`

- [ ] **Step 1: Write the failing entrypoint contract test**

Add this test without adding `TERMFLOW_SHELL` to the existing
`optional_environment` tuple: that tuple checks the empty-default form `${NAME:-}`, while this
variable deliberately uses `${TERMFLOW_SHELL:-bash}`.

```python
def test_node_entrypoint_selects_only_supported_term_shells() -> None:
    entrypoint = Path("deploy/entrypoint.node.sh").read_text()

    assert 'case "${TERMFLOW_SHELL:-bash}" in' in entrypoint
    assert "bash)\n        SHELL=/bin/bash" in entrypoint
    assert "sh)\n        SHELL=/bin/sh" in entrypoint
    assert 'echo "invalid TERMFLOW_SHELL: expected bash or sh" >&2' in entrypoint
    assert "exit 64" in entrypoint
    assert "export SHELL" in entrypoint
    assert entrypoint.index('case "${TERMFLOW_SHELL:-bash}" in') > entrypoint.index(
        'cd "${work_dir}"'
    )
    assert entrypoint.index("export SHELL") < entrypoint.index(
        'if [ ! -f "${HOME}/.config/termflow/config.json" ]'
    )
```

- [ ] **Step 2: Run the targeted test and confirm the new contract fails**

Run:

```bash
uv run --frozen --all-packages python -m pytest \
  tests/deploy/test_compose_contract.py::test_node_entrypoint_selects_only_supported_term_shells -q
```

Expected: FAIL because the entrypoint does not contain the `TERMFLOW_SHELL` case statement.

- [ ] **Step 3: Implement the minimal POSIX-shell mapping**

Insert the following block in `deploy/entrypoint.node.sh` immediately after `cd "${work_dir}"` and before the automatic-login condition:

```sh
case "${TERMFLOW_SHELL:-bash}" in
    bash)
        SHELL=/bin/bash
        ;;
    sh)
        SHELL=/bin/sh
        ;;
    *)
        echo "invalid TERMFLOW_SHELL: expected bash or sh" >&2
        exit 64
        ;;
esac
export SHELL
```

Do not execute the environment-variable value and do not pass a shell command through the Python API. The existing root initialization re-exec preserves `TERMFLOW_SHELL`; this block runs after the process has dropped to the `termflow` user and before network login, tmux, or Bridge startup.

- [ ] **Step 4: Run the entrypoint and existing deployment contract tests**

Run:

```bash
uv run --frozen --all-packages python -m pytest tests/deploy/test_compose_contract.py -q
bash -n deploy/entrypoint.node.sh
```

Expected: all deployment contract tests PASS and `bash -n` exits 0.

- [ ] **Step 5: Commit the entrypoint behavior**

```bash
git add -- deploy/entrypoint.node.sh tests/deploy/test_compose_contract.py
git commit -m "feat(node): configure Docker Term shell"
```

### Task 2: Built-image tmux shell proof

**Files:**
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `scripts/verify-node-image.sh`

- [ ] **Step 1: Write the failing image-verifier contract**

Add this test to `tests/deploy/test_compose_contract.py`:

```python
def test_node_image_verifier_proves_the_actual_tmux_shell() -> None:
    verifier = Path("scripts/verify-node-image.sh").read_text()

    for expected in (
        "TERMFLOW_SHELL=sh",
        "TERMFLOW_SHELL=zsh",
        "#{pane_current_command}",
        'test "$(pane_shell "${first_status}")" = "bash"',
        'test "$(pane_shell "${third_status}")" = "sh"',
        "invalid TERMFLOW_SHELL: expected bash or sh",
    ):
        assert expected in verifier
```

- [ ] **Step 2: Run the targeted contract and confirm it fails**

Run:

```bash
uv run --frozen --all-packages python -m pytest \
  tests/deploy/test_compose_contract.py::test_node_image_verifier_proves_the_actual_tmux_shell -q
```

Expected: FAIL because the verifier does not yet inspect `#{pane_current_command}` or run the configured-shell cases.

- [ ] **Step 3: Add a pane-shell helper and invalid-value smoke**

After the cleanup trap in `scripts/verify-node-image.sh`, add:

```bash
pane_shell() {
  local status_payload="$1"
  local socket_path
  socket_path="$(printf '%s' "${status_payload}" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["socket_path"])')"
  docker exec --user termflow "${NODE_CONTAINER}" \
    tmux -S "${socket_path}" display-message -p -t demo:0.0 '#{pane_current_command}'
}

invalid_shell_output=""
if invalid_shell_output="$(docker run --rm \
  "${NODE_RUNTIME_SECURITY_ARGS[@]}" \
  --env TERMFLOW_SHELL=zsh \
  "${NODE_IMAGE}" true 2>&1)"; then
  echo "node entrypoint accepted an unsupported TERMFLOW_SHELL" >&2
  exit 1
fi
printf '%s\n' "${invalid_shell_output}" \
  | grep -Fq "invalid TERMFLOW_SHELL: expected bash or sh"
```

This verifies a non-zero container exit and a bounded fixed error message without interpolating or executing the rejected value.

- [ ] **Step 4: Prove default Bash in the existing persistent-node smoke**

Immediately after the existing `first_status` health assertions, add:

```bash
test "$(pane_shell "${first_status}")" = "bash"
```

This reads the real tmux pane process rather than only checking the container environment.

- [ ] **Step 5: Recreate the same Docker A with sh and prove identity plus shell**

After the existing `single_instance` assertion, append:

```bash
# Environment changes require container recreation. Preserve the identity
# volume, then prove the same Term comes back with POSIX sh as its pane shell.
docker stop --time 15 "${NODE_CONTAINER}" >/dev/null
docker rm "${NODE_CONTAINER}" >/dev/null
docker run --detach \
  --name "${NODE_CONTAINER}" \
  "${NODE_RUNTIME_SECURITY_ARGS[@]}" \
  --env TERMFLOW_SHELL=sh \
  --volume "${NODE_VOLUME}:/home/termflow" \
  "${NODE_IMAGE}" \
  termflow serve --name demo >/dev/null

third_status=""
for attempt in $(seq 1 30); do
  third_status="$(docker exec --user termflow "${NODE_CONTAINER}" \
    termflow status demo --json 2>/dev/null)" || { sleep 1; continue; }
  echo "${third_status}" | grep -q '"bridge_alive":true' && break
  sleep 1
done
third_id="$(echo "${third_status}" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["instance_id"])')"
test "${third_id}" = "${first_id}"
test "$(pane_shell "${third_status}")" = "sh"
```

The existing cleanup trap remains responsible for deleting only the uniquely named temporary container and volume.

- [ ] **Step 6: Run static syntax and contract checks**

Run:

```bash
bash -n scripts/verify-node-image.sh
uv run --frozen --all-packages python -m pytest tests/deploy/test_compose_contract.py -q
```

Expected: shell syntax exits 0 and all deployment contract tests PASS.

- [ ] **Step 7: Build the Node image and run the dynamic verifier**

Run:

```bash
scripts/build-node-image.sh termflow-node:verify
scripts/verify-node-image.sh termflow-node:verify
```

Expected: both commands exit 0. The verifier observes `bash` in the default tmux pane, rejects `zsh`, recreates Docker A with the same identity volume, and observes `sh` in the restored Term.

- [ ] **Step 8: Commit the image-level proof**

```bash
git add -- scripts/verify-node-image.sh tests/deploy/test_compose_contract.py
git commit -m "test(node): verify Docker Term shell selection"
```

### Task 3: Docker A operator documentation

**Files:**
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `README.md`

- [ ] **Step 1: Write the failing README contract**

Extend `test_readme_docker_node_uses_local_managed_directories` with:

```python
    assert "Web C 进入 Docker A 的 Term 默认使用 Bash" in readme
    assert "--env TERMFLOW_SHELL=sh" in readme
    assert "只接受 `bash` 和 `sh`" in readme
    assert "重新创建 Docker A 容器" in readme
```

- [ ] **Step 2: Run the README contract and confirm it fails**

Run:

```bash
uv run --frozen --all-packages python -m pytest \
  tests/deploy/test_compose_contract.py::test_readme_docker_node_uses_local_managed_directories -q
```

Expected: FAIL because README has not documented `TERMFLOW_SHELL`.

- [ ] **Step 3: Document the default and configured shell**

Add this text immediately after the Docker A `docker run` example and before “进入 Docker A 的 Term”:

````markdown
Web C 进入 Docker A 的 Term 默认使用 Bash。如需改用 POSIX sh，在上述
`docker run` 命令中追加：

```bash
--env TERMFLOW_SHELL=sh
```

`TERMFLOW_SHELL` 只接受 `bash` 和 `sh`。修改后需要保留身份和工作目录、重新创建
Docker A 容器；新的 tmux pane 使用所选 shell，已经运行的 pane 不会热切换。
````

Keep the main example on the default Bash path so first-time installation needs no extra environment variable.

- [ ] **Step 4: Run the documentation and deployment contracts**

Run:

```bash
uv run --frozen --all-packages python -m pytest \
  tests/deploy/test_compose_contract.py tests/docs/test_documentation_contract.py -q
git diff --check
```

Expected: all selected tests PASS and `git diff --check` reports no errors.

- [ ] **Step 5: Commit the operator documentation**

```bash
git add -- README.md tests/deploy/test_compose_contract.py
git commit -m "docs: explain Docker Term shell selection"
```

### Task 4: Final regression verification

**Files:**
- Verify only; no new files expected.

- [ ] **Step 1: Run the complete Node unit and integration test directory**

```bash
uv run --frozen --package termflow-node python -m pytest apps/node/tests -q
```

Expected: all Node tests PASS; tmux integration tests run where the host provides tmux and otherwise retain their existing skip behavior.

- [ ] **Step 2: Re-run deployment/docs contracts and shell syntax**

```bash
uv run --frozen --all-packages python -m pytest \
  tests/deploy/test_compose_contract.py tests/docs/test_documentation_contract.py -q
bash -n deploy/entrypoint.node.sh scripts/verify-node-image.sh
```

Expected: all selected tests PASS and both scripts parse successfully.

- [ ] **Step 3: Re-run built-image verification as runtime evidence**

```bash
scripts/build-node-image.sh termflow-node:verify
scripts/verify-node-image.sh termflow-node:verify
```

Expected: image build and all runtime smoke checks PASS, including the actual tmux pane process checks for default `bash` and configured `sh`.

- [ ] **Step 4: Confirm repository scope and commit history**

```bash
git diff --check
git status --short
git log -5 --oneline
```

Expected: no uncommitted changes; the log contains the design commit, plan commit, and the three scoped implementation commits. Do not describe static pytest results as Docker runtime evidence; only the completed image verifier supports the shell-in-pane claim.
