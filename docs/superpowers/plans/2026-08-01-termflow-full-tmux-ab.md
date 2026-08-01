# TermFlow Full tmux A+B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing Node A and Control Plane B so one authenticated Web C can attach to a real tmux client PTY, receive A-authoritative terminal output and size, send raw input and semantic tmux actions, manage Computers and Terms, and reconnect without persisting terminal content.

**Architecture:** Keep the existing Pane automation channel intact and add a separate terminal channel. B terminates browser sessions and WebSockets but only routes terminal bytes; A owns tmux, the remote PTY client, authoritative size, command semantics, and the short reconnect buffer. Shared Pydantic models define the A-B wire contract and browser-facing HTTP control models.

**Tech Stack:** Python 3.12, Pydantic 2, FastAPI/Starlette WebSockets, SQLAlchemy 2 with SQLite, asyncio, POSIX PTY/ioctl, tmux 3.2+, pytest, httpx, websockets, uv, Ruff, mypy.

---

## File ownership

This plan may modify only these implementation areas, plus their tests:

- `packages/protocol/`
- `apps/node/`
- `apps/control-plane/`

It must not edit `apps/clients/web/`, `packages/design-tokens/`, Docker files, or delivery documentation. Those files belong to the Web C and delivery plans. The approved behavior is defined in
[`../specs/2026-08-01-termflow-web-control-design.md`](../specs/2026-08-01-termflow-web-control-design.md).

## Task 1: Extend topology and terminal wire models

**Files:**

- Modify: `packages/protocol/src/termflow_protocol/common.py`
- Modify: `packages/protocol/src/termflow_protocol/topology.py`
- Modify: `packages/protocol/src/termflow_protocol/messages.py`
- Modify: `packages/protocol/src/termflow_protocol/http.py`
- Modify: `packages/protocol/src/termflow_protocol/__init__.py`
- Modify: `packages/protocol/tests/test_messages.py`
- Modify: `packages/protocol/tests/test_http_models.py`

- [ ] Add failing tests proving `PaneSnapshot` accepts and serializes `left`, `top`, and raw `current_command`, while old payloads default geometry to zero and command to `None`.
- [ ] Add failing table-driven tests for these exact message types: `terminal.open`, `terminal.opened`, `terminal.input`, `terminal.output`, `terminal.size`, `terminal.bindings`, `terminal.action`, `terminal.action_result`, `terminal.close`, and `terminal.closed`.
- [ ] Specify `terminal_id: UUID` on every terminal payload; `stream_id: UUID` and monotonically increasing `seq >= 1` on output; strict Base64 for input/output; and a decoded data limit of 65,536 bytes per payload.
- [ ] Model terminal actions as a closed literal set: `split_left_right`, `split_top_bottom`, `new_window`, `select_left`, `select_right`, `select_up`, `select_down`, `toggle_zoom`, `copy_mode`, and `close_pane`. Include stable `target_pane_id` where the action requires a Pane target.
- [ ] Model close reasons as a closed set including `client_closed`, `replaced`, `grace_expired`, `stream_gap`, `instance_offline`, and `internal_error`.
- [ ] Add browser-facing DTOs for login/logout session responses, Computer summaries, Computer rename, Term rename, dashboard metrics, enrollment metadata, and terminal control frames. Do not expose terminal byte bodies through HTTP DTOs.
- [ ] Run `uv run --package termflow-protocol pytest packages/protocol/tests -q`; expected: all protocol tests pass.
- [ ] Run `uv run ruff check packages/protocol && uv run mypy packages/protocol/src`; expected: both exit 0.
- [ ] Commit with `git commit -m "feat(protocol): define full terminal and dashboard contracts"`.

## Task 2: Make enrollment consumption atomic and persist Computer metadata

**Files:**

- Modify: `apps/control-plane/src/termflow_control_plane/persistence/models.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/database.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/enrollment.py`
- Modify: `apps/node/src/termflow_node/control_plane_client.py`
- Modify: `apps/node/src/termflow_node/cli.py`
- Modify: `apps/node/src/termflow_node/__init__.py`
- Modify: `apps/control-plane/tests/test_repositories.py`
- Modify: `apps/control-plane/tests/test_enrollment_api.py`
- Modify: `apps/node/tests/test_login.py`

