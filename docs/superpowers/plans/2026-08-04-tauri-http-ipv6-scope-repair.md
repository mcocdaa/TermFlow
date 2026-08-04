# Tauri HTTP IPv6 Scope Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the native client's Tauri HTTP capability parse and match HTTPS plus loopback HTTP correctly, and distinguish capability failures from network outages.

**Architecture:** Keep the existing Tauri allowlist boundary and repair only its IPv6 URLPattern syntax. Add a Rust integration contract against the pinned URLPattern implementation, then propagate a typed `http_capability_denied` error from the Tauri transport through client-core to the connection UI.

**Tech Stack:** Tauri 2, Rust, `urlpattern` 0.3, serde/serde_json, Vue 3, TypeScript, Vitest.

---

### Task 1: Parse and Match the Native HTTP Capability

**Files:**
- Modify: `apps/clients/tauri/src-tauri/capabilities/default.json:13-18`
- Modify: `apps/clients/tauri/src-tauri/capabilities/mobile.json:15-20`
- Modify: `apps/clients/tauri/src-tauri/Cargo.toml:32-34`
- Create: `apps/clients/tauri/src-tauri/tests/http_capability_scope.rs`
- Modify: `tests/tauri/test_native_network_capability_contract.py:7-43`

- [ ] **Step 1: Write the failing Rust capability test**

Add direct dev dependencies without changing third-party versions already locked:

```toml
[dev-dependencies]
regex = "1.11.1"
tempfile = "3.20.0"
urlpattern = "=0.3.0"
```

Create an integration test that mirrors `tauri-plugin-http` 2.5.9's pattern preparation and reads both product capability files:

```rust
use regex::Regex;
use serde_json::Value;
use std::{fs, path::PathBuf};
use url::Url;
use urlpattern::{UrlPattern, UrlPatternInit, UrlPatternMatchInput};

fn parse_pattern(value: &str) -> UrlPattern {
    let mut init = UrlPatternInit::parse_constructor_string::<Regex>(value, None)
        .unwrap_or_else(|error| panic!("invalid HTTP capability pattern {value}: {error}"));
    if init.search.as_ref().map(|value| value.is_empty()).unwrap_or(true) {
        init.search.replace("*".to_string());
    }
    if init.hash.as_ref().map(|value| value.is_empty()).unwrap_or(true) {
        init.hash.replace("*".to_string());
    }
    if init.pathname.as_ref().map(|value| value.is_empty() || value == "/").unwrap_or(true) {
        init.pathname.replace("*".to_string());
    }
    UrlPattern::parse(init, Default::default())
        .unwrap_or_else(|error| panic!("invalid HTTP capability pattern {value}: {error}"))
}

fn configured_patterns(capability: &str) -> Vec<UrlPattern> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("capabilities")
        .join(capability);
    let document: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    document["permissions"].as_array().unwrap().iter()
        .find(|permission| permission["identifier"] == "http:default")
        .unwrap()["allow"].as_array().unwrap().iter()
        .map(|entry| parse_pattern(entry["url"].as_str().unwrap()))
        .collect()
}

fn is_allowed(patterns: &[UrlPattern], value: &str) -> bool {
    let url = Url::parse(value).unwrap();
    patterns.iter().any(|pattern| {
        pattern.test(UrlPatternMatchInput::Url(url.clone())).unwrap_or(false)
    })
}

#[test]
fn native_http_capabilities_parse_and_allow_only_secure_or_loopback_servers() {
    for capability in ["default.json", "mobile.json"] {
        let patterns = configured_patterns(capability);
        for allowed in [
            "https://relay.example.com/.well-known/oauth-authorization-server",
            "http://127.0.0.1:8765/healthz",
            "http://localhost:8765/healthz",
            "http://[::1]:8765/healthz",
        ] {
            assert!(is_allowed(&patterns, allowed), "{capability} rejected {allowed}");
        }
        assert!(!is_allowed(&patterns, "http://relay.example.com/healthz"));
    }
}
```

- [ ] **Step 2: Run the Rust test and verify it fails for the raw IPv6 rule**

Run:

```bash
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --test http_capability_scope --locked
```

