# Tauri Tokenless Native Boundary Implementation Plan

**Implementation status (2026-08-28):** All repository changes in this plan are implemented and pass `scripts/verify.sh`; Tauri's 21 Rust unit tests and 3 capability-scope tests pass with credentials and DPoP private operations retained behind the Rust boundary.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure OAuth access/refresh tokens and DPoP signing never cross from Rust into the Tauri WebView, while constraining the remaining native HTTP capability by origin, redirect, header, time, request-size, and response-size limits.

**Architecture:** Rust remains the sole owner of access state, refresh tokens, private keys, nonces, authorization headers, and DPoP signing. TypeScript receives only public-key metadata and a tokenless authorization status, then asks Rust to perform same-origin API requests. The generic signer/header/refresh IPC commands and the JavaScript credential vault are deleted instead of hidden behind convention.

**Tech Stack:** Rust, Tauri 2, reqwest, TypeScript, Vue, Vitest, Cargo test

---

## Security invariants

- No Tauri command serializes an access token, refresh token, `Authorization` header, DPoP proof, or raw private-key signature to JavaScript.
- The invoke handler does not register `native_sign_jwt`, `native_refresh_access`, `native_request_headers`, or `native_remember_dpop_nonce`.
- The WebView has no credential vault and no generic signing port; it sees `{ authorized: true, expiresAt, tokenType }` only.
- Native HTTP accepts no caller-supplied headers or nonce, follows no redirects, targets only the configured issuer origin under `/api/` or the fixed public bootstrap paths, and rejects request/response bodies over fixed byte limits.
- Production CSP does not allow arbitrary `https:` or `wss:` destinations.

### Task 1: Replace shared native authorization credentials with tokenless status

**Files:**
- Modify: `packages/client-core/src/auth/ports.ts`
- Modify: `packages/client-core/src/auth/nativeAuthorization.ts`
- Modify: `packages/client-core/src/auth/deviceAuthorization.ts`
- Modify: `packages/client-core/src/auth/nativeAuthorization.test.ts`
- Modify: `packages/client-core/src/auth/deviceAuthorization.test.ts`
- Modify: `packages/client-core/src/index.ts`
- Delete: `packages/client-core/src/auth/tokenSession.ts`
- Delete: `packages/client-core/src/auth/tokenSession.test.ts`

- [ ] **Step 1: Write failing tokenless-session tests**

Change the native authorization test to construct a session without a vault and assert the public result exactly:

```typescript
expect(await session.authorize()).toEqual({
  authorized: true,
  expiresAt: '2026-08-21T12:00:00Z',
  tokenType: 'DPoP',
})
expect(exchangeAuthorization).toHaveBeenCalledWith({
  issuer: 'https://termflow.example',
  transactionId: expect.any(String),
  codeVerifier: expect.any(String),
  redirectUri: expect.stringMatching(/^http:\/\/127\.0\.0\.1:/),
})
```

In the device-flow test, make `poll` return the same status object and assert no vault call exists.

- [ ] **Step 2: Run the two tests and observe type/constructor failures**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-core -- \
  src/auth/nativeAuthorization.test.ts src/auth/deviceAuthorization.test.ts
```

Expected: tests fail because sessions still require `CredentialVaultPort` and return `NativeAccessCredential`.

- [ ] **Step 3: Split public-key metadata from generic signing**

Use these interfaces in `ports.ts`:

```typescript
export interface NativePublicKeyPort {
  publicJwk(): Promise<PublicEcJwk>
  thumbprint(): Promise<string>
}

export interface NativeKeyPort extends NativePublicKeyPort {
  signJwt(signingInput: Uint8Array): Promise<Uint8Array>
}

export interface NativeAuthorizationStatus {
  authorized: true
  expiresAt: string
  tokenType: 'DPoP'
}
```

Delete `NativeAccessCredential` and `CredentialVaultPort`. Keep `NativeKeyPort` only for the package-level DPoP primitives; native authorization sessions must depend on `NativePublicKeyPort`.

- [ ] **Step 4: Make both authorization sessions tokenless**

In `nativeAuthorization.ts`, remove `vault` from `NativeAuthorizationOptions`, change `key` to `NativePublicKeyPort`, change `exchangeAuthorization` and `authorize()` to return `Promise<NativeAuthorizationStatus>`, and return the exchange result directly.

In `deviceAuthorization.ts`, change the poll response to `NativeAuthorizationStatus`, remove OAuth-token-to-credential conversion and vault persistence, and return status directly after the successful poll.

- [ ] **Step 5: Delete the unused generic token session surface**

Delete `tokenSession.ts` and its test, then remove their exports from `packages/client-core/src/index.ts`. Verify no production or test import remains:

```bash
rg -n "CredentialVaultPort|NativeAccessCredential|TokenSession" packages apps
```

Expected: no matches.

- [ ] **Step 6: Run package tests and commit**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/client-core
git add -- packages/client-core
git commit -m "security: make native authorization results tokenless"
```

