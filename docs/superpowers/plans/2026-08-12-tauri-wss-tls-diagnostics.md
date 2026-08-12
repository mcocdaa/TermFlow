# Tauri C WSS TLS and Terminal Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Rust-owned production `wss://` terminal connections in Tauri C and add credential-safe terminal connection lifecycle logs.

**Architecture:** Keep the existing TypeScript endpoint construction, Rust-owned DPoP credentials, same-origin target validation, and shared reconnect state machine. Enable the explicit `tokio-tungstenite` Rustls/WebPKI feature set, then let `terminal_socket.rs` write structured lifecycle events through the existing `NativeLogger` while exposing only stable safe error codes to the WebView.

**Tech Stack:** Rust 2021, Tauri 2, tokio-tungstenite 0.28, Rustls/WebPKI roots, Python 3.12 `tomllib` contract tests, Cargo tests, Vitest.

---

## File map

- Create `tests/tauri/test_terminal_wss_contract.py`: lock the Tauri Cargo dependency to an explicit WSS-capable feature set.
- Modify `apps/clients/tauri/src-tauri/Cargo.toml`: enable `connect` and `rustls-tls-webpki-roots` without implicit default features.
- Modify `apps/clients/tauri/src-tauri/Cargo.lock`: record the Rustls/WebPKI dependency graph resolved by Cargo.
- Modify `apps/clients/tauri/src-tauri/src/terminal_socket.rs`: classify connection errors, record connection/close lifecycle events, and emit abnormal close events to the existing reconnect state machine.
- Reuse `apps/clients/tauri/src-tauri/src/diagnostics.rs`: do not change its format or WebView command; call `NativeLogger::event` directly from Rust.

### Task 1: Lock and enable WSS TLS support

**Files:**
- Create: `tests/tauri/test_terminal_wss_contract.py`
- Modify: `apps/clients/tauri/src-tauri/Cargo.toml:31`
- Modify: `apps/clients/tauri/src-tauri/Cargo.lock`

- [ ] **Step 1: Write the failing Cargo feature contract test**

```python
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "apps/clients/tauri/src-tauri/Cargo.toml"


def test_terminal_websocket_enables_public_ca_rustls() -> None:
    manifest = tomllib.loads(MANIFEST.read_text())
    dependency = manifest["dependencies"]["tokio-tungstenite"]

    assert isinstance(dependency, dict)
    assert dependency["version"] == "0.28.0"
    assert dependency["default-features"] is False
    assert set(dependency["features"]) == {
        "connect",
        "rustls-tls-webpki-roots",
    }


def test_terminal_websocket_feature_tree_contains_rustls_and_webpki_roots() -> None:
    result = subprocess.run(
        [
            "cargo",
            "tree",
            "--manifest-path",
            str(MANIFEST),
            "-e",
            "features",
            "-i",
            "tokio-tungstenite",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert 'tokio-tungstenite feature "rustls-tls-webpki-roots"' in result.stdout
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python -m pytest tests/tauri/test_terminal_wss_contract.py -q
```

Expected: FAIL because `tokio-tungstenite` is currently a string dependency and the feature tree has no Rustls/WebPKI feature.

- [ ] **Step 3: Enable the minimal explicit TLS feature set**

Replace the dependency with:

```toml
tokio-tungstenite = { version = "0.28.0", default-features = false, features = ["connect", "rustls-tls-webpki-roots"] }
```

Refresh only the Rust lock resolution needed by the manifest:

```bash
cargo check --manifest-path apps/clients/tauri/src-tauri/Cargo.toml
```

Expected: exit 0 and `Cargo.lock` adds `rustls`, `tokio-rustls`, `rustls-pki-types`, and the compatible WebPKI roots edge to `tokio-tungstenite`.

- [ ] **Step 4: Run the feature contract and verify GREEN**

Run:

```bash
python -m pytest tests/tauri/test_terminal_wss_contract.py -q
cargo tree --manifest-path apps/clients/tauri/src-tauri/Cargo.toml -e features -i tokio-tungstenite
```

Expected: `2 passed`; the feature tree includes `connect`, `rustls-tls-webpki-roots`, and `__rustls-tls`.

- [ ] **Step 5: Commit the WSS dependency slice**

```bash
git add -- tests/tauri/test_terminal_wss_contract.py apps/clients/tauri/src-tauri/Cargo.toml apps/clients/tauri/src-tauri/Cargo.lock
git commit -m "fix(tauri): enable TLS for terminal WebSockets"
```

### Task 2: Add safe native terminal lifecycle diagnostics

**Files:**
- Modify: `apps/clients/tauri/src-tauri/src/terminal_socket.rs:9-153`
- Test: `apps/clients/tauri/src-tauri/src/terminal_socket.rs:190-end`

- [ ] **Step 1: Write failing unit tests for error classification and safe log records**

Extend the existing `terminal_socket.rs` test module with these imports and tests:

```rust
use std::fs;
use tempfile::tempdir;
use tokio_tungstenite::tungstenite::error::{ProtocolError, UrlError};
use tokio_tungstenite::tungstenite::Error as WsError;

#[test]
fn terminal_connection_errors_map_to_stable_safe_codes() {
    assert_eq!(
        terminal_connect_error_code(&WsError::Url(UrlError::TlsFeatureNotEnabled)),
        "socket_tls_unavailable"
    );
    assert_eq!(
        terminal_connect_error_code(&WsError::Io(std::io::Error::other("secret host"))),
        "socket_transport_failed"
    );
    assert_eq!(
        terminal_connect_error_code(&WsError::Protocol(ProtocolError::HandshakeIncomplete)),
        "socket_handshake_failed"
    );
    let response = http::Response::builder()
        .status(403)
        .body(None)
        .expect("HTTP response");
    assert_eq!(
        terminal_connect_error_code(&WsError::Http(response.into())),
        "socket_handshake_rejected"
    );
}

#[test]
fn terminal_log_records_keep_only_origin_and_stable_codes() {
    let directory = tempdir().expect("temporary log directory");
    let logger = crate::diagnostics::NativeLogger::new(directory.path().to_path_buf());

    log_terminal_event(
        &logger,
        "terminal_connect_failed",
        "https://b.example/path?terminal_id=secret",
        Some("socket_tls_failed"),
        Some("error"),
    );

    let line = fs::read_to_string(directory.path().join("termflow-client.log"))
        .expect("terminal log line");
    let record: serde_json::Value = serde_json::from_str(line.trim()).expect("JSON log");
    assert_eq!(record["event"], "terminal_connect_failed");
    assert_eq!(record["issuer"], "https://b.example/");
    assert_eq!(record["error_code"], "socket_tls_failed");
    assert!(!line.contains("terminal_id"));
    assert!(!line.contains("secret"));
}

#[test]
fn close_codes_are_bounded_structured_diagnostics() {
    assert_eq!(terminal_close_error_code(1000), "ws_close_1000");
    assert_eq!(terminal_close_error_code(4401), "ws_close_4401");
}
```

- [ ] **Step 2: Run the Rust tests and verify RED**

Run:

```bash
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml terminal_socket::tests -- --nocapture
```

Expected: compile FAIL because `terminal_connect_error_code`, `log_terminal_event`, and `terminal_close_error_code` do not exist.

- [ ] **Step 3: Add stable error mapping and the logger adapter**

Add imports:

```rust
use tokio_tungstenite::tungstenite::Error as WsError;

use crate::diagnostics::NativeLogger;
```

Add focused helpers above `native_terminal_connect`:

```rust
fn terminal_connect_error_code(error: &WsError) -> &'static str {
    use tokio_tungstenite::tungstenite::error::UrlError;

    match error {
        WsError::Url(UrlError::TlsFeatureNotEnabled) => "socket_tls_unavailable",
        WsError::Tls(_) => "socket_tls_failed",
        WsError::Io(_) => "socket_transport_failed",
        WsError::Http(_) => "socket_handshake_rejected",
        WsError::Protocol(_) | WsError::HttpFormat(_) => "socket_handshake_failed",
        _ => "socket_connect_failed",
    }
}

fn terminal_close_error_code(code: u16) -> String {
    format!("ws_close_{code}")
}

fn log_terminal_event(
    logger: &NativeLogger,
    event: &str,
    issuer: &str,
    error_code: Option<&str>,
    level: Option<&str>,
) {
    logger.event(event, level, Some(issuer), None, error_code, None);
}
```

- [ ] **Step 4: Instrument connect start, failure, and success without logging secrets**

Add `logger: State<'_, NativeLogger>` to `native_terminal_connect`. After canonicalizing the issuer, write `terminal_connect_started`. For each pre-connect `Result`, preserve the existing returned safe code and log it as `terminal_connect_failed`. Handle `connect_async` explicitly:

```rust
log_terminal_event(&logger, "terminal_connect_started", &issuer, None, None);

validate_terminal_targets(&issuer, &proof_url, &socket_url).map_err(|code| {
    log_terminal_event(
        &logger,
        "terminal_connect_failed",
        &issuer,
        Some(&code),
        Some("error"),
    );
    code
})?;
```

Apply the same closure pattern to auth-header and request/header construction failures. Replace the existing `connect_async(...).map_err(...)` with:

```rust
let (ws_stream, _) = match connect_async(request).await {
    Ok(connected) => connected,
    Err(error) => {
        let diagnostic_code = terminal_connect_error_code(&error);
        log_terminal_event(
            &logger,
            "terminal_connect_failed",
            &issuer,
            Some(diagnostic_code),
            Some("error"),
        );
        return Err(auth::safe_error("socket_connect_failed"));
    }
};
log_terminal_event(
    &logger,
    "terminal_connect_succeeded",
    &issuer,
    None,
    None,
);
```