- [ ] Add a concurrent repository test in which two transactions consume the same valid enrollment token and exactly one succeeds.
- [ ] Replace select-then-update consumption with one conditional `UPDATE` constrained by token hash, `used_at IS NULL`, and unexpired timestamp. Treat an affected-row count other than one as an invalid/used token.
- [ ] Extend `Installation` with `hostname`, editable `display_name`, `platform`, `client_version`, and `last_seen_at`; extend `Instance` with `last_seen_at`. All new columns must be nullable or have safe defaults for an existing V1 SQLite database.
- [ ] Add an idempotent startup migration in `database.py` that inspects SQLite columns and issues only explicit `ALTER TABLE ... ADD COLUMN` statements for missing V2 columns before normal repository use. Test both a fresh database and a handcrafted V1 schema.
- [ ] Send `socket.gethostname()`, `platform.system()`, and `termflow_node.__version__` during `termflow login`. Initialize `display_name` from hostname and never log the enrollment or installation token.
- [ ] Update installation `last_seen_at` during successful Instance registration and authenticated Bridge hello/heartbeat, without creating a per-heartbeat audit row.
- [ ] Run `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_repositories.py apps/control-plane/tests/test_enrollment_api.py -q`; expected: all pass, including the concurrency and migration tests.
- [ ] Run `uv run --package termflow-node pytest apps/node/tests/test_login.py -q`; expected: all pass.
- [ ] Commit with `git commit -m "feat(control-plane): harden enrollment and computer identity"`.

## Task 3: Add browser session authentication and WebSocket Origin policy

**Files:**

- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Add: `apps/control-plane/src/termflow_control_plane/auth/sessions.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/__init__.py`
- Add: `apps/control-plane/src/termflow_control_plane/api/sessions.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/dependencies.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Add: `apps/control-plane/tests/test_browser_sessions.py`
- Modify: `apps/control-plane/tests/test_config.py`
- Modify: `apps/control-plane/tests/test_privacy.py`

- [ ] Add failing tests for `POST /api/v1/session` with the admin token, `DELETE /api/v1/session`, expired sessions, invalid tokens, and access to an existing admin endpoint through the cookie.
- [ ] Implement a process-local random session store with 8-hour expiry. Store only a SHA-256 digest of the random session secret, cap the number of live sessions, prune expired entries, and invalidate on logout.
- [ ] Set an `HttpOnly`, `SameSite=Strict`, path `/` cookie. Use `__Host-termflow_session` and `Secure` when `public_base_url` is HTTPS; allow a non-prefixed non-Secure cookie only for configured loopback development.
- [ ] Preserve `Authorization: Bearer` authentication for curl and native clients. API dependencies accept either a valid bearer token or browser session; never accept the admin token in query parameters, local storage, or WebSocket URLs.
- [ ] Add `trusted_web_origins` settings. For browser WebSockets require an exact `Origin` match before `accept()`; native clients without Origin continue to use Bearer authentication.
- [ ] Add privacy tests proving request logs, exception text, response bodies, and session-store representations do not include admin, enrollment, installation, or session secrets.
- [ ] Run `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_browser_sessions.py apps/control-plane/tests/test_config.py apps/control-plane/tests/test_privacy.py -q`; expected: all pass.
- [ ] Commit with `git commit -m "feat(control-plane): add secure browser sessions"`.

## Task 4: Add dashboard, Computer management, and Term rename APIs

**Files:**

- Add: `apps/control-plane/src/termflow_control_plane/api/dashboard.py`
- Add: `apps/control-plane/src/termflow_control_plane/api/computers.py`
- Add: `apps/control-plane/src/termflow_control_plane/api/terms.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/__init__.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Modify: `apps/control-plane/src/termflow_control_plane/connections/registry.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/bridge.py`
- Add: `apps/control-plane/tests/test_dashboard_api.py`
- Add: `apps/control-plane/tests/test_computers_api.py`
- Add: `apps/control-plane/tests/test_terms_api.py`