Expected: FAIL with `invalid HTTP capability pattern http://[::1]:*` and the URLPattern tokenizer error.

- [ ] **Step 3: Apply the minimal capability fix**

In both capability files, replace only the IPv6 entry:

```json
{ "url": "http://[\\:\\:1]:*" }
```

Update the Python contract's expected literal to the raw string `r"http://[\:\:1]:*"` so it continues to enforce HTTPS-or-loopback-only policy at the JSON level.

- [ ] **Step 4: Run focused capability checks**

Run:

```bash
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --test http_capability_scope --locked
.venv/bin/python -m pytest tests/tauri/test_native_network_capability_contract.py -q
```

Expected: Rust test passes; Python reports `2 passed`.

- [ ] **Step 5: Commit the capability repair**

```bash
git add apps/clients/tauri/src-tauri/Cargo.toml \
  apps/clients/tauri/src-tauri/Cargo.lock \
  apps/clients/tauri/src-tauri/capabilities/default.json \
  apps/clients/tauri/src-tauri/capabilities/mobile.json \
  apps/clients/tauri/src-tauri/tests/http_capability_scope.rs \
  tests/tauri/test_native_network_capability_contract.py
git commit -m "fix(tauri): repair IPv6 HTTP capability scope"
```

### Task 2: Preserve Capability Failures Through Client Core

**Files:**
- Modify: `packages/client-core/src/http/types.ts:46-54`
- Modify: `packages/client-core/src/http/apiError.ts:1-12`
- Modify: `packages/client-core/src/http/apiClient.ts:29-39`
- Modify: `packages/client-core/src/http/apiClient.test.ts:98-109`

- [ ] **Step 1: Write the failing client-core mapping test**

Extend the transport-failure table:

```ts
it.each([
  ['aborted', 'aborted'],
  ['offline', 'offline'],
  ['http_capability_denied', 'http_capability_denied'],
] as const)('maps the %s transport failure without logging payloads', async (transportKind, apiKind) => {
```

- [ ] **Step 2: Run the focused client-core test and verify type/test failure**

Run:

```bash
npm run test:run --workspace @termflow/client-core -- --run src/http/apiClient.test.ts
```

Expected: FAIL because `http_capability_denied` is not yet a transport or API error kind.

- [ ] **Step 3: Add the typed error kind and safe mapping**

Use these unions and safe message:

```ts
export type HttpTransportErrorKind = 'aborted' | 'offline' | 'invalid_request' | 'http_capability_denied'

export type ApiErrorKind = 'offline' | 'authentication' | 'validation' | 'rate_limit' | 'server' | 'aborted' | 'http_capability_denied'

const safeMessages: Record<ApiErrorKind, string> = {
  offline: '无法连接服务，请检查网络后重试。',
  authentication: '会话已过期，请重新登录。',
  validation: '提交的内容不符合要求。',
  rate_limit: '操作过于频繁，请稍后重试。',
  server: '服务暂时不可用，请稍后重试。',
  aborted: '请求已取消。',
  http_capability_denied: '客户端网络权限配置无效，请升级或重新安装 TermFlow。',
}
```

Before the fallback in `transportFailure`, add:

```ts
if (error.kind === 'http_capability_denied') return new ApiError('http_capability_denied')
```

- [ ] **Step 4: Run client-core tests and type checking**

Run:

```bash
npm run test:run --workspace @termflow/client-core
npm run typecheck --workspace @termflow/client-core
```

Expected: both commands pass.

- [ ] **Step 5: Commit typed propagation**

```bash
git add packages/client-core/src/http/types.ts \
  packages/client-core/src/http/apiError.ts \
  packages/client-core/src/http/apiClient.ts \
  packages/client-core/src/http/apiClient.test.ts
git commit -m "fix(client-core): preserve HTTP capability failures"
```

### Task 3: Classify Tauri Errors and Show Actionable UI Copy

**Files:**
- Modify: `apps/clients/tauri/src/adapters/tauriHttpTransport.ts:6-61`
- Modify: `apps/clients/tauri/src/adapters/tauriHttpTransport.test.ts:1-91`
- Modify: `apps/clients/tauri/src/views/NativeConnectView.vue:24-42`
- Modify: `apps/clients/tauri/src/views/NativeConnectView.test.ts:91-117`

