# Offline Term Removal and Viewport Lock Design

**Date:** 2026-08-02  
**Status:** Design approved; pending written-spec review  
**Scope:** Control Plane, Node CLI/Bridge, shared client UI/core, and the Web C browser surface

## 1. Problem statement

Three related control-surface problems remain after the first mobile terminal pass:

1. An offline Term remains in the Control Plane indefinitely and Web C has no supported cleanup action.
2. The titlebar lock is visible on desktop, but its state currently affects only touch gestures. Desktop mouse input returns before the lock state is consulted, and the active visual treatment exists only inside the coarse-pointer media query.
3. The mobile modifier-key row is a native horizontal scroller without an explicit touch or overscroll boundary. The terminal shell itself is height-constrained, but the root document is not route-locked, so dragging the key row can move or rubber-band the page and expose the page background.

The product model must keep four different actions separate:

- deleting a remote Term registration;
- destroying a local tmux Session;
- moving or scaling the terminal viewport;
- scrolling or rubber-banding the outer web page.

## 2. Goals

- Let an administrator permanently remove an offline Term from the Control Plane through a trash SVG action in the shared client UI surfaced by Web C.
- Revoke the removed Term's old remote credential without deleting its local tmux Session.
- Stop a remotely removed Bridge from retrying an invalid credential forever.
- Require an explicit local `termflow activate <name-or-uuid>` before the same local Term may register again.
- Give the lock button one deterministic cross-device meaning: lock the terminal canvas at its current viewport position.
- Preserve two-dimensional terminal movement for oversized content, including 100% display mode.
- Prevent all outer-page movement on the mobile Term route while retaining horizontal key-row scrolling and terminal-canvas pan/zoom.
- Verify the behavior in component, backend, Node, and real-browser tests, then deploy and smoke-test the actual Docker service.

## 3. Non-goals

- The shared client UI does not kill or delete the local tmux Session.
- This work does not add Computer deletion.
- A removed Term does not reactivate automatically on `attach`, process restart, or network recovery.
- This work does not add retention-based cleanup or hidden/archived rows.
- Desktop mouse-drag panning is not added. Desktop overflow movement continues to use the terminal frame's native horizontal and vertical scrolling.
- Fit mode continues to fit the complete grid and does not gain artificial overflow.

## 4. Chosen approach

### 4.1 Alternatives considered

1. **Complete removal and activation lifecycle — chosen.** The Control Plane deletes the Term and invalidates its credential. The Node records that remote activation is required and stops reconnecting. A local command explicitly re-registers the same UUID.
2. **Delete only on B while the Bridge keeps retrying — rejected.** It is smaller, but creates an unbounded invalid-authentication loop and gives the user no legible local state.
3. **Hide or archive the row — rejected.** It avoids credential invalidation but leaves metadata to accumulate and does not represent a permanent cleanup action.

### 4.2 Product semantics

The remote delete action means “remove this Term's remote registration and control credential.” It does not mean “kill the terminal.” If the local tmux Session still exists, the local user can attach to it at any time. Remote access returns only after explicit activation.

## 5. Offline Term removal

### 5.1 Shared client interaction

An offline `TermRow` renders a trash-only icon button using the existing Lucide SVG set. The button:

- is never rendered for an online Term;
- has an accessible name containing the real Term name;
- uses the destructive color contract without making the entire offline row interactive;
- opens a confirmation dialog and never deletes immediately.

The confirmation dialog displays the Term name and explains all consequences:

- the Control Plane record and remote credential will be permanently removed;
- the local tmux Session will not be deleted;
- recovery requires `termflow activate <name-or-uuid>` on the owning Computer.

The confirm button is disabled while the request is pending. The client does not optimistically remove the row. On success it closes the dialog, restores focus safely, and refreshes the dashboard so the metrics and cards come from one authoritative snapshot. On failure it leaves the card and dialog state intact and shows the mapped API error.

The implementation follows the newly merged shared-client boundary:

- `packages/client-core` adds the transport-neutral Term removal API method;
- `packages/client-contracts` carries the generated HTTP/error contract;
- `packages/client-ui` owns `DashboardView`, `ComputerCard`, `TermRow`, the confirmation dialog, focus handling, and styles;
- `apps/clients/web` continues to provide only browser runtime adapters and must not duplicate this feature.

`DashboardView` owns pending deletion and refresh behavior. `ComputerCard` and `TermRow` only render data and emit the selected Term. A dedicated confirmation component owns focus, pending, and error presentation.

### 5.2 HTTP contract

Add the administrator-only endpoint:

```http
DELETE /api/v1/terms/{instance_id}
```

Success returns `204 No Content`.

Errors use the existing structured envelope:

- `404 instance_not_found` when the Term does not exist;
- `409 instance_online` when it is already online before retirement begins;
- existing authentication and validation errors remain unchanged.

The endpoint reads the current instance, checks the live registry, deletes the persistent instance row, records a metadata-only `term.delete` audit event, and ensures any connection that wins a reconnect race is disconnected. A delete initiated from a genuinely offline dashboard row is authoritative: if the old Bridge races to reconnect during the request, deletion wins and the old credential remains invalid.

The generated client contract is refreshed after the backend route is added. `createTermsApi` exposes the deletion through the injected `ApiClient`; the shared UI never calls `fetch` directly.

Deleting an Instance does not delete its `Installation` or its audit history. A Computer with no remaining Terms continues to appear as an empty Computer.

### 5.3 Persistence and re-registration

`InstanceRepository` gains an exact-ID delete operation. The `instances` row contains the only valid hash for that Term credential, so deleting the row invalidates the old token. `AuditEvent.instance_id` remains metadata without a foreign-key cascade and is retained.

The existing installation-authenticated `POST /api/v1/instances/register` remains the reactivation primitive. After a remote deletion, a manual activation may register the same UUID again. `register_or_rotate` creates a fresh row and issues a fresh Term token. It does not restore the deleted credential.

## 6. Local remote-access state

### 6.1 Metadata model

Local Instance metadata is upgraded to schema version 3 with an explicit field:

```text
remote_access = active | activation_required
```

Version 1 and 2 records migrate in memory to `active`; their next atomic save writes version 3. Schema version 3 continues to require the stable tmux Session ID introduced by version 2.

This field is independent from the local tmux lifecycle. A Term may be locally running while its remote access is `activation_required`.

### 6.2 Detecting remote credential rejection

Transient DNS, TCP, TLS, server, or WebSocket interruptions keep the existing retry/backoff behavior. A definitive Bridge authentication rejection for an already-issued Term credential is different:

1. classify it as remote activation required;
2. atomically save `remote_access=activation_required`, `instance_token=null`, and `bridge_pid=null`;
3. stop the Bridge process instead of retrying;
4. never re-register automatically.

`termflow list` and `termflow status` report this state explicitly. They must not label a merely running Bridge process as a confirmed remote connection.

`termflow attach` still attaches to the live local tmux Session. When `remote_access=activation_required`, attach must not launch a replacement Bridge.

### 6.3 Explicit activation command

Add:

```bash
termflow activate <Term-name-or-UUID>
```

Activation follows the existing exact UUID or unique-name resolution rules. It:

1. loads the current Computer login;
2. verifies that the selected local tmux Session still exists;
3. stops only a stale Bridge process for that exact UUID, if one exists;
4. registers the same UUID through the installation credential and receives a fresh Term token;
5. atomically saves the new token and `remote_access=active`;
6. starts a new Bridge for that Term.

The command is valid only when `remote_access=activation_required`; an already-active Term reports that no activation is needed and does not rotate its credential. If the name is ambiguous, the command requires a UUID. If the Computer is not logged in, tmux no longer exists, registration fails, or the Bridge cannot start, the command exits non-zero and retains `activation_required`. A server row created before a later local startup failure may remain offline; a subsequent activation rotates it safely with the same installation ownership check. If Bridge launch fails after registration, the local rollback clears the newly returned token before reporting failure.

## 7. Unified canvas-lock behavior

Rename the internal state from touch-specific control to viewport locking. The user-facing button remains “锁定画布” / “解除画布锁定,” defaults to off on every new Term detail mount, survives orientation changes within that mount, and resets after leaving and re-entering the Term.

The active border, background, foreground, icon, and `aria-pressed` treatment are shared by desktop and mobile rather than being defined only at the coarse-pointer breakpoint.

### 7.1 Desktop

- In fit mode the grid remains fully fitted and overflow-free.
- In 50%, 75%, 100%, or a focused presentation whose grid exceeds the frame, unlocked mode permits native horizontal and vertical frame scrolling.
- Locking preserves the current scroll offsets, hides/disables local frame scrolling, and keeps the terminal canvas at that position.
- Unlocking restores scrollability without resetting to the top-left corner.
- Keyboard input, xterm text selection, and terminal/tmux mouse reporting remain active in either lock state.

### 7.2 Mobile