- [ ] Define and test `GET /api/v1/dashboard`, `GET /api/v1/computers`, `PATCH /api/v1/computers/{installation_id}`, and `PATCH /api/v1/terms/{instance_id}`.
- [ ] Return dashboard metrics for online Terms, active Panes from live topology, 24-hour interaction audit count, and total Computers. Group Term rows under Computer DTOs; retain last-known Term names for offline rows.
- [ ] Validate editable Computer and Term names as 1-128 Unicode characters without C0/C1 control characters. Pass names as values, never interpolate them into SQL or shell text.
- [ ] Implement Term rename as a routed semantic request to online A. Persist the new name only after A returns success; reject offline rename with the existing structured offline error and do not queue it.
- [ ] When a topology snapshot reports a tmux session name changed locally, update the last-known Instance name and A installation/Instance `last_seen_at`.
- [ ] Audit interaction metadata only: operation, IDs, byte count where applicable, result, error code, timestamp. Add a test that names and terminal content do not enter audit rows.
- [ ] Run `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_dashboard_api.py apps/control-plane/tests/test_computers_api.py apps/control-plane/tests/test_terms_api.py -q`; expected: all pass.
- [ ] Commit with `git commit -m "feat(control-plane): add computer and term management APIs"`.

## Task 5: Use real tmux session identity and enrich topology

**Files:**

- Modify: `apps/node/src/termflow_node/instances/models.py`
- Modify: `apps/node/src/termflow_node/instances/store.py`
- Modify: `apps/node/src/termflow_node/instances/manager.py`
- Modify: `apps/node/src/termflow_node/tmux/runner.py`
- Modify: `apps/node/src/termflow_node/tmux/topology.py`
- Modify: `apps/node/src/termflow_node/tmux/control_parser.py`
- Modify: `apps/node/src/termflow_node/cli.py`
- Modify: `apps/node/tests/test_instance_store.py`
- Modify: `apps/node/tests/test_tmux_runner.py`
- Modify: `apps/node/tests/test_topology.py`
- Add: `apps/node/tests/integration/test_session_identity.py`

- [ ] Write migration tests for a V1 local Instance with `session_name="main"`, a locally renamed legacy session, and an already migrated record.
- [ ] Store a schema version, stable tmux `session_id` such as `$0`, and latest `session_name`. Make `termflow list`, `attach`, Bridge startup, and kill target the stable ID.
- [ ] On new Instance creation, use the requested Term name as the actual tmux session name. Record the real `session_id` from tmux immediately after creation.
- [ ] For legacy records, inspect the only managed session. If it remains `main`, rename it once to the old Instance display name; if it was already renamed locally, preserve that tmux name. Persist the stable ID and resolved name atomically.
- [ ] Add `TmuxRunner.rename_session`, `session_identity`, `list_clients`, and raw format helpers implemented with argv arrays. Never use `shell=True` or construct a shell command string.
- [ ] Query `pane_left`, `pane_top`, and `pane_current_command` along with the existing topology fields. Treat all names and commands as raw display data without Agent classification.
- [ ] Run `uv run --package termflow-node pytest apps/node/tests/test_instance_store.py apps/node/tests/test_tmux_runner.py apps/node/tests/test_topology.py apps/node/tests/integration/test_session_identity.py -q`; expected: all pass with real tmux tests enabled where tmux exists.
- [ ] Commit with `git commit -m "feat(node): align term identity with tmux sessions"`.

## Task 6: Implement the A-side remote tmux client PTY

**Files:**

- Add: `apps/node/src/termflow_node/tmux/remote_client.py`
- Add: `apps/node/src/termflow_node/tmux/client_size.py`
- Modify: `apps/node/src/termflow_node/tmux/__init__.py`
- Add: `apps/node/tests/test_remote_client.py`
- Add: `apps/node/tests/test_client_size.py`
- Add: `apps/node/tests/integration/test_remote_tmux_client.py`

