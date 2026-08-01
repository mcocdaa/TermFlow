# TermFlow clients

`web/` is the independent Vue Web C. It uses only the public, relative `/api/v1`
HTTP endpoints and same-origin WebSocket endpoints exposed by the control plane.
It does not import control-plane implementation code or read its database.

Development commands run from `apps/clients/web`:

```bash
npm ci
npm run test:run
npm run typecheck
npm run build
```
