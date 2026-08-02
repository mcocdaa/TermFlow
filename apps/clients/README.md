# TermFlow clients

`web/` is the thin browser composition root for C. Shared DTOs, transport-neutral
HTTP/terminal behavior, Vue pages, components and styles live in the root
`packages/client-*` workspaces. Web owns only browser HTTP/WebSocket/storage
adapters, browser history and the Vue mount point.

All clients use only B's public `/api/v1` HTTP and WebSocket contracts. They do
not import control-plane implementation code or read its database.

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