### Task 2: Keep access credentials inside Rust and delete credential-export IPC

**Files:**
- Modify: `apps/clients/tauri/src-tauri/src/auth.rs`
- Modify: `apps/clients/tauri/src-tauri/src/lib.rs`
- Modify: `apps/clients/tauri/src-tauri/tests/http_capability_scope.rs`

- [ ] **Step 1: Add failing Rust/source contract assertions**

In `http_capability_scope.rs`, load `src/lib.rs` and assert each forbidden command is absent from `generate_handler!`:

```rust
for forbidden in [
    "auth::native_sign_jwt,",
    "auth::native_refresh_access,",
    "auth::native_request_headers,",
    "auth::native_remember_dpop_nonce,",
] {
    assert!(!lib_source.contains(forbidden), "forbidden IPC command: {forbidden}");
}
```

Add a unit test in `auth.rs` that serializes the public status and asserts:

```rust
let value = serde_json::to_value(NativeAuthorizationStatus {
    authorized: true,
    expires_at: "2026-08-21T12:00:00Z".to_owned(),
    token_type: "DPoP",
})
.unwrap();
assert_eq!(value["authorized"], true);
assert!(value.get("accessToken").is_none());
assert!(value.get("refreshToken").is_none());
assert!(value.get("authorization").is_none());
assert!(value.get("dpop").is_none());
```

- [ ] **Step 2: Run the focused Rust tests and observe failures**

```bash
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml \
  --test http_capability_scope
```

Expected: the handler contract fails because the four commands are still registered.

- [ ] **Step 3: Introduce a tokenless Rust return type**

Replace `AccessCredential` with:

```rust
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NativeAuthorizationStatus {
    authorized: bool,
    expires_at: String,
    token_type: &'static str,
}
```

Keep `AccessState` private. Rename `access_credential` to
`authorization_status`, retain its existing `OffsetDateTime` conversion, and
make it return:

```rust
fn authorization_status(value: &AccessState) -> NativeAuthorizationStatus {
    let timestamp = time::OffsetDateTime::from_unix_timestamp(value.expires_at_unix)
        .unwrap_or(time::OffsetDateTime::UNIX_EPOCH);
    NativeAuthorizationStatus {
        authorized: true,
        expires_at: timestamp
            .format(&time::format_description::well_known::Rfc3339)
            .unwrap_or_default(),
        token_type: "DPoP",
    }
}
```

Change `store_token_response`, `refresh_access`, and both authorization exchange
commands to return `NativeAuthorizationStatus`. The store function still writes
the access token only into `NativeAuthState.access` and refresh data only to the
keyring. Refresh remains a private helper invoked by native HTTP, not an IPC
command.

- [ ] **Step 4: Remove export-only command functions**

Delete the `#[tauri::command]` functions `native_sign_jwt`, `native_refresh_access`, `native_request_headers`, and `native_remember_dpop_nonce`. Keep private signing, refresh, nonce, and header construction helpers because `native_http_request` needs them internally.

Remove the four names from `generate_handler!` in `lib.rs`.

- [ ] **Step 5: Run Rust tests and commit**

```bash
cargo fmt --manifest-path apps/clients/tauri/src-tauri/Cargo.toml -- --check
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
git add -- apps/clients/tauri/src-tauri/src/auth.rs \
  apps/clients/tauri/src-tauri/src/lib.rs \
  apps/clients/tauri/src-tauri/tests/http_capability_scope.rs
git commit -m "security: keep native credentials behind the Rust IPC boundary"
```

### Task 3: Remove the JavaScript credential vault and signer adapter

**Files:**
- Modify: `apps/clients/tauri/src/adapters/tauriAuthorization.ts`
- Modify: `apps/clients/tauri/src/adapters/tauriAuthorization.test.ts`
- Create: `apps/clients/tauri/src/adapters/tauriCredentialControl.ts`
- Create: `apps/clients/tauri/src/adapters/tauriCredentialControl.test.ts`
- Delete: `apps/clients/tauri/src/adapters/tauriCredentialVault.ts`
- Delete: `apps/clients/tauri/src/adapters/tauriCredentialVault.test.ts`
- Delete: `apps/clients/tauri/src/adapters/memoryAccessVault.ts`
- Modify: `apps/clients/tauri/src/nativeAuth.ts`
- Modify: `apps/clients/tauri/src/nativeAuth.test.ts`
- Modify: `apps/clients/tauri/src/runtime.ts`
- Modify: `apps/clients/tauri/src/runtime.test.ts`