- [ ] Write unit tests around an injected PTY/process adapter for startup, output sequencing, binary input, graceful detach, abnormal exit, and `TIOCSWINSZ` updates.
- [ ] Spawn `tmux -S SOCKET attach-session -t SESSION_ID` with the PTY slave connected to stdin/stdout/stderr and a TermFlow-owned environment marker identifying the proxy client. Close inherited slave descriptors in the parent and reliably reap the child.
- [ ] Read PTY master bytes without UTF-8 decoding, split output into at most 65,536-byte chunks, and assign one `stream_id` plus increasing sequence numbers.
- [ ] Implement a 1 MiB byte-bounded ring buffer with exact replay-after-sequence behavior. Detect overwritten ranges rather than silently returning a partial transcript.
- [ ] Determine rows/cols from the most recently active non-TermFlow local tmux client. Fall back to last observed A size, then tmux creation size, then 80x24. A resize applies only to the proxy PTY and never originates from C.
- [ ] Ensure closing or replacing the proxy client cannot run `kill-server`, `kill-session`, or terminate Pane processes.
- [ ] Real-tmux integration test: create a private server, attach the proxy, observe the tmux status screen, send a Prefix action through raw bytes, detach the proxy, and prove the server and Pane remain alive.
- [ ] Run `uv run --package termflow-node pytest apps/node/tests/test_remote_client.py apps/node/tests/test_client_size.py apps/node/tests/integration/test_remote_tmux_client.py -q`; expected: all pass.
- [ ] Commit with `git commit -m "feat(node): attach remote tmux clients through pty"`.

## Task 7: Add semantic tmux actions and binding discovery on A

**Files:**

- Add: `apps/node/src/termflow_node/tmux/actions.py`
- Add: `apps/node/src/termflow_node/tmux/bindings.py`
- Add: `apps/node/tests/test_tmux_actions.py`
- Add: `apps/node/tests/test_tmux_bindings.py`
- Add: `apps/node/tests/integration/test_tmux_actions.py`

- [ ] Map every protocol action to direct tmux argv: `split-window -h`, `split-window -v`, `new-window`, directional `select-pane`, `resize-pane -Z`, `copy-mode`, and `kill-pane`.
- [ ] Require an existing stable Pane ID for Pane-scoped actions and reject unknown/dead Pane targets before execution. Keep close confirmation a C responsibility but require an explicit `confirmed=true` field at the A-B boundary for `close_pane`.
- [ ] Query the active `prefix`/`prefix2` options and relevant `list-keys` bindings. Return display-only tooltip strings with sensible tmux-default fallback if a binding is unbound.
- [ ] Rename Term through `rename-session -t SESSION_ID NEW_NAME`; after success, update the local Instance store and emit a fresh topology snapshot.
- [ ] Integration tests prove splits and new windows alter real topology, navigation changes the active Pane, zoom toggles, copy mode enters, rename stays attachable by stable session ID, and confirmed close removes only the selected Pane.
- [ ] Run `uv run --package termflow-node pytest apps/node/tests/test_tmux_actions.py apps/node/tests/test_tmux_bindings.py apps/node/tests/integration/test_tmux_actions.py -q`; expected: all pass.
- [ ] Commit with `git commit -m "feat(node): expose semantic tmux actions"`.

## Task 8: Multiplex terminal sessions through the existing A-B Bridge

**Files:**

- Add: `apps/node/src/termflow_node/bridge/terminal_manager.py`
- Modify: `apps/node/src/termflow_node/bridge/runtime.py`
- Modify: `apps/node/src/termflow_node/bridge/transport.py`
- Modify: `apps/node/src/termflow_node/bridge/__init__.py`
- Add: `apps/node/tests/test_terminal_manager.py`
- Modify: `apps/node/tests/test_bridge_runtime.py`
- Add: `apps/node/tests/integration/test_terminal_bridge.py`

- [ ] Write failing runtime tests for open, input, output, size, bindings, action, close, replacement, reconnect within 30 seconds, replay gap, and grace expiry.
- [ ] Keep Pane control-mode streaming and the new PTY stream as independent producers on one serialized Bridge send queue; no two coroutines may call the WebSocket transport send concurrently.
- [ ] Allow one input-capable remote client per Term. A new open replaces the current owner and emits `terminal.closed(reason="replaced")` for the old terminal ID.
- [ ] On Bridge loss, retain the PTY manager for 30 seconds and buffer at most 1 MiB. Resume only when terminal ID, stream ID, and last sequence are consistent; otherwise close it and let a fresh tmux client produce a full redraw.
- [ ] Treat malformed Base64, oversized chunks, stale terminal IDs, invalid targets, and unsupported actions as structured `terminal.action_result` or `terminal.closed` failures without crashing the Bridge reconnect loop.
- [ ] Run `uv run --package termflow-node pytest apps/node/tests/test_terminal_manager.py apps/node/tests/test_bridge_runtime.py apps/node/tests/integration/test_terminal_bridge.py -q`; expected: all pass.
- [ ] Commit with `git commit -m "feat(node): multiplex full terminals on the bridge"`.

