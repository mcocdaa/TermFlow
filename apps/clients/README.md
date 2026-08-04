# TermFlow clients

`web/` is the thin browser composition root for C. Shared DTOs, transport-neutral
HTTP/terminal behavior, Vue pages, components and styles live in the root
`packages/client-*` workspaces. Web owns only browser HTTP/WebSocket/storage
adapters, browser history and the Vue mount point.

All clients use only B's public `/api/v1` HTTP and WebSocket contracts. They do
not import control-plane implementation code or read its database.

Web C signs in by exchanging the administrator token for a short-lived HttpOnly
browser session. The Tauri desktop/mobile client does not embed that token in its
own login form: it asks for the relay server URL, opens the system browser for the
OAuth/PKCE authorization page, and receives the one-time callback at
`termflow://auth/callback`. The browser user approves the requested scopes with the
administrator token and, when enabled, one current TOTP code. The native client keeps
the short-lived access token in memory and stores the refresh token plus its device
signing key in the platform keyring. This flow
is the same for Windows, Linux, macOS, Android and iOS; the native platform only
changes the secure-storage and callback adapter.

Install and verify from the repository root so every client uses the single root
lock file and the pinned Node 22 toolchain:

```bash
nvm use
npm ci
npm run contracts:check
npm run test:run
npm run typecheck
npm run build:web
```

The repository pins Node 22.23.2 (`.nvmrc`) and npm 10.9.8. The single Tauri 2
project under `tauri/` shares the client packages, but native builds still require
the real target toolchain:

- Linux: Rust stable plus WebKitGTK 4.1 and the documented system libraries.
- Windows: Rust stable MSVC, Microsoft C++ Build Tools and WebView2.
- macOS: Rust stable and Xcode Command Line Tools; desktop compilation does not
  provide an iOS build.
- Android: Android Studio/SDK, NDK, Java and Android Rust targets; the generated
  `src-tauri/gen/android` project must exist.
- iOS: a macOS host with full Xcode, the iOS Rust targets and the generated
  `src-tauri/gen/apple` project. Linux and Windows cannot compile iOS.

`scripts/verify-tauri.sh` runs Rust fmt/clippy/test/check and an unsigned desktop
`--no-bundle` compile on the current host. A successful host build不能证明跨平台安装包已经构建；
CI keeps Linux, Windows, macOS, Android and iOS compile evidence separate, and
code signing/publication remains a protected release concern.

For downloadable packages, use the reusable workflows documented in
[`docs/github-actions.md`](../../docs/github-actions.md). WSL can run the shared
TypeScript tests and Linux checks, but it cannot provide evidence for a native
Windows installer or an iOS device build.

Native diagnostics are written by the Tauri process to the platform application
log directory as `termflow-client.log` (10 MiB rotation, five backups). Web C
intentionally writes no files. Do not share credentials or terminal contents
when collecting client diagnostics.
