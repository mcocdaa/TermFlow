# TermFlow Scaled Terminal Mouse Fidelity Design

**Date:** 2026-08-01

**Status:** Approved from the user's explicit requirement that every Web display mode behave like the existing 100% mode, plus the standing authorization to complete the documented plan without further questions.

## Problem

The Web terminal currently renders the authoritative A-side grid with xterm.js and then applies a CSS `scale(...)` transform to `.terminal-grid` for 50%, 75%, fit, mobile zoom, and Pane focus. xterm.js calculates mouse reports and selection coordinates from viewport event pixels divided by its own untransformed cell dimensions. A transformed terminal therefore displays at one coordinate scale while xterm receives events in another.

This explains the observed behavior as one root cause:

- 100% works because the CSS scale is exactly `1`;
- 50% and fit report incorrect tmux mouse coordinates;
- selection and double-click word selection miss because they use the same xterm geometry;
- wheel handling in fit can feel stuck because the browser viewport and tmux compete around a visually transformed target.

## Product Contract

1. Every display mode must preserve the mouse behavior of the current 100% mode.
2. When tmux mouse reporting is active, clicks, drags, releases, motion, and wheel events over terminal content belong to tmux through xterm.js.
3. When tmux mouse reporting is inactive, xterm's native selection and scrollback behavior remains available. The same modifier-assisted selection behavior used by a normal xterm/tmux client remains available when tmux mouse reporting is active.
4. Hidden parts of an oversized Web presentation are reached through the outer terminal viewport's native horizontal and vertical scrollbars. Dragging those scrollbar thumbs does not generate terminal input.
5. Touch pan and pinch remain client-only presentation controls. Mouse events are not repurposed for TermFlow pan or zoom.
6. A remains authoritative for rows and columns. Web presentation changes never send a terminal resize command and never resize the A-side tmux client.

## Considered Approaches

### 1. Rewrite DOM mouse coordinates before xterm receives them

This preserves the existing CSS transform, but it must correctly rewrite element listeners and document-level drag/release listeners for mouse reporting and selection. It is fragile around wheel events, double-click selection, pointer capture, nested translation, and browser differences.

### 2. Patch xterm's private mouse service

This directly changes the coordinate divisor, but depends on unsupported private fields. A routine xterm upgrade could silently break input.

### 3. Render xterm at the requested visual cell size

This is the selected approach. xterm's public `fontSize` option changes visual cell geometry without changing rows or columns. Because the DOM is no longer geometrically scaled, xterm computes mouse reports and selection from the same coordinate system the user sees.

## Architecture

### Stable 100% baseline

The xterm adapter owns a constant base font size and tracks the current visual scale. `setVisualScale(scale)` always sets `fontSize` from `baseFontSize * scale`; it never multiplies the current font size. This prevents cumulative drift when moving repeatedly among display modes or changing orientation.

The adapter reports rendered cell metrics after the option update. TerminalCanvas retains 100% baseline metrics separately from current rendered metrics:

- baseline metrics determine the requested display scale;
- rendered metrics determine the actual grid width and height used by the outer viewport.

### Presentation

`displayPresentation` continues to calculate a dimensionless scale from the A grid, the Web viewport, and 100% baseline cell metrics. TerminalCanvas passes the resulting total scale, including client-only Pane focus or touch zoom, to the adapter.

`.terminal-grid` is sized to the actual rendered xterm grid and receives translation only. No ancestor of `.xterm-screen` receives a scale transform.

For fit mode, TerminalCanvas compares the rendered grid with the available frame after the first font update. If browser font rounding exceeds either dimension by more than one CSS pixel, it performs one bounded downward correction. It never grows beyond the originally calculated fit scale and never loops indefinitely.

### Event ownership

xterm remains the only owner of mouse events originating over `.xterm`:

- xterm mouse reporting emits `onData` or `onBinary`, which the existing WebSocket path forwards unchanged to A's remote tmux PTY;
- xterm selection remains native when reporting is disabled or the normal force-selection modifier is used;
- xterm prevents the outer frame from scrolling when a tmux mouse wheel report is active.

TerminalCanvas continues to ignore `pointerType === "mouse"` in its pan and pinch handlers. The outer `.terminal-frame` keeps native overflow scrollbars for presentations larger than the frame. Fit mode remains overflow-free.

## Component Changes

### `terminalAdapter.ts`

- Add `setVisualScale(scale)` to the adapter interface.
- Apply scale through the public xterm `fontSize` option.
- Return current rendered cell metrics after updating the option.
- Keep `onData`, `onBinary`, input enablement, theme, and rows/cols behavior unchanged.

### `useTerminalSession.ts`

- Expose the adapter's visual-scale operation to TerminalCanvas.
- Do not create any socket message or resize side effect for visual changes.

### `TerminalCanvas.vue`

- Maintain distinct baseline and rendered cell metrics.
- Apply total presentation scale through the adapter.
- Size the terminal content from rendered metrics.
- Remove scale from the grid CSS transform; retain translation.
- Perform at most one fit overflow correction per stable geometry update.

### CSS

- Preserve outer overflow and fit-mode no-overflow rules.
- Remove any terminal-screen scale transform dependency.
- Keep the scrollbars as the explicit mouse-accessible mechanism for navigating oversized presentations.

## Failure Handling

- Invalid, zero, or non-finite scales fall back to `1` inside the adapter.
- If rendered cell measurement is temporarily unavailable, TerminalCanvas keeps its last valid metrics and retries on the next Vue render/ResizeObserver update.
- Fit correction is bounded and downward-only, so font rounding cannot create an update loop.
- Disconnect/reconnect, terminal replacement, and server size changes preserve the selected display mode and reapply it to the new authoritative grid.

## Verification

### Unit and component tests

- 50%, 75%, fit, and 100% set xterm visual scale without changing rows/cols.
- Grid CSS contains translation but no scale transform.
- Repeated mode changes derive from the fixed base font size.
- Fit correction is bounded, downward-only, and produces no overflow.
- Presentation changes emit no WebSocket input or resize frame.
- Existing touch pan/pinch and Pane-focus tests remain green.

### Disposable real-browser acceptance

Use the repository's isolated Web E2E runner with a disposable Control Plane, Computer, Term, tmux socket, configuration directories, and loopback port. Enable tmux mouse only for that disposable Term.

For 100%, 50%, and fit:

- click known cells/Panes and verify the real tmux topology changes focus at the expected coordinate;
- wheel over terminal content and verify a mouse report is sent to tmux rather than scrolling the outer fit viewport;
- perform native/force selection and verify the selected word can be copied;
- verify browser and terminal-frame overflow contracts;
- confirm no Web terminal resize command exists and A's authoritative rows/cols remain unchanged.

The existing user service, live Terms, database, Docker container, and ports must remain unchanged. Temporary resources are removed by exact identifier after the run.

