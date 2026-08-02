# TermFlow Unified Authentication and Native Clients Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved Control Plane authentication hardening, optional TOTP, Web C security/settings flows, OAuth-style PKCE and DPoP authorization for native clients, and one Tauri 2 client project shared by desktop and mobile.

**Architecture:** B remains the only authentication authority and persists only credential digests, encrypted TOTP material, client registrations, challenges, and audit metadata. `client-contracts` is generated from Python protocol models, `client-core` owns platform-neutral authentication state machines and cryptographic request construction, and `client-ui` owns the shared Vue flows. Web remains a same-origin Cookie client; Tauri supplies system-browser, deep-link, signing, and secure-storage adapters without receiving the bootstrap administrator Token.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, `cryptography`, PyJWT, Vue 3, TypeScript 5.7, Web Crypto, Vitest, Tauri 2, Rust, P-256, Playwright, Docker Compose.

---

## Locked file ownership

```text
packages/protocol/src/termflow_protocol/http.py
    Public request/response models and validation only.

apps/control-plane/src/termflow_control_plane/auth/
    Authentication algorithms, rate limiting, TOTP, DPoP, and orchestration.

apps/control-plane/src/termflow_control_plane/persistence/
    Durable auth state, native client, challenge, and token transactions.

apps/control-plane/src/termflow_control_plane/api/
    HTTP translation, Cookie policy, Origin policy, and dependency wiring.

packages/client-core/src/auth/
    PKCE, authorization state, DPoP claims, token rotation, and ports without DOM/Vue/Tauri.

packages/client-ui/src/views and components/settings/
    Login challenge, settings/security, server QR, client management, and consent UI.

apps/clients/web/src/adapters/
    Same-origin Cookie transport and browser-only QR/download behavior.

apps/clients/tauri/src and src-tauri/
    Thin WebView composition plus native system-browser, deep-link, signing, and vault adapters.
```

The reverse proxy, DNS, TLS termination, mTLS, terminal persistence, A-B Bridge, and A-authoritative terminal dimensions remain out of scope.

### Task 1: Public authentication contracts and deterministic generation

**Files:**
- Modify: `packages/protocol/src/termflow_protocol/http.py`
- Modify: `packages/protocol/src/termflow_protocol/__init__.py`
- Modify: `packages/protocol/tests/test_http_models.py`
- Modify: `scripts/generate-client-contracts/generate.py`
- Modify: `tests/contracts/test_client_contract_generation.py`
- Regenerate: `packages/client-contracts/src/generated.ts`

- [ ] **Step 1: Write failing protocol tests**

Add strict model tests for these contracts and reject extra fields, malformed six-digit codes, non-S256 PKCE, unsafe redirect URIs, unsupported JWK algorithms, empty scopes, and invalid client names:

```python
BrowserSessionChallengeResponse(status="totp_required", challenge_id=uuid4(), expires_at=now)
BrowserSessionTotpRequest(code="123456")
TotpStatusResponse(enabled=False, available=True)
TotpSetupRequest(admin_token=SecretStr("..."))
TotpSetupResponse(setup_id=uuid4(), provisioning_uri="otpauth://...", setup_key="...")
TotpConfirmRequest(code="123456")
TotpDisableRequest(admin_token=SecretStr("..."), code="123456")
OAuthAuthorizationRequest(..., code_challenge_method="S256", dpop_jkt="...")
OAuthAuthorizationDecisionRequest(...)
OAuthTokenRequest(grant_type="authorization_code", ...)
OAuthTokenResponse(token_type="DPoP", ...)
CliTokenRequest(admin_token=SecretStr("..."), totp_code=None)
NativeClientListResponse(clients=[...])
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q packages/protocol/tests/test_http_models.py tests/contracts/test_client_contract_generation.py`

Expected: FAIL because the authentication DTOs and generated TypeScript interfaces do not exist.

- [ ] **Step 3: Implement strict models and exports**

Use `HttpModel(extra="forbid")`, `SecretStr` for submitted credentials, `Field(repr=False)` for returned one-time secrets, `Literal` for fixed status/grant/token values, and validators that only allow:

```text
TOTP: exactly six ASCII digits
PKCE: S256 with a 43-128 character verifier/challenge
DPoP: ES256 public EC JWK with P-256 curve and no private `d`
redirect URI: termflow://auth/callback, claimed HTTPS, or loopback HTTP with an explicit ephemeral port
scope: terminal.read terminal.write computers.read computers.write
```

Add every browser-visible response to `MODELS` in the generator. Never generate `SecretStr` request fields into client response storage types without preserving their sensitive naming.

- [ ] **Step 4: Regenerate and verify GREEN**

Run:

```bash
npm run contracts:generate
.venv/bin/pytest -q packages/protocol/tests/test_http_models.py tests/contracts/test_client_contract_generation.py
npm run contracts:check
```

Expected: contract tests PASS and regeneration leaves no diff.

- [ ] **Step 5: Commit**

```bash
git add packages/protocol scripts/generate-client-contracts packages/client-contracts/src/generated.ts tests/contracts
git commit -m "feat(auth): define unified authentication contracts"
```

### Task 2: Persistent authentication state and encrypted secrets

**Files:**
- Modify: `apps/control-plane/pyproject.toml`
- Modify: `uv.lock`
- Modify: `apps/control-plane/src/termflow_control_plane/config.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/models.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/database.py`
- Modify: `apps/control-plane/src/termflow_control_plane/persistence/repositories.py`
- Create: `apps/control-plane/src/termflow_control_plane/auth/secret_box.py`
- Create: `apps/control-plane/tests/test_auth_persistence.py`
- Modify: `apps/control-plane/tests/test_config.py`
- Modify: `apps/control-plane/tests/test_repositories.py`

- [ ] **Step 1: Write failing persistence and configuration tests**

Cover a fresh database and upgrade of an existing database. Assert:

```text
authentication_state has a singleton epoch starting at 1
TOTP ciphertext, nonce, key version, enabled time, and last accepted counter are persisted
native_clients stores public JWK/thumbprint, metadata, scopes, timestamps, and revocation only
auth_challenges stores digest/encrypted context, expiry, attempts, epoch, and one-time completion
auth_tokens stores only token digest, kind, scope, key thumbprint, expiry, rotation/revocation
no raw access, refresh, CLI, TOTP, authorization-code, or administrator secret is present in SQLite
```

Validate that `TERMFLOW_TOTP_MASTER_KEY`, when present, is unpadded base64url for exactly 32 bytes. Validate administrator Tokens contain at least 32 UTF-8 bytes; deployment documentation generates them from 32 random bytes instead of claiming that arbitrary text length proves entropy. Add settings for challenge/access/refresh/CLI TTLs and authentication budgets with bounded defaults from the approved design.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q apps/control-plane/tests/test_auth_persistence.py apps/control-plane/tests/test_config.py apps/control-plane/tests/test_repositories.py`

Expected: FAIL because auth tables, repositories, settings, and secret box do not exist.

- [ ] **Step 3: Implement schema and transactional repositories**

Create focused repositories:

```python
class AuthStateRepository:
    async def get(self) -> AuthenticationState: ...
    async def enable_totp(self, encrypted: EncryptedSecret, counter: int) -> None: ...
    async def accept_totp_counter(self, counter: int) -> bool: ...
    async def reset_and_increment_epoch(self) -> int: ...

class AuthChallengeRepository:
    async def create(self, kind: str, context_ciphertext: bytes, expires_at: datetime, epoch: int) -> UUID: ...
    async def fail_attempt(self, challenge_id: UUID, maximum: int = 5) -> bool: ...
    async def consume(self, challenge_id: UUID, kind: str, epoch: int) -> bytes | None: ...

