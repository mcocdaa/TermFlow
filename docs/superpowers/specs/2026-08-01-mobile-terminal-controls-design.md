# TermFlow Mobile Terminal Controls Design

**Date:** 2026-08-01

**Status:** Approved by the user

## Goal

Make Web C's authenticated header and Term detail usable on phones without changing A-side tmux dimensions, the WebSocket protocol, or B's control-plane API.

The phone layout must show logout as an SVG icon while retaining an accessible `退出登录` name; hide the Computer name in the Term titlebar; render Display, Pane focus, tmux actions, and touch-control lock as four always-visible SVG icon buttons without labels or chevrons; replace the bottom floating tmux shortcut drawer with the titlebar tmux icon menu; distinguish local canvas navigation from remote tmux mouse control; and prevent the bottom modifier-key row from being dragged or scrolled away to reveal a black terminal underlay.

Desktop wording, menus, keyboard access, and existing semantic tmux actions remain unchanged.

## Existing Boundary and Root Causes

`TerminalView.vue` already owns mutually-exclusive Display, Pane, and tmux menu state. `TerminalCanvas.vue` owns local pointer presentation and `TerminalAdapter` owns xterm, including its mouse-reporting path to the existing WebSocket input stream. The mobile tmux drawer duplicates that titlebar action path.

The current mobile stylesheet always gives `.terminal-frame` `touch-action: none`, uses a fixed bottom modifier-key bar, and makes a fixed floating tmux drawer available. The fixed controls overlay the terminal grid rather than reserving a layout row. The current terminal scale is a CSS `scale(...)` transform, so xterm calculates mouse coordinates from a different geometry than the one the user sees. That coordinate error must be eliminated before touch can safely synthesize remote mouse interaction in fit, 50%, 75%, Pane-focus, or client-zoom presentations.

## Selected Architecture

### Titlebar and Header

`App.vue` keeps the existing logout handler and desktop text. A mobile-only Lucide logout glyph replaces the visible text and retains `aria-label="退出登录"` and a tooltip.

`TerminalTitlebar.vue`, `DisplayMenu.vue`, `PaneFocusMenu.vue`, and `TmuxActionMenu.vue` keep their current controlled open-state contract. At a coarse-pointer/mobile breakpoint, the Display, Pane, and tmux triggers become icon-only controls; their text and chevrons are visually hidden but their accessible names stay explicit. The titlebar adds a fourth icon-only toggle, named `锁定画布` in accessible text, with `aria-pressed` and a visible active state. The user chose this four-icon layout over a single overflow menu.

The Computer label is visually hidden at the same breakpoint. Term name, back button, and the four controls remain in one row; names truncate rather than forcing a second row. The existing `openMenu` identifier keeps the three menus mutually exclusive. The tmux icon opens the existing action menu, so the mobile floating `快捷操作` trigger and its drawer are deleted.

### Touch Modes

`TerminalView.vue` owns `touchControlLocked`, initialized to `false` for every new Term detail mount. It is not stored in orientation view state, so portrait/landscape changes preserve the current state. Reconnects preserve it; leaving and re-entering the Term resets it to the safe default.

`TerminalCanvas.vue` receives this boolean and delegates raw touch recognition to a dedicated gesture arbiter. `usePointerViewport` remains responsible only for presentation scale and pan bounds.

| State | One finger | Two fingers | Terminal input |
|---|---|---|---|
| Unlocked (default) | Local canvas pan | Local pinch zoom | Never emitted |
| Locked | Remote left click / drag | Ignored for remote mouse control | xterm emits existing mouse-report bytes |
| Long press while locked | xterm text selection | N/A | No mouse bytes |

For locked touch, the arbiter translates a tap or drag to synthetic standard `mousedown`, `mousemove`, and `mouseup` events delivered to the xterm element and its document-level drag handlers. xterm already calculates SGR/X10-style tmux mouse reports and forwards them through its `onData` / `onBinary` path; Web C does not encode tmux protocol itself.

