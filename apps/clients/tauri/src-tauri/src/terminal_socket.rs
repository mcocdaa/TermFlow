//! Rust-owned terminal WebSocket channel.
//!
//! The WebView never opens raw sockets; it asks this module to connect, and
//! every target URL is pinned to the configured issuer origin and the
//! `/api/v1/terms/` prefix before any credentials are attached.

use std::collections::HashMap;
use std::sync::Mutex;

use futures_util::{SinkExt, StreamExt};
use http::HeaderValue;
use tauri::ipc::Channel;
use tauri::{Manager, State};
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::Error as WsError;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};

use crate::auth;
use crate::auth::NativeAuthState;
use crate::diagnostics::NativeLogger;

type SocketStream = WebSocketStream<MaybeTlsStream<TcpStream>>;
type SocketSink = futures_util::stream::SplitSink<SocketStream, WsMessage>;

struct OpenSocket {
    sink: Option<SocketSink>,
}

#[derive(Default)]
pub struct TerminalSocketState {
    sockets: Mutex<HashMap<String, OpenSocket>>,
}

fn validate_terminal_targets(
    issuer: &str,
    proof_url: &str,
    socket_url: &str,
) -> Result<(), String> {
    let base = url::Url::parse(issuer).map_err(|_| auth::safe_error("issuer_invalid"))?;
    for target in [proof_url, socket_url] {
        let mut parsed = url::Url::parse(target).map_err(|_| auth::safe_error("url_invalid"))?;
        if parsed.scheme() == "ws" {
            parsed
                .set_scheme("http")
                .map_err(|_| auth::safe_error("url_not_allowed"))?;
        } else if parsed.scheme() == "wss" {
            parsed
                .set_scheme("https")
                .map_err(|_| auth::safe_error("url_not_allowed"))?;
        }
        if parsed.origin() != base.origin()
            || parsed.username() != ""
            || parsed.password().is_some()
            || !parsed.path().starts_with("/api/v1/terms/")
        {
            return Err(auth::safe_error("url_not_allowed"));
        }
    }
    Ok(())
}

fn take_sink(state: &TerminalSocketState, id: &str) -> Result<SocketSink, String> {
    let mut sockets = state
        .sockets
        .lock()
        .map_err(|_| auth::safe_error("socket_state_unavailable"))?;
    let socket = sockets
        .get_mut(id)
        .ok_or_else(|| auth::safe_error("socket_not_found"))?;
    socket
        .sink
        .take()
        .ok_or_else(|| auth::safe_error("socket_busy"))
}

fn restore_sink(state: &TerminalSocketState, id: &str, sink: SocketSink) {
    if let Ok(mut sockets) = state.sockets.lock() {
        if let Some(socket) = sockets.get_mut(id) {
            socket.sink = Some(sink);
        }
    }
}

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

