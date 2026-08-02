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