class NativeClientRepository: ...
class AuthTokenRepository: ...
```

All consumes and counter updates use one conditional `UPDATE ... RETURNING` transaction. Implement `AesGcmSecretBox` with a random 96-bit nonce and versioned associated data; its `repr` exposes no key or plaintext.

- [ ] **Step 4: Verify GREEN and privacy**

Run:

```bash
.venv/bin/pytest -q apps/control-plane/tests/test_auth_persistence.py apps/control-plane/tests/test_config.py apps/control-plane/tests/test_repositories.py apps/control-plane/tests/test_privacy.py
.venv/bin/ruff check apps/control-plane/src apps/control-plane/tests
.venv/bin/mypy apps/control-plane/src
```

Expected: all tests and static checks PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane pyproject.toml uv.lock
git commit -m "feat(auth): persist encrypted authentication state"
```

### Task 3: Login rate limits, progressive delay, and safe audit

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/auth/rate_limit.py`
- Create: `apps/control-plane/src/termflow_control_plane/auth/audit.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Modify: `apps/control-plane/src/termflow_control_plane/errors.py`
- Create: `apps/control-plane/tests/test_auth_rate_limit.py`
- Modify: `apps/control-plane/tests/test_privacy.py`

- [ ] **Step 1: Write failing deterministic clock tests**

Test a source-keyed limiter with injected monotonic clock:

```text
first five requests are allowed as a burst
capacity recovers at one request per minute
consecutive failures impose 1, 2, 4 ... 300 second delays
Retry-After is stable and rounded up
success clears only the matching source/purpose failure state
global concurrency budget rejects overload without consuming credential attempts
expired state is pruned and capacity is bounded
```

Audit tests must assert events contain operation, generic result, source digest, and time but never IP plaintext, submitted credentials, TOTP codes, DPoP proofs, or token values.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q apps/control-plane/tests/test_auth_rate_limit.py apps/control-plane/tests/test_privacy.py`

Expected: FAIL because limiter and authentication audit service do not exist.

- [ ] **Step 3: Implement limiter and error translation**

Provide `AuthRateLimiter.check(purpose, source)`, `record_failure`, and `record_success`. Source is the direct peer address; proxy headers are deliberately ignored because trusted-proxy configuration belongs to a later deployment design. Raise one structured `rate_limited` error with `Retry-After` and a generic public message. Bound maps by LRU/expiry and enforce a global semaphore around expensive verification. Add a FastAPI validation-error handler that replaces credential-bearing 422 details with a generic structured error rather than echoing submitted Token, TOTP, verifier, proof, or code values.

- [ ] **Step 4: Verify GREEN**

Run the focused tests and `ruff`/`mypy` for the new modules.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/src/termflow_control_plane/auth apps/control-plane/src/termflow_control_plane/app.py apps/control-plane/src/termflow_control_plane/errors.py apps/control-plane/tests
git commit -m "feat(auth): enforce bounded login defenses"
```

### Task 4: TOTP and protected Web session exchange

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/auth/totp.py`
- Create: `apps/control-plane/src/termflow_control_plane/auth/service.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/sessions.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/sessions.py`
- Create: `apps/control-plane/src/termflow_control_plane/api/security.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/dependencies.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Create: `apps/control-plane/tests/test_totp.py`
- Create: `apps/control-plane/tests/test_totp_api.py`
- Modify: `apps/control-plane/tests/test_browser_sessions.py`
- Modify: `apps/control-plane/tests/test_privacy.py`

- [ ] **Step 1: Write failing TOTP algorithm and API tests**

Use RFC 6238 vectors and an injected wall clock. Cover SHA-1, six digits, 30-second steps, current plus one adjacent step, constant-time comparison, concurrent counter replay rejection, setup expiry, five-attempt challenge destruction, and no setup availability without the master key.

API flow expectations:

```text
POST /admin/sessions -> 201 Cookie when TOTP disabled
POST /admin/sessions -> 202 opaque challenge only after correct admin Token when enabled
POST /admin/sessions/{id}/totp -> 201 Cookie for a fresh code
invalid Token and invalid TOTP share the same public error envelope
GET /admin/totp -> enabled/available status using Cookie and exact Origin policy
POST /admin/totp/setups -> QR URI/setup key only after Cookie plus administrator re-auth
POST /admin/totp/setups/{id}/confirm -> enable only after a valid first code
DELETE /admin/totp -> require administrator Token plus current fresh code
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q apps/control-plane/tests/test_totp.py apps/control-plane/tests/test_totp_api.py apps/control-plane/tests/test_browser_sessions.py`

