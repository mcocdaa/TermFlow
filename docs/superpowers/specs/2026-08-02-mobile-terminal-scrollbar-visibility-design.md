# Mobile Terminal Scrollbar Visibility Design

**Date:** 2026-08-02

**Status:** Approved in conversation

## Goal

Hide the two native horizontal scroll-position indicators visible on the mobile Term detail page while preserving all existing touch navigation:

1. the scrollbar rendered by the terminal canvas container; and
2. the scrollbar rendered below the mobile modifier-key row.

The change applies only at the existing mobile/coarse-pointer breakpoint. Desktop scrollbars and scrollbars inside titlebar menus remain unchanged.

## Root Cause

`.terminal-frame` uses `overflow: auto` so oversized terminal content can be navigated, and `.mobile-keybar` uses `overflow-x: auto` so its buttons remain reachable on narrow screens. Mobile browsers therefore render native scrollbar indicators for both containers. These indicators are visual browser affordances; they are not required by the pointer viewport or modifier-key logic.

## Selected Design

Keep the existing overflow and gesture rules unchanged. Inside `terminal-responsive.css`'s mobile/coarse-pointer media query, hide only the scrollbar presentation for `.terminal-frame` and `.mobile-keybar`:

- use `scrollbar-width: none` for Firefox;
- use scoped `::-webkit-scrollbar { display: none; }` rules for Chromium and Safari.

Do not set `overflow: hidden` as the general mobile behavior: unlocked terminal overflow and keybar horizontal scrolling must remain available. Do not apply a global scrollbar rule because titlebar menus may still need a visible scroll affordance.

The bottom-center and bottom-right handles shown by a desktop browser's mobile-device emulator are emulator controls, not TermFlow elements, and are outside this change.

## Component Boundaries

- `terminal-responsive.css` owns the mobile-only visual suppression.
- `TerminalCanvas.vue` and `usePointerViewport.ts` retain the existing canvas pan, pinch, lock, and remote-mouse behavior.
- `MobileKeyBar.vue` retains the existing key input and horizontal scroller structure.
- No Control Plane, Node, tmux, WebSocket, or data-model behavior changes.

## Verification

Implementation is test-first. Coverage must prove:

1. the mobile CSS contract contains both Firefox and WebKit scrollbar suppression for exactly the terminal frame and keybar;
2. the mobile keybar still has horizontal overflow and `touch-action: pan-x`;
3. a real mobile browser can still change keybar `scrollLeft` through a horizontal touch drag;
4. existing mobile terminal pan, pinch zoom, viewport lock, and page-containment tests still pass; and
5. the client UI test suite, typecheck, and production build remain green.

## Out of Scope

- Removing or changing desktop scrollbars.
- Hiding scrollbar indicators in titlebar menus or dialogs.
- Changing canvas dimensions, display modes, viewport-lock semantics, or keybar contents.
- Styling browser developer-tool or device-emulator resize handles.