Do not pass `error.to_string()`, `proof_url`, `socket_url`, headers, terminal IDs, or frame data to `NativeLogger`.

- [ ] **Step 5: Instrument close/read failures and preserve reconnect delivery**

Move a cloned canonical issuer into the spawned reader. On a close frame, emit the existing close event and record `terminal_socket_closed` with `ws_close_<code>`. On a read error or EOF without a close frame, emit a synthetic close event with code 1006 so `TerminalSession` follows its existing reconnect path, then log only `socket_read_failed` or `socket_eof`:

```rust
let socket_id = id.clone();
let socket_issuer = issuer.clone();
tauri::async_runtime::spawn(async move {
    let mut close_delivered = false;
    while let Some(message) = read.next().await {
        match message {
            Ok(WsMessage::Text(text)) => {
                let _ = on_message.send(
                    serde_json::json!({"type": "Text", "data": text.as_str()}),
                );
            }
            Ok(WsMessage::Binary(bytes)) => {
                let _ = on_message.send(
                    serde_json::json!({"type": "Binary", "data": bytes.to_vec()}),
                );
            }
            Ok(WsMessage::Close(frame)) => {
                let code: u16 = frame
                    .as_ref()
                    .map(|frame| frame.code.into())
                    .unwrap_or(1000);
                let _ = on_message.send(
                    serde_json::json!({"type": "Close", "data": {"code": code}}),
                );
                let code_text = terminal_close_error_code(code);
                let logger = app.state::<NativeLogger>();
                log_terminal_event(
                    &logger,
                    "terminal_socket_closed",
                    &socket_issuer,
                    Some(&code_text),
                    None,
                );
                close_delivered = true;
                break;
            }
            Ok(_) => {}
            Err(_) => {
                let _ = on_message.send(
                    serde_json::json!({"type": "Close", "data": {"code": 1006}}),
                );
                let logger = app.state::<NativeLogger>();
                log_terminal_event(
                    &logger,
                    "terminal_socket_closed",
                    &socket_issuer,
                    Some("socket_read_failed"),
                    Some("warn"),
                );
                close_delivered = true;
                break;
            }
        }
    }
    if !close_delivered {
        let _ = on_message.send(
            serde_json::json!({"type": "Close", "data": {"code": 1006}}),
        );
        let logger = app.state::<NativeLogger>();
        log_terminal_event(
            &logger,
            "terminal_socket_closed",
            &socket_issuer,
            Some("socket_eof"),
            Some("warn"),
        );
    }
    let sockets = app.state::<TerminalSocketState>();
    if let Ok(mut sockets) = sockets.sockets.lock() {
        sockets.remove(&socket_id);
    };
});
```

- [ ] **Step 6: Run focused Rust and TypeScript tests and verify GREEN**

Run:

```bash
cargo fmt --manifest-path apps/clients/tauri/src-tauri/Cargo.toml --all
cargo test --manifest-path apps/clients/tauri/src-tauri/Cargo.toml terminal_socket::tests -- --nocapture
npm run test:run --workspace @termflow/tauri-client -- src/adapters/tauriTerminalTransport.test.ts
npm run test:run --workspace @termflow/client-core -- src/terminal/session.test.ts
```

Expected: all focused Rust tests and both focused Vitest files pass; no credential or URL-query text appears in captured output.

- [ ] **Step 7: Commit the terminal diagnostics slice**

```bash
git add -- apps/clients/tauri/src-tauri/src/terminal_socket.rs
git commit -m "feat(tauri): log terminal socket lifecycle"
```

### Task 3: Run the release-relevant verification gates

**Files:**
- Verify only; no expected source changes.

- [ ] **Step 1: Run the Tauri Python contracts**

```bash
python -m pytest tests/tauri -q
```

Expected: all Tauri contract tests pass, including the new TLS feature contract.

- [ ] **Step 2: Run the complete Tauri WebView test suite and build**

```bash
npm run test:run --workspace @termflow/tauri-client
npm run typecheck --workspace @termflow/tauri-client
npm run build --workspace @termflow/tauri-client
```

Expected: Vitest reports zero failures; typecheck and Vite build exit 0.

- [ ] **Step 3: Run the complete Rust/Tauri verification script**

```bash
scripts/verify-tauri.sh
```

Expected: formatting, Clippy with `-D warnings`, all Rust targets/tests, Cargo check, and the unsigned Linux Tauri build gate exit 0. If the host lacks GTK/WebKit, the script must use its Docker fallback rather than skipping Rust checks.

- [ ] **Step 4: Run repository integrity checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; status contains no uncommitted implementation changes.

- [ ] **Step 5: Record the remaining Windows runtime gate**

Do not claim production runtime completion from Linux tests. The handoff must state that a freshly built Windows artifact still needs installation and connection to `https://termflow.mcocdaa-newapi.xin`, with `terminal_connect_succeeded` and `terminal.ready` observed before the WSS production issue is considered runtime-verified.