Expected: FAIL on missing TOTP service/routes and challenge response.

- [ ] **Step 3: Implement TOTP and epoch-aware sessions**

Generate 20 random bytes, render uppercase base32 without logging, build `otpauth://totp/TermFlow:<issuer-host>?secret=...&issuer=TermFlow&algorithm=SHA1&digits=6&period=30`, and encrypt pending and enabled secrets with distinct associated-data labels. Store the auth epoch in every process-local Web session; epoch mismatch revokes it. Never put setup material in `repr`, logs, audit details, URL query, or cacheable responses.

- [ ] **Step 4: Verify GREEN and replay behavior**

Run the focused suite twice, including the concurrent replay test, then privacy tests and static checks.

- [ ] **Step 5: Commit**

```bash
git add packages/protocol packages/client-contracts apps/control-plane
git commit -m "feat(auth): add optional TOTP web sessions"
```

### Task 5: Native authorization, PKCE, DPoP, token rotation, and clients

**Files:**
- Create: `apps/control-plane/src/termflow_control_plane/auth/pkce.py`
- Create: `apps/control-plane/src/termflow_control_plane/auth/dpop.py`
- Create: `apps/control-plane/src/termflow_control_plane/auth/oauth.py`
- Create: `apps/control-plane/src/termflow_control_plane/api/oauth.py`
- Create: `apps/control-plane/src/termflow_control_plane/api/clients.py`
- Modify: `apps/control-plane/src/termflow_control_plane/api/dependencies.py`
- Modify: `apps/control-plane/src/termflow_control_plane/auth/sessions.py`
- Modify: `apps/control-plane/src/termflow_control_plane/app.py`
- Create: `apps/control-plane/tests/test_pkce.py`
- Create: `apps/control-plane/tests/test_dpop.py`
- Create: `apps/control-plane/tests/test_oauth_api.py`
- Create: `apps/control-plane/tests/test_clients_api.py`
- Modify: protected HTTP and WebSocket authentication tests

- [ ] **Step 1: Write failing PKCE/DPoP unit tests**

Test RFC 7636 S256 vectors and RFC 9449 proof requirements:

```text
typ=dpop+jwt, alg=ES256, public P-256 JWK only
exact htm and normalized canonical htu
iat within a bounded clock window
unique jti and server nonce
ath equals base64url(SHA-256(access token)) on resource requests
JWK thumbprint matches authorization dpop_jkt and stored token binding
proof, code, access token, and refresh token replay are rejected
```

- [ ] **Step 2: Write failing end-to-end API tests**

Test metadata, authorization preview, administrator/TOTP step-up, explicit allow/deny, safe callback construction, atomic code exchange, ten-minute DPoP access token, rotating refresh token, revocation, client list/rename/delete, and WebSocket DPoP authentication. Assert a copied token with a different key fails. The callback carries only the original `state` plus an opaque public transaction handle; no authorization code/token is placed in a deep-link or loopback URL. The app redeems its pre-existing transaction handle with the PKCE verifier and DPoP proof after the callback signal.

- [ ] **Step 3: Verify RED**

Run the four new test files plus current HTTP/WebSocket auth tests. Expected: missing route/service failures.

- [ ] **Step 4: Implement the authorization service**

The public metadata advertises issuer, `/api/v1/oauth/authorize`, token, revoke, S256, ES256 DPoP, and scopes. Authorization GET validates and redirects the system browser to the shared `/authorize` UI while preserving only public request fields. Authorization POST advances one opaque server transaction through administrator authentication, optional fresh TOTP, consent, and one-time server-side code issuance. Approval redirects with only the transaction handle and the original state; the code remains server-side and is consumed only when the initiating app proves PKCE and DPoP possession at the token endpoint.