- [ ] **Step 1: Rewrite adapter tests around a public-only key**

The adapter test must expect only these invokes:

```typescript
expect(await key.publicJwk()).toEqual(publicJwk)
expect(await key.thumbprint()).toBe('thumbprint')
expect(invoke).toHaveBeenNthCalledWith(1, 'native_public_jwk', { issuer })
expect(invoke).toHaveBeenNthCalledWith(2, 'native_key_thumbprint', { issuer })
```

Delete signer, refresh, headers, and nonce expectations. Add a focused credential-control test:

```typescript
await clearNativeCredentials(issuer)
expect(invoke).toHaveBeenCalledWith('native_clear_credentials', { issuer })
```

- [ ] **Step 2: Run the Tauri TypeScript tests and observe failures**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/tauri-client -- \
  src/adapters/tauriAuthorization.test.ts \
  src/nativeAuth.test.ts src/runtime.test.ts
```

- [ ] **Step 3: Replace the adapter with a public-key-only port**

Export `createTauriPublicKey(issuer): NativePublicKeyPort` with exactly `publicJwk()` and `thumbprint()`. Exchange and device-poll adapters return `NativeAuthorizationStatus`; no function receives an access token.

Create `tauriCredentialControl.ts`:

```typescript
import { invoke } from '@tauri-apps/api/core'

export async function clearNativeCredentials(issuer: string): Promise<void> {
  await invoke('native_clear_credentials', { issuer })
}
```

- [ ] **Step 4: Delete all JavaScript vault code and update runtime composition**

Delete both vault adapter files and `memoryAccessVault.ts`. Update `nativeAuth.ts` and `runtime.ts` so authorization sessions receive the public key and exchange functions only; use `clearNativeCredentials` on logout/revocation.

Run:

```bash
rg -n "accessToken|native_sign_jwt|native_refresh_access|native_request_headers|native_remember_dpop_nonce|CredentialVault" \
  apps/clients/tauri/src
```

Expected: no matches outside negative security-contract assertions.

- [ ] **Step 5: Run tests and commit**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/tauri-client
git add --all apps/clients/tauri/src
git commit -m "security: remove WebView credential and signing capabilities"
```

### Task 4: Constrain native HTTP and production CSP

**Files:**
- Modify: `apps/clients/tauri/src-tauri/Cargo.toml`
- Modify: `apps/clients/tauri/src-tauri/src/auth.rs`
- Modify: `apps/clients/tauri/src/adapters/tauriHttpTransport.ts`
- Modify: `apps/clients/tauri/src/adapters/tauriHttpTransport.test.ts`
- Modify: `apps/clients/tauri/src-tauri/tauri.conf.json`
- Modify: `apps/clients/tauri/src/security-contract.test.ts`

- [ ] **Step 1: Add failing transport-security tests**

Add TypeScript tests proving the invoke payload has neither `headers` nor
`nonce`. The existing 401 DPoP-nonce retry must now require only one JavaScript
invoke because Rust performs the retry internally. In the security contract,
require `redirect(Policy::none())`, both byte-limit constants, and absence of
broad CSP sources `https:` and `wss:`.

Add Rust unit tests for the allowlisted response headers and body limit:

```rust
#[test]
fn native_http_exposes_only_safe_response_headers() {
    for allowed in ["content-type", "dpop-nonce", "retry-after", "x-request-id"] {
        assert!(response_header_allowed(allowed));
    }
    for forbidden in ["set-cookie", "authorization", "proxy-authenticate", "location"] {
        assert!(!response_header_allowed(forbidden));
    }
}
```

Extract a pure append helper so the byte ceiling is unit-testable without a
network server:

```rust
fn append_response_chunk(buffer: &mut Vec<u8>, chunk: &[u8]) -> Result<(), String> {
    if chunk.len() > NATIVE_HTTP_RESPONSE_MAX_BYTES.saturating_sub(buffer.len()) {
        return Err(safe_error("response_too_large"));
    }
    buffer.extend_from_slice(chunk);
    Ok(())
}
```

The body-collector test must call it with one chunk of
`NATIVE_HTTP_RESPONSE_MAX_BYTES` and a second one-byte chunk, then assert
`response_too_large`.