Touch down starts a short long-press timer (about 500ms). Motion beyond a small slop before the timer becomes a remote drag. A completed press before the timer becomes a remote click. When the timer wins, the arbiter sends force-selection mouse events rather than remote mouse events, so xterm renders a normal terminal text selection and the browser's existing copy path can be used. This is the relevant phone text-selection behavior for xterm's rendered terminal; it is not generic DOM text selection. Long press, selection move, cancellation, unmount, and a lock-state change clear timers and any synthetic button state.

### Presentation Geometry Prerequisite

The existing scaled-terminal-mouse-fidelity design is a required part of this work. `TerminalAdapter` gains a public visual-scale operation that derives `fontSize` from a fixed 100% base. `TerminalCanvas` keeps baseline and rendered cell metrics separately and sizes the grid from the rendered metrics. The grid keeps only translation; no ancestor of `.xterm-screen` gets a CSS scale transform.

Therefore synthetic mouse events use xterm's real on-screen cell geometry in every display mode. Presentation remains client-only: it never sends a resize message or changes the authoritative A-side rows/columns.

### Bottom Control Row

At the mobile/coarse-pointer breakpoint, the terminal view uses three explicit rows: titlebar, terminal frame, and `MobileKeyBar`. `MobileKeyBar` is no longer fixed and `.terminal-frame` no longer extends behind it. The key row owns safe-area bottom padding and stays in view while only the terminal frame presents the grid. This removes the black exposed area during gesture/overflow interactions.

## Component Ownership

- `App.vue`: responsive logout presentation only.
- `TerminalView.vue`: lock state, mutually-exclusive titlebar menus, and routing the lock state to the canvas.
- `TerminalTitlebar.vue`: phone identity compression and the lock icon toggle.
- `DisplayMenu.vue`, `PaneFocusMenu.vue`, `TmuxActionMenu.vue`: accessible icon-only mobile trigger variants; tmux drawer removal.
- `TerminalCanvas.vue`: presentation versus locked-touch dispatch boundary.
- New touch-gesture composable: tap, drag, pinch, long-press selection, pointer cancellation, and timer cleanup.
- `terminalAdapter.ts`: public xterm visual-scale and synthetic mouse dispatch boundary; no protocol implementation.
- `useTerminalSession.ts`: exposes only the adapter operations needed by canvas.
- `app.css` and `terminal-responsive.css`: responsive icon styling, grid-row sizing, safe-area treatment, and mode-specific touch behavior.

## Accessibility and Failure Handling

- Every SVG-only control has an `aria-label`, `title`, visible focus ring, and minimum touch target. Lock state uses `aria-pressed`; menu triggers retain `aria-expanded` and their existing keyboard/Escape behavior.
- When the terminal is disconnected, lock is visibly disabled or cannot inject remote input. Local viewport navigation remains available.
- A second touch, `pointercancel`, lost capture, unmount, or a lock toggle ends the active synthetic gesture without a stale pressed button.
- Long-press selection never creates a tmux mouse frame. Unlocked touch never invokes the adapter's mouse dispatcher.
- All actions continue to use server-reported semantic tmux actions; no shortcut keys or A-side topology are hard-coded.

## Verification

Test-first coverage must include:

1. mobile logout icon and accessible name, hidden Computer label, four icon-only titlebar triggers, and absence of the mobile tmux drawer;
2. lock default/reset/orientation behavior and `aria-pressed` state;
3. unlocked single-pan and pinch with no synthetic mouse or WebSocket input;
4. locked tap and drag dispatching the expected xterm mouse lifecycle;
5. long press selecting terminal text with no remote mouse lifecycle;
6. cancellation, disconnect, and unmount cleanup; and
7. mobile grid-row and safe-area CSS contracts that exclude a fixed overlaid modifier bar or exposed terminal underlay.

Fresh Web unit tests, typecheck, and production build are required. A disposable real-browser mobile acceptance run must cover portrait and landscape, each display mode, actual tmux Pane targeting by tap/drag, local pan/pinch while unlocked, long-press selection, titlebar menus, and the absence of a bottom black gap. It must use only the repository's isolated fixture and leave current services, Terms, databases, ports, and Docker containers untouched.

## Out of Scope

- Native Windows/ConPTY work, A/B service changes, tmux topology changes, and terminal resizing from Web C.
- Persisting the lock state across Term detail remounts or browser sessions.
- Replacing xterm's selection/copy implementation with a generic mobile DOM selection system.