Token exchange consumes the server-side code/transaction with one conditional update and verifies PKCE plus the DPoP-bound JWK thumbprint. Store only SHA-256 token digests. Rotate refresh tokens on every use, revoke the entire token family on replay, and update client last-used time. Add a bounded DPoP `jti` replay cache and unpredictable nonce rotation. Canonicalize issuer and `htu` from `TERMFLOW_PUBLIC_BASE_URL`, reject configured base URLs with a non-root path in V1, and never derive signed URLs from forwarded headers.

`require_admin` accepts:

```text
same-origin Web Cookie with exact Origin for mutations
short-lived CLI Bearer
DPoP access token plus valid proof for native HTTP
legacy administrator Bearer only while TOTP is disabled
```

Apply the equivalent policy to terminal/events WebSocket handshakes.

- [ ] **Step 5: Verify GREEN**

Run all new auth tests, current protected API/WebSocket tests, privacy tests, Ruff, and Mypy.

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane packages/protocol packages/client-contracts
git commit -m "feat(auth): authorize DPoP bound native clients"
```

### Task 6: CLI token exchange and the only TOTP recovery path

**Files:**
- Modify: `apps/control-plane/src/termflow_control_plane/api/security.py`
- Modify: `apps/control-plane/src/termflow_control_plane/cli.py`
- Modify: `apps/control-plane/tests/test_cli.py`
- Create: `apps/control-plane/tests/test_cli_tokens.py`
- Modify: `deploy/compose.yaml`
- Modify: `docs/operations.md`

- [ ] **Step 1: Write failing CLI and API tests**

Test short-lived scoped CLI token issuance with administrator Token and optional TOTP. Test interactive `termflow-control auth totp reset`: abort on anything except explicit confirmation, delete TOTP, increment epoch atomically, revoke all Web/native/CLI/challenge credentials, retain native client records/public keys, and write a secret-free audit event. Assert there is no HTTP reset route and no recovery-code model. Because the CLI is a separate process, add a running-B test proving database epoch changes are observed, all in-memory Web sessions are invalidated, and established authenticated WebSockets close without restarting B.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest -q apps/control-plane/tests/test_cli.py apps/control-plane/tests/test_cli_tokens.py`

- [ ] **Step 3: Implement CLI and deployment secret wiring**

Add Typer group `auth totp reset` with an interactive confirmation. B runs a short bounded epoch-watch task and also checks epoch during every authentication/message boundary so a reset from the separate CLI process revokes active credentials promptly. Add optional `TERMFLOW_TOTP_MASTER_KEY` Compose passthrough without a default secret value in source control. Document generation using a local cryptographically secure command and make clear that reverse proxy/TLS/mTLS remain external.

- [ ] **Step 4: Verify GREEN**