## Task 9: Implement B-side terminal WebSocket routing

**Files:**

- Add: `apps/control-plane/src/termflow_control_plane/connections/terminal_hub.py`
- Add: `apps/control-plane/src/termflow_control_plane/routing/terminal_router.py`
- Add: `apps/control-plane/src/termflow_control_plane/api/terminal.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/bridge.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Add: `apps/control-plane/tests/test_terminal_hub.py`
- Add: `apps/control-plane/tests/test_terminal_websocket.py`
- Modify: `apps/control-plane/tests/test_bridge_websocket.py`
- Modify: `apps/control-plane/tests/test_privacy.py`

- [ ] Test `WS /api/v1/terms/{instance_id}/terminal` with a valid browser cookie and Origin, invalid Origin, Bearer-native access, offline Instance, replacement, binary input/output, text control frames, and disconnect.
- [ ] Convert browser binary frames to strict Base64 A-B messages and A-B output back to browser binary frames. Text frames are JSON control envelopes only; reject terminal bytes sent as JSON.
- [ ] Limit a browser binary frame to 65,536 bytes, inbound rate to a configurable bytes/second token bucket, and each direction queue to configured byte/count bounds. Close only the affected terminal on sustained backpressure.
- [ ] Send `terminal.ready`, `terminal.size`, `terminal.binding_snapshot`, `terminal.error`, and `terminal.closed` as documented control events. C never sends a resize message; reject unknown control types.
- [ ] Route one current owner per Term. Replacement is explicit and deterministic; a B process restart creates a new terminal client and does not claim replay it cannot prove.
- [ ] Do not write terminal frame content to database, audit, application logs, exception messages, or tracing fields. Audit only open/close/action metadata and input byte counts.
- [ ] Run `uv run --package termflow-control-plane pytest apps/control-plane/tests/test_terminal_hub.py apps/control-plane/tests/test_terminal_websocket.py apps/control-plane/tests/test_bridge_websocket.py apps/control-plane/tests/test_privacy.py -q`; expected: all pass.
- [ ] Commit with `git commit -m "feat(control-plane): route browser terminal websockets"`.

## Task 10: Verify the complete A+B boundary

**Files:**

- Modify: only the failing source or test files previously listed in Tasks 1-9, and only when the verification output identifies the defect.

- [ ] Run `uv run --all-packages pytest packages/protocol/tests apps/control-plane/tests apps/node/tests -q`; expected: no failures and no unexpected skips.
- [ ] Run `uv run --all-packages ruff check packages/protocol apps/control-plane apps/node`; expected: exit 0.
- [ ] Run `uv run --all-packages mypy packages/protocol/src apps/control-plane/src apps/node/src`; expected: exit 0.
- [ ] Run `rg -n "codex|claude" packages/protocol apps/control-plane apps/node -g '!**/tests/**'`; expected: no production Agent-brand classification or hardcoded command detection.
- [ ] Run `rg -n "terminal.*(data|text)|data_base64" apps/control-plane/src/termflow_control_plane/persistence`; expected: no terminal-content persistence columns or repository writes.
- [ ] Run `git status --short`; expected: clean worktree after the final commit.

## Requirement coverage

| Requirement | Task |
| --- | --- |
| Browser and native authentication without URL secrets | 3 |
| Atomic one-time Computer registration | 2 |
| Computer metadata and management | 2, 4 |
| Term name equals real tmux session name | 4, 5, 7 |
| Raw runtime command display without Agent hardcoding | 5 |
| A-authoritative dimensions | 5, 6, 9 |
| Real tmux client PTY and full tmux UI | 6, 8, 9 |
| Semantic auxiliary actions and binding tooltips | 7, 8, 9 |
| One writer, replacement, 30-second/1-MiB reconnect | 6, 8, 9 |
| B stores no terminal content | 4, 9, 10 |
| Existing Pane HTTP/API remains compatible | 1, 8, 10 |
