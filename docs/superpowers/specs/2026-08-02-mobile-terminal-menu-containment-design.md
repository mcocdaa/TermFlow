# Mobile Terminal Menu and Keybar Containment Design

**Date:** 2026-08-02

**Status:** Approved in conversation

## Goal

Correct three independent Term-detail UI problems without changing terminal canvas navigation, viewport locking, display-mode behavior, tmux actions, or the A/B protocol:

1. keep mobile Display and tmux menus inside the visible titlebar/viewport boundary;
2. make Display and tmux trigger highlighting match their real expanded state; and
3. prevent horizontal keybar dragging from exposing the page background.

The obsolete Pane-focus presentation feature is removed independently. Its removal must not replace or modify full-canvas pan, pinch zoom, native desktop scrolling, viewport locking, or orientation-specific viewport snapshots.

## Root Causes

The mobile menus inherit the desktop `.floating-menu` geometry. Each menu is positioned relative to its small icon wrapper and keeps a desktop minimum width, so the Display menu can cross the inline viewport edge on narrow titlebars.

Menu state is already controlled by `TerminalView` through `openMenu` and `aria-expanded`. The visible stale highlight is not stale Vue state: Display returns focus to its trigger after a choice or Escape, and the shared CSS gives `:focus-visible` the same filled treatment as `[aria-expanded='true']`. tmux has the same selector and therefore the same visual defect after closing.

The keybar is both the grid row background and the horizontal scroller. It permits inline overscroll with `contain`, which still permits a local boundary affordance. On affected mobile browsers, dragging past either horizontal edge can visually displace that layer and expose the page background.

Pane focus is a separate, incomplete presentation path. It adds a titlebar menu, `focusedPaneId`, a canvas focus API, and portrait restore behavior without providing a reliable product interaction. None of those concepts is required for ordinary full-canvas pan or zoom.

## Selected Design

### Mobile menus

At the mobile/coarse-pointer breakpoint, menu wrappers become statically positioned so their absolutely positioned panels use the titlebar as the containing block. Display and tmux panels are constrained between the titlebar's inline safe-area insets, use the available width, and scroll internally when their height exceeds the remaining viewport. Desktop positioning and sizing remain unchanged.

`TerminalView.openMenu` remains the only open-menu state. Clicking a trigger toggles it; selecting an item or pressing Escape closes it. The filled active style applies only to `[aria-expanded='true']`. `:focus-visible` retains the global accessible outline but no longer impersonates an expanded menu. This rule applies equally to Display and tmux.

### Mobile keybar

`MobileKeyBar` gains a non-scrolling outer shell that owns grid-row placement, safe-area padding/background coverage, width clipping, and the top border. A width-constrained inner element owns horizontal button scrolling. Inline overscroll uses `none`, not `contain`, so boundary dragging cannot chain to the page or show a local rubber-band gap. Vertical dragging remains unavailable and sends no terminal input.

The outer shell and inner scroller each use `min-width: 0`, `width: 100%`, and `max-width: 100%` at the mobile breakpoint. The Term route's existing fixed root containment remains in place as defense in depth.

### Pane-focus removal

Delete `PaneFocusMenu.vue` and remove the Pane menu branch from `TerminalView`. Remove `focusPane`, `applyPaneFocus`, pending-focus state, `focusedPaneId`, and Pane geometry from the client-only viewport API and orientation snapshots. Portrait startup and orientation restoration use the same full-canvas reset/restore behavior as other presentations.

Do not change:

- unlocked mobile one-finger pan or two-finger pinch zoom;
- locked mobile remote mouse and long-press selection;
- desktop horizontal/vertical overflow scrolling;
- viewport-lock state or semantics;
- Display choices or tmux action behavior.

## Component Boundaries

- `TerminalView.vue`: owns only the mutually exclusive Display/tmux menu state and orientation viewport snapshots.
- `DisplayMenu.vue` and `TmuxActionMenu.vue`: keep controlled open-state contracts and accessible names.
- `MobileKeyBar.vue`: owns key input and the shell/scroller structure, not page or terminal viewport state.
- `TerminalCanvas.vue` and `usePointerViewport.ts`: retain generic full-canvas pan/zoom only.
- Responsive CSS: owns mobile menu geometry and keybar containment; desktop rules remain unchanged.

## Verification

Implementation is test-first. Coverage must prove:

1. Display and tmux buttons expose `aria-expanded=false` and lose filled active styling after trigger close, item selection, and Escape;
2. only one Display/tmux menu can be open at a time;
3. both menu rectangles stay within the visual viewport at 360x800 and 800x360;
4. the keybar shell remains fixed to the viewport width while the inner row scrolls, including drags past both horizontal boundaries;
5. page/root geometry and visual viewport offsets do not move during keybar drags;
6. no Pane-focus button, menu, API, state field, automatic portrait focus, or production reference remains; and
7. full-canvas mobile pan/pinch, lock behavior, desktop overflow scrolling, typecheck, build, and shared-client tests still pass.

Real-browser verification uses the repository's isolated Web fixture. Deployment follows the normal verified Docker build and force-recreate flow without mutating existing Term or tmux data.

## Out of Scope

- Redesigning menus as a bottom sheet or full-screen drawer.
- Changing tmux actions, terminal dimensions, WebSocket input, or Control Plane/Node behavior.
- Changing the meaning or default state of viewport locking.