Run CLI tests, documentation contract, Compose config, and a disposable-container reset test against a temporary volume.

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane deploy docs
git commit -m "feat(auth): add CLI sessions and local TOTP reset"
```

### Task 7: Platform-neutral client authentication core

**Files:**
- Create: `packages/client-core/src/auth/ports.ts`
- Create: `packages/client-core/src/auth/pkce.ts`
- Create: `packages/client-core/src/auth/dpop.ts`
- Create: `packages/client-core/src/auth/nativeAuthorization.ts`
- Create: `packages/client-core/src/auth/tokenSession.ts`
- Create corresponding `*.test.ts` files
- Create: `packages/client-core/src/api/security.ts`
- Create: `packages/client-core/src/api/oauth.ts`
- Create: `packages/client-core/src/api/clients.ts`
- Modify: `packages/client-core/src/api/session.ts`
- Modify: `packages/client-core/src/http/types.ts`
- Modify: `packages/client-core/src/http/apiClient.ts`
- Modify: `packages/client-core/src/index.ts`

- [ ] **Step 1: Write failing core state-machine tests**

With fake clock/random/key/vault/browser/deep-link transports, cover:

```text
Web login result is authenticated or totp_required without storing administrator Token
PKCE verifier/challenge generation and exact state correlation
native authorization ignores callbacks with wrong issuer/state
authorization code is cleared after one exchange attempt
DPoP proof has fresh jti, exact htm/htu, nonce, and ath
use_dpop_nonce retries once with the supplied nonce
access expiry refreshes early and refresh rotation replaces stored value atomically
invalid_grant/revocation clears all native credentials
no DOM, Vue, localStorage, Tauri import, or raw global administrator Token dependency
```

- [ ] **Step 2: Verify RED**

Run: `npm run test:run --workspace @termflow/client-core`

Expected: missing auth modules and API methods.

- [ ] **Step 3: Implement ports and state machines**

Define narrow ports:

```ts
export interface NativeKeyPort {
  publicJwk(): Promise<PublicEcJwk>
  thumbprint(): Promise<string>
  signJwt(signingInput: Uint8Array): Promise<Uint8Array>
}
export interface CredentialVaultPort {
  load(server: string): Promise<NativeCredentialSet | null>
  replace(server: string, value: NativeCredentialSet | null): Promise<void>
}
export interface AuthorizationBrowserPort {
  open(url: string): Promise<void>
  waitForCallback(state: string, signal?: AbortSignal): Promise<string>
}
```

Keep credential material out of error objects and `repr`-like debug output. Do not add a platform-global singleton.

- [ ] **Step 4: Verify GREEN and boundaries**

Run core tests/typecheck and `tests/test_client_workspace_contract.py` with new forbidden APIs and credential-storage checks.

- [ ] **Step 5: Commit**

```bash
git add packages/client-core tests/test_client_workspace_contract.py
git commit -m "feat(client): add portable native authorization core"
```

### Task 8: Web C login challenge, settings, consent, and client management

**Files:**
- Modify: `packages/client-ui/src/runtime.ts`
- Modify: `packages/client-ui/src/test/fakeRuntime.ts`
- Modify: `packages/client-ui/src/views/LoginView.vue`
- Modify: `packages/client-ui/src/views/LoginView.test.ts`
- Create: `packages/client-ui/src/views/SettingsView.vue`
- Create: `packages/client-ui/src/views/SettingsView.test.ts`
- Create: `packages/client-ui/src/views/NativeAuthorizeView.vue`
- Create: `packages/client-ui/src/views/NativeAuthorizeView.test.ts`
- Create: `packages/client-ui/src/components/settings/TotpPanel.vue`
- Create: `packages/client-ui/src/components/settings/ServerConnectionPanel.vue`
- Create: `packages/client-ui/src/components/settings/AuthorizedClientsPanel.vue`
- Modify: `packages/client-ui/src/router/routes.ts`
- Modify: `packages/client-ui/src/App.vue`
- Modify: `packages/client-ui/src/styles/app.css`
- Modify: `packages/client-ui/src/test/a11y-contract.test.ts`
- Modify: `packages/client-ui/src/test/responsive-contract.test.ts`
- Modify: `apps/clients/web/src/router.ts`
- Modify Web runtime/adapter tests as needed
- Modify: `packages/client-ui/package.json`
- Modify: `package-lock.json`

- [ ] **Step 1: Write failing component and route tests**

Cover desktop and mobile navigation, keyboard/focus behavior, and these flows:

```text
login clears administrator Token on both 201 and 202
202 changes the same form to a six-digit TOTP challenge
generic authentication errors do not reveal which credential failed
settings appearance uses the existing shared ThemePicker
server URL is read-only from metadata and supports copy plus QR
QR contains issuer/public transaction data but no credential
TOTP unavailable message when B lacks a master key
enable shows QR/setup key once and confirms first code
disable/reconfigure requires administrator Token plus fresh TOTP
no recovery-code or remote-reset UI exists
authorized clients list, rename, and revoke with explicit confirmation
native authorization displays issuer, name, platform, key fingerprint, scopes, and allow/deny
```

- [ ] **Step 2: Verify RED**

Run focused client-ui tests. Expected: missing routes/views/components and old login response handling.

- [ ] **Step 3: Implement shared UI**

Add `/settings` and `/authorize`, with `/authorize` using the bare layout. QR rendering occurs locally from the server-provided URI and is never persisted. The settings page groups Appearance, Server, Security, and Authorized Clients. It never exposes an editable B URL in Web C. All network and clipboard work goes through `ClientRuntime`.

- [ ] **Step 4: Verify GREEN**

Run all client-ui/Web tests, typecheck, production build, accessibility/responsive contracts, and privacy storage scans.

- [ ] **Step 5: Commit**

```bash
git add packages/client-ui apps/clients/web package.json package-lock.json tests
git commit -m "feat(web): add secure authentication settings"
```

### Task 9: One Tauri 2 project for desktop and mobile

**Files:**
- Create: `apps/clients/tauri/package.json`
- Create: `apps/clients/tauri/index.html`
- Create: `apps/clients/tauri/tsconfig.json`
- Create: `apps/clients/tauri/vite.config.ts`
- Create: `apps/clients/tauri/src/main.ts`
- Create: `apps/clients/tauri/src/router.ts`
- Create: `apps/clients/tauri/src/runtime.ts`
- Create: `apps/clients/tauri/src/adapters/tauriHttpTransport.ts`
- Create: `apps/clients/tauri/src/adapters/tauriAuthorizationBrowser.ts`
- Create: `apps/clients/tauri/src/adapters/tauriCredentialVault.ts`
- Create: `apps/clients/tauri/src/adapters/tauriKey.ts`
- Create adapter/runtime tests
- Create: `apps/clients/tauri/src-tauri/Cargo.toml`
- Create: `apps/clients/tauri/src-tauri/build.rs`
- Create: `apps/clients/tauri/src-tauri/tauri.conf.json`
- Create: `apps/clients/tauri/src-tauri/capabilities/default.json`
- Create: `apps/clients/tauri/src-tauri/src/main.rs`
- Create: `apps/clients/tauri/src-tauri/src/lib.rs`
- Create: `apps/clients/tauri/src-tauri/src/auth.rs`
- Create Rust unit tests
- Modify: `package-lock.json`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing Tauri composition and Rust security tests**

TypeScript tests assert the Tauri entry imports only shared client packages plus adapters, uses native token transport rather than Cookie login, and never uses browser storage for credentials. Rust tests assert P-256 private key and refresh token serialization never reaches command responses/log output, signing returns only signatures/public JWK, vault entries are keyed by canonical issuer, and deep-link callbacks are single-consumer/state checked.

- [ ] **Step 2: Verify RED**

Run workspace tests and `cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml`. Expected: project and adapters do not exist.

- [ ] **Step 3: Scaffold the thin Tauri composition**

Use one Tauri 2 application identifier and shared Vue routes. Add only official opener, deep-link, HTTP, and Stronghold capabilities required by the adapters. Configure `termflow://auth/callback` for desktop development and claimed HTTPS placeholders for mobile release configuration. The Rust shell owns the non-exportable-or-vaulted P-256 private key, DPoP signing, and refresh token storage; JavaScript receives public JWK, signature bytes, access token, expiry, and non-sensitive assurance status only.