- [ ] **Step 2: Run focused tests and observe failures**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/tauri-client -- \
  src/adapters/tauriHttpTransport.test.ts src/security-contract.test.ts
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
```

- [ ] **Step 3: Build a hardened reqwest client explicitly**

Remove `#[derive(Default)]` from `NativeAuthState` and implement `Default` with:

```rust
impl Default for NativeAuthState {
    fn default() -> Self {
        let http = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .expect("native HTTP client configuration must be valid");
        Self {
            access: Mutex::new(HashMap::new()),
            nonces: Mutex::new(HashMap::new()),
            callback_listeners: Mutex::new(HashMap::new()),
            refresh_gate: tokio::sync::Mutex::new(()),
            http,
        }
    }
}
```

Add:

```rust
const NATIVE_HTTP_REQUEST_MAX_BYTES: usize = 256 * 1024;
const NATIVE_HTTP_RESPONSE_MAX_BYTES: usize = 1024 * 1024;
```

Serialize a request body once with `serde_json::to_vec`, reject it above the
request limit, and send those bytes with native-set `Content-Type:
application/json`. Enable reqwest's `stream` feature, consume `bytes_stream()`,
and pass every chunk to `append_response_chunk`; abort before concatenating
beyond the response limit.

- [ ] **Step 4: Remove arbitrary request headers and filter response headers**

Delete `headers` and `nonce` from the Rust command arguments and TypeScript
invoke payload. Stop forwarding `HttpRequest.headers`; no production API caller
sets one. Native code alone sets `Content-Type`, `Authorization`, and `DPoP`,
remembers `DPoP-Nonce` from the first response, performs the one native retry,
and stores the final nonce without returning it for retry control in JavaScript.

Implement:

```rust
fn response_header_allowed(name: &str) -> bool {
    matches!(
        name,
        "content-type" | "dpop-nonce" | "retry-after" | "x-request-id"
    )
}
```

Serialize only those response headers. Keep `assert_http_target` as the
same-origin gate for `/api/` and the fixed public bootstrap paths, and treat any
3xx as a response rather than following it.

- [ ] **Step 5: Narrow the CSP**

Set the production CSP in `tauri.conf.json` to:

```text
default-src 'self'; connect-src ipc: http://ipc.localhost http://127.0.0.1:* http://localhost:* ws://127.0.0.1:* ws://localhost:*; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'
```

This retains loopback development/HMR paths and removes arbitrary Internet destinations from WebView fetch/WebSocket APIs.

- [ ] **Step 6: Run focused verification and commit**

```bash
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH \
  npm run test:run --workspace @termflow/tauri-client
cargo fmt --manifest-path apps/clients/tauri/src-tauri/Cargo.toml -- --check
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
git add -- apps/clients/tauri/src-tauri apps/clients/tauri/src/adapters/tauriHttpTransport.ts \
  apps/clients/tauri/src/adapters/tauriHttpTransport.test.ts \
  apps/clients/tauri/src/security-contract.test.ts
git commit -m "security: constrain native HTTP and WebView CSP"
```

### Task 5: Align documentation and perform boundary-wide verification

**Files:**
- Modify: `docs/security.md`
- Modify: `tests/docs/test_documentation_contract.py`
- Verify: all Tauri and repository checks

- [ ] **Step 1: Make the documentation contract describe mechanically enforced facts**

Require these statements in the security guide:

```markdown
The Tauri WebView never receives access tokens, refresh tokens, DPoP proofs,
private-key signatures, or authorization headers. Rust returns only public-key
metadata and tokenless authorization status. Authenticated API requests are
performed by the bounded same-origin native HTTP command.
```

Delete any sentence that attributes this boundary only to convention.

- [ ] **Step 2: Run negative source scans**

```bash
rg -n "native_sign_jwt|native_refresh_access|native_request_headers|native_remember_dpop_nonce" \
  apps/clients/tauri packages/client-core
rg -n "accessToken|CredentialVaultPort|NativeAccessCredential" \
  apps/clients/tauri packages/client-core
```

Expected: only negative-test string literals may match; production sources must not.

- [ ] **Step 3: Run complete verification**

```bash
.envs/dev/bin/python -m pytest tests/docs/test_documentation_contract.py -q
PATH=/home/mcocdaa/.nvm/versions/node/v22.23.2/bin:$PATH ./scripts/verify.sh
```

Expected: the full verifier passes. Treat this as source/build evidence; separately rerun native login, authenticated API calls, logout, and refresh-expiry behavior on at least one desktop and one Android build before release acceptance.

- [ ] **Step 4: Commit documentation**

```bash
git add -- docs/security.md tests/docs/test_documentation_contract.py
git commit -m "docs: record the enforced tokenless native boundary"
```