- [ ] **Step 1: Write failing transport classification tests**

Add tests covering the two messages emitted by Tauri/Tauri HTTP:

```ts
it.each([
  new Error('error deserializing scope: `bad` is not a valid URL pattern'),
  'url not allowed on the configured scope: http://relay.example.com/',
])('classifies a Tauri HTTP scope failure separately from offline errors', async (failure) => {
  tauriFetch.mockRejectedValue(failure)

  await expect(createTauriHttpTransport().request('/healthz', { method: 'GET' }))
    .rejects.toMatchObject({ kind: 'http_capability_denied' })
})

it('keeps ordinary fetch failures classified as offline', async () => {
  tauriFetch.mockRejectedValue(new TypeError('Failed to fetch'))

  await expect(createTauriHttpTransport().request('/healthz', { method: 'GET' }))
    .rejects.toMatchObject({ kind: 'offline' })
})
```

Add a connection-view case using `new ApiError('http_capability_denied')` and expecting `客户端网络权限配置无效。请升级或重新安装 TermFlow。`.

- [ ] **Step 2: Run focused Tauri frontend tests and verify failure**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client -- --run \
  src/adapters/tauriHttpTransport.test.ts src/views/NativeConnectView.test.ts
```

Expected: capability failures are still reported as `offline`, and the UI still uses the generic network message.

- [ ] **Step 3: Implement narrow Tauri error classification**

Add helpers that accept both `Error` and string rejections without exposing raw errors:

```ts
function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return typeof error === 'string' ? error : ''
}

function isHttpCapabilityFailure(error: unknown): boolean {
  const message = errorMessage(error).toLowerCase()
  return message.includes('error deserializing scope:')
    || message.includes('url not allowed on the configured scope:')
}
```

Order the catch block from most specific to fallback:

```ts
if (error instanceof DOMException && error.name === 'AbortError') throw new HttpTransportError('aborted')
if (isHttpCapabilityFailure(error)) throw new HttpTransportError('http_capability_denied')
throw new HttpTransportError('offline')
```

In `registrationErrorMessage`, separate the two cases:

```ts
if (code === 'http_capability_denied') {
  return '客户端网络权限配置无效。请升级或重新安装 TermFlow。'
}
if (code === 'offline') {
  return '无法连接服务器。请检查服务器地址、网络连接和本机服务是否正在运行。'
}
```

- [ ] **Step 4: Run focused Tauri tests and type checking**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client
npm run typecheck --workspace @termflow/tauri-client
```

Expected: both commands pass.

- [ ] **Step 5: Commit the client-facing repair**

```bash
git add apps/clients/tauri/src/adapters/tauriHttpTransport.ts \
  apps/clients/tauri/src/adapters/tauriHttpTransport.test.ts \
  apps/clients/tauri/src/views/NativeConnectView.vue \
  apps/clients/tauri/src/views/NativeConnectView.test.ts
git commit -m "fix(tauri): distinguish HTTP capability errors"
```

### Task 4: Full Verification

**Files:**
- Verify only; no planned product changes.

- [ ] **Step 1: Run native and Python capability checks**

```bash
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --locked
.venv/bin/python -m pytest tests/tauri/test_native_network_capability_contract.py -q
```

Expected: all Rust tests and both Python capability cases pass.

- [ ] **Step 2: Run workspace frontend verification**

```bash
npm run test:run
npm run typecheck
npm run build --workspace @termflow/tauri-client
```

Expected: all workspace tests and type checks pass; the Tauri frontend production build succeeds.

- [ ] **Step 3: Run repository hygiene checks**

```bash
git diff --check main...HEAD
git status --short
```

Expected: no whitespace errors; only intentional branch commits remain and the worktree is clean.

- [ ] **Step 4: Report the packaging boundary**

Record that source and local tests are fixed. Do not claim a Windows installer exists until the branch is merged/pushed and the Windows GitHub Actions packaging job completes successfully.