Desktop and mobile execute the identical `NativeAuthorizationSession`; platform conditionals are restricted to callback registration and secure-storage assurance reporting.

- [ ] **Step 4: Verify GREEN on available toolchains**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client
npm run typecheck --workspace @termflow/tauri-client
npm run build --workspace @termflow/tauri-client
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
cargo check --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
```

On Linux with the documented Tauri prerequisites installed, also run `npm run tauri:build --workspace @termflow/tauri-client -- --no-bundle`. Cross-platform bundle jobs are added in Task 10 rather than pretending Linux produced macOS/iOS/Windows-signed artifacts.

- [ ] **Step 5: Commit**

```bash
git add apps/clients/tauri package.json package-lock.json .gitignore
git commit -m "feat(client): add shared Tauri desktop and mobile shell"
```

### Task 10: End-to-end delivery, Docker recovery, CI, and documentation

**Files:**
- Modify: `scripts/verify.sh`
- Modify: `scripts/run-web-e2e.sh`
- Modify: `.github/workflows/ci.yml`
- Modify: `deploy/Dockerfile.control-plane`
- Modify: `deploy/compose.yaml`
- Modify: `.dockerignore`
- Modify: `tests/deploy/test_compose_contract.py`
- Modify: `tests/e2e/conftest.py`
- Create: `tests/e2e/test_unified_auth.py`
- Modify: `apps/clients/web/e2e/control-center.spec.ts`
- Modify: `docs/web-client.md`
- Modify: `docs/operations.md`
- Modify: `apps/clients/README.md`

- [ ] **Step 1: Write failing delivery and E2E tests**

Add real-process tests for TOTP enable/login/replay/disable, native authorization code interception failure, DPoP token/key mismatch, refresh rotation/replay family revocation, client revoke, Docker CLI reset and epoch invalidation, Web settings/QR/consent, and existing A reconnect. Assert terminal content remains absent from B persistence.

Extend the image contract to assert the final runtime contains installed B/protocol wheels or their installed virtual environment, compiled Web `dist`, and reset CLI but excludes A (including `apps/node/pyproject.toml`), Tauri, Cargo, Node, Python/TypeScript project source trees, frontend source, tests, and the monorepo lock/source tree. Build Python wheels in a dedicated uv stage rather than running editable workspace installs in the final stage.

- [ ] **Step 2: Verify RED**

Run focused E2E/deploy tests before wiring scripts and CI. Expected: missing orchestration and build gates.

- [ ] **Step 3: Wire verification and CI**

The primary verification script runs generated contract drift, all npm workspace tests/typechecks/builds, all Python tests, Ruff, Mypy, Rust format/clippy/test/check, Compose config, Docker build, and final image file checks. CI pins Node 22.23.2 and Rust stable, caches by root lock/Cargo.lock, and uses separate unsigned build jobs for Linux, Windows, macOS, Android compile, and iOS compile where the runner supports them. Signing credentials and publication remain separate protected release work.

- [ ] **Step 4: Run complete acceptance**

Run:

```bash
./scripts/verify.sh
./scripts/run-web-e2e.sh
.venv/bin/pytest -q tests/e2e/test_unified_auth.py tests/e2e/test_full_terminal_control.py tests/e2e/test_no_content_persistence.py
docker compose -f deploy/compose.yaml build --no-cache control-plane
```

Start a disposable Compose project with a temporary volume and test health, SPA fallback, TOTP setup/login, native authorization, reset, and A reconnect. Record exact unsupported platform gates rather than treating configuration as a successful build.

- [ ] **Step 5: Self-review the complete spec**

Re-read every requirement in `docs/superpowers/specs/2026-08-01-termflow-unified-client-auth-design.md`; map it to code and test evidence. Search for `TBD`, `TODO`, raw credential logging, browser credential persistence, direct platform imports in core/UI, and terminal resize messages. Fix every gap before review.

- [ ] **Step 6: Commit**

```bash
git add .github .dockerignore deploy scripts tests docs apps/clients
git commit -m "test(auth): verify unified client delivery"
```

### Task 11: Independent review, merge, and live deployment

**Files:** all changed files and generated artifacts.

- [ ] **Step 1: Spec compliance review**

Dispatch an independent reviewer with the complete approved spec, exact commit range, and acceptance outputs. Resolve every Critical or Important gap and repeat review until PASS.

- [ ] **Step 2: Code quality and security review**

Dispatch a separate reviewer to inspect cryptography boundaries, transaction atomicity, concurrency, error non-enumeration, secret handling, client isolation, dependency pinning, Docker contents, and test quality. Resolve findings and repeat review until PASS.

- [ ] **Step 3: Fresh final verification**

Run all Task 10 commands from a clean worktree and verify `git diff --check` plus clean status. Tests or configuration alone do not count as live acceptance.

- [ ] **Step 4: Merge and deploy**

Fast-forward or merge the reviewed branch into local `main`, rerun the complete verification on merged `main`, rebuild/recreate the existing Compose deployment while retaining `termflow-data`, and verify:

```text
container healthy
/healthz returns 200
Web settings route returns the compiled app
TOTP disabled default login works
configured TOTP enable/login/reset disposable test works
existing database and Computer/Term metadata remain present
```

- [ ] **Step 5: Cleanup**

Remove only the project-owned merged worktree and feature branch. Do not push or publish installers without an explicit user request.