/// Connect the terminal WebSocket and stream frames back over the channel.
#[tauri::command]
pub async fn native_terminal_connect(
    app: tauri::AppHandle,
    auth_state: State<'_, NativeAuthState>,
    state: State<'_, TerminalSocketState>,
    issuer: String,
    proof_url: String,
    socket_url: String,
    on_message: Channel<serde_json::Value>,
) -> Result<String, String> {
    let logger = app.state::<NativeLogger>();
    let issuer = auth::canonical_issuer(&issuer)?;
    log_terminal_event(&logger, "terminal_connect_started", &issuer, None, None);
    validate_terminal_targets(&issuer, &proof_url, &socket_url).inspect_err(|code| {
        log_terminal_event(
            &logger,
            "terminal_connect_failed",
            &issuer,
            Some(code),
            Some("error"),
        );
    })?;
    let headers = auth::request_auth_headers(&auth_state, &issuer, "GET", &proof_url)
        .await
        .inspect_err(|code| {
            log_terminal_event(
                &logger,
                "terminal_connect_failed",
                &issuer,
                Some(code),
                Some("error"),
            );
        })?;

    let mut request = socket_url
        .into_client_request()
        .map_err(|_| auth::safe_error("socket_url_invalid"))
        .inspect_err(|code| {
            log_terminal_event(
                &logger,
                "terminal_connect_failed",
                &issuer,
                Some(code),
                Some("error"),
            );
        })?;
    request.headers_mut().insert(
        "Authorization",
        HeaderValue::from_str(&headers.authorization)
            .map_err(|_| auth::safe_error("socket_header_invalid"))
            .inspect_err(|code| {
                log_terminal_event(
                    &logger,
                    "terminal_connect_failed",
                    &issuer,
                    Some(code),
                    Some("error"),
                );
            })?,
    );
    request.headers_mut().insert(
        "DPoP",
        HeaderValue::from_str(&headers.dpop)
            .map_err(|_| auth::safe_error("socket_header_invalid"))
            .inspect_err(|code| {
                log_terminal_event(
                    &logger,
                    "terminal_connect_failed",
                    &issuer,
                    Some(code),
                    Some("error"),
                );
            })?,
    );

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
    let (sink, mut read) = ws_stream.split();
    let id = uuid::Uuid::new_v4().to_string();
    state
        .sockets
        .lock()
        .map_err(|_| auth::safe_error("socket_state_unavailable"))?
        .insert(id.clone(), OpenSocket { sink: Some(sink) });
    log_terminal_event(&logger, "terminal_connect_succeeded", &issuer, None, None);

    let socket_id = id.clone();
    let socket_issuer = issuer.clone();
    tauri::async_runtime::spawn(async move {
        let mut close_delivered = false;
        while let Some(message) = read.next().await {
            match message {
                Ok(WsMessage::Text(text)) => {
                    let _ =
                        on_message.send(serde_json::json!({"type": "Text", "data": text.as_str()}));
                }
                Ok(WsMessage::Binary(bytes)) => {
                    let _ = on_message
                        .send(serde_json::json!({"type": "Binary", "data": bytes.to_vec()}));
                }
                Ok(WsMessage::Close(frame)) => {
                    let code: u16 = frame
                        .as_ref()
                        .map(|frame| frame.code.into())
                        .unwrap_or(1000);
                    let _ = on_message
                        .send(serde_json::json!({"type": "Close", "data": {"code": code}}));
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
                    let _ = on_message
                        .send(serde_json::json!({"type": "Close", "data": {"code": 1006}}));
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
            let _ = on_message.send(serde_json::json!({"type": "Close", "data": {"code": 1006}}));
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
    Ok(id)
}

/// Send one binary or text frame on an established terminal socket.
#[tauri::command]
pub async fn native_terminal_send(
    state: State<'_, TerminalSocketState>,
    id: String,
    data: Vec<u8>,
    is_binary: bool,
) -> Result<(), String> {
    let message = if is_binary {
        WsMessage::Binary(data.into())
    } else {
        let text = String::from_utf8(data).map_err(|_| auth::safe_error("socket_text_invalid"))?;
        WsMessage::Text(text.into())
    };
    let mut sink = take_sink(&state, &id)?;
    let result = sink
        .send(message)
        .await
        .map_err(|_| auth::safe_error("socket_send_failed"));
    restore_sink(&state, &id, sink);
    result
}

/// Close an established terminal socket with the standard 1000 frame.
#[tauri::command]
pub async fn native_terminal_close(
    state: State<'_, TerminalSocketState>,
    id: String,
) -> Result<(), String> {
    let mut sink = take_sink(&state, &id)?;
    let result = sink
        .send(WsMessage::Close(None))
        .await
        .map_err(|_| auth::safe_error("socket_close_failed"));
    restore_sink(&state, &id, sink);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
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

    #[test]
    fn terminal_targets_must_be_issuer_same_origin_terms_path() {
        let issuer = "https://b.example";
        assert!(validate_terminal_targets(
            issuer,
            "https://b.example/api/v1/terms/term/terminal",
            "wss://b.example/api/v1/terms/term/terminal"
        )
        .is_ok());
        assert!(validate_terminal_targets(
            issuer,
            "https://attacker.example/api/v1/terms/term/terminal",
            "wss://b.example/api/v1/terms/term/terminal"
        )
        .is_err());
        assert!(validate_terminal_targets(
            issuer,
            "https://b.example/api/v1/terms/term/terminal",
            "wss://b.example/other"
        )
        .is_err());
        assert!(validate_terminal_targets(
            issuer,
            "https://b.example/",
            "wss://b.example/api/v1/terms/term/terminal"
        )
        .is_err());
    }
}