- Unlocked: one finger pans the terminal canvas in both axes and two fingers scale it.
- Locked: the canvas position and client zoom do not change; tap/drag routes to remote mouse handling and long press enters local xterm selection.
- The actual rendered terminal dimensions remain the pan boundary. Oversized 100% content can be moved left/right and up/down, but clamping prevents the complete grid from being dragged away from the viewport.
- Locking at an offset preserves that offset. Unlocking resumes movement from the same position.

The lock affects only local viewport movement. It never means “disable the terminal.”

## 8. Mobile page and key-row containment

The shared `packages/client-ui` Term route owns an explicit root-page lock. While the route is mounted, `html`, `body`, and `#app` use a route-scoped class with fixed height, hidden overflow, and disabled overscroll. The class is removed when leaving the route so the dashboard and Computer pages retain normal document scrolling. This behavior belongs to the shared `App`/route lifecycle rather than a Web C-only adapter.

The Term view remains a three-row grid:

1. titlebar;
2. terminal frame;
3. mobile modifier-key row.

The root lock does not set the terminal frame to fit-only or discard overflow. Terminal movement remains inside the frame through the viewport controller.

The mobile key row:

- remains a static third grid row rather than a fixed overlay;
- keeps horizontal scrolling for narrow screens;
- uses `touch-action: pan-x`;
- contains inline overscroll and disables block-axis overscroll;
- paints through the bottom safe-area inset with the panel background;
- never forwards a vertical drag into outer-page motion.

The terminal frame retains its own `touch-action: none` so its gesture arbiter, not the browser page, owns terminal pan, pinch, remote mouse, and long-press selection.

## 9. Error handling

- The shared client maps `instance_online` to “Term 已重新上线，无法删除。”
- A stale `404` closes no data optimistically; the dashboard refresh determines the current view.
- A failed delete leaves the confirmation available for retry or cancellation.
- A Bridge authentication rejection is the only network outcome that transitions local metadata to `activation_required`; transient connectivity errors continue backoff.
- Activation never reuses a deleted Term token.
- Activation reports success only after server registration and local Bridge startup both succeed.
- No error path logs administrator, installation, or Term tokens.

## 10. Verification strategy

### 10.1 Control Plane

- offline delete returns 204 and removes the dashboard row and metric count;
- online delete returns `409 instance_online` without persistence changes;
- unknown UUID returns `404 instance_not_found`;
- the deleted token cannot authenticate a Bridge;
- audit history remains;
- the owning installation can manually register the same UUID and receives a new credential;
- a reconnect race cannot leave a deleted live registry entry.

### 10.2 Node

- definitive authentication rejection stops retry and persists schema-v3 `activation_required`;
- transient failures still retry;
- `list` and `status` expose the remote-access state;
- `attach` works locally without launching Bridge while activation is required;
- activate rejects ambiguous names, missing login, and dead tmux;
- successful activate registers the same UUID, stores a fresh token, marks active, and launches Bridge;
- registration or launch failures remain activation-required and exit non-zero.

### 10.3 Shared client components

- only an offline Term has the trash SVG and accessible name;
- confirmation text states the remote/local boundary and activation command;
- online conflict and generic failures preserve the row;
- success refreshes dashboard metrics;
- lock active styling and `aria-pressed` are identical across device classes.

### 10.4 Real browser

Cover desktop, mobile portrait, and mobile landscape with a real online tmux fixture plus a disposable offline Term.

- delete the disposable offline Term through the dialog and verify it remains absent after reload;
- prove an online Term cannot be deleted;
- in 100% mode create horizontal and vertical overflow;
- unlocked desktop scrolls both axes; locked desktop preserves both offsets and blocks further local scrolling;
- unlocked mobile pans the terminal in both axes and pinches; locked mobile preserves its position and routes remote mouse/selection;
- vertically drag the mobile key row and assert `window.scrollY`, root scroll positions, visual viewport offset, titlebar, frame, and key-row geometry remain fixed;
- at the minimum supported width, horizontally drag the overflowing key row and assert only its `scrollLeft` changes;
- assert the key-row bottom remains inside the visual viewport and no page background is exposed.

## 11. Delivery

Implementation must preserve unrelated worktree changes. Verification consists of focused red/green tests, the complete shared-client and Web adapter suites, workspace typecheck/lint, `npm run build:web`, the complete repository verification script, and isolated real-browser E2E.

After those checks pass, rebuild `termflow-control-plane:local` from the implementation commit, recreate the `127.0.0.1:8765` container while preserving `termflow-data`, verify image identity and health, and repeat a read-only smoke test against the actually deployed UI. Deployment success is distinct from an isolated test pass.
