//! Rust-owned Tauri Agent stream transport (M6 plan line 1017). The WebView
//! makes no network requests for the Agent stream: this command owns the SSE
//! long connection — it pins the target to the canonical issuer origin and
//! the `/api/` surface, signs DPoP locally, forwards complete SSE frames over
//! a Tauri IPC channel, and stops on a cancel command keyed by `request_id`.
//! The command's lifetime is exactly one connection; reconnect/backoff stays
//! in the JS-side `AgentStreamSession`.
//!
//! The DPoP/nonce handshake reuses `native_http_request`'s logic in
//! `auth.rs` (remembered nonce, 401 + `DPoP-Nonce` retry, nonce
//! remembering). Because this is a long connection the nonce retry only
//! happens at the 401 handshake, before the stream body is read — the DPoP
//! proof is signed once per attempt with htu = the stream URL (query
//! stripped, exactly as `dpop_proof` does for every other command).
//!
//! Channel payload contract (aligned with the client-core
//! `AgentStreamTransportEvent` union, minus the parse step):
//!
//! - `{"type":"open"}` — the 2xx handshake succeeded, before the body.
//! - `{"type":"event","data":<raw frame>}` — one complete SSE frame
//!   (`event:`/`data:` lines); the TS adapter applies
//!   `parseAgentStreamFrameAgui` to it, so Rust never parses untrusted
//!   server content and `reset`/server-`closed` frames travel as raw events.
//! - `{"type":"close","code":C,"reason":R}` — terminal, emitted by this side
//!   only (initial non-2xx status, EOF, or transport error). The TS adapter
//!   owns the at-most-one-terminal-frame guard, mirroring the browser
//!   transport's `finish`.

use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::sync::{Arc, Mutex};

use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use tauri::ipc::Channel;
use tauri::State;
use tokio::sync::Notify;

use crate::auth::{
    assert_http_target, canonical_issuer, current_access_token, safe_error, send_with_dpop_nonce,
    NativeAuthState,
};

/// The Agent stream endpoint (`termflow_control_plane.api.agent_stream`,
/// plan §13.1 / M6.2), pinned at compile time so the WebView cannot point
/// the stream anywhere outside the issuer's `/api/` surface.
pub const AGENT_STREAM_PATH: &str = "/api/v1/agent/stream";

/// WebView-supplied stream parameters: everything except the endpoint path
/// and the wire format, which are pinned by this module.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentStreamParams {
    /// Omit to subscribe to the global live stream (every conversation).
    pub conversation_id: Option<String>,
    /// Opaque `{epoch}-{seq}` cursor from the last delivered frame.
    pub cursor: Option<String>,
}

/// Build the pinned stream URL. The endpoint path is compile-time pinned
/// and the `wire=agui` parameter is forced here; only the optional
/// conversation/cursor values come from the WebView and they are
/// percent-encoded by `url::Url`'s query-pair serializer. The caller must
/// pass the canonical issuer origin.
pub fn build_stream_url(issuer: &str, params: &AgentStreamParams) -> Result<String, String> {
    let mut url = assert_http_target(issuer, AGENT_STREAM_PATH)?;
    {
        let mut query = url.query_pairs_mut();
        query.append_pair("wire", "agui");
        if let Some(value) = &params.conversation_id {
            query.append_pair("conversation_id", value);
        }
        if let Some(value) = &params.cursor {
            query.append_pair("cursor", value);
        }
    }
    Ok(url.to_string())
}

/// Initial HTTP status → terminal close frame, mirroring the browser
/// transport's `closeForStatus` (M6b spec §4.2). The session already owns
/// 4401 (authentication required, no reconnect) and 4412 (terminal
/// binding/conversation closure) semantics; other failures are transient
/// 1006 so the session may retry.
pub fn close_for_status(status: u16) -> (u16, &'static str) {
    match status {
        401 => (4401, "authentication_required"),
        403 => (4403, "forbidden"),
        404 => (4404, "conversation_not_found"),
        _ => (1006, "http_error"),
    }
}

/// Map a pre-handshake stream-command failure to a terminal close frame,
/// mirroring the browser transport's HTTP-status mapping. `authorization_required`
/// (no refreshable credential for the issuer) is the command-level analogue
/// of an HTTP 401: it must terminate as 4401 `authentication_required` so
/// the session goes to `onAuthenticationRequired` — surfacing it as a
/// command rejection would land in the adapter's transient 1006 path and
/// the session would enter the backoff-reconnect loop forever. Any other
/// failure stays a command rejection (`None`) and the adapter surfaces it
/// as a transient 1006 the session may retry.
fn close_for_stream_error(error: &str) -> Option<(u16, &'static str)> {
    match error {
        "authorization_required" => Some((4401, "authentication_required")),
        _ => None,
    }
}

/// One IPC channel payload. The shape mirrors the client-core
/// `AgentStreamTransportEvent` union.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum AgentStreamFrame {
    /// The HTTP handshake succeeded (2xx) before the first body chunk.
    Open,
    /// One complete raw SSE frame (the `event:`/`data:` lines).
    Event { data: String },
    /// Terminal frame from this side (initial non-2xx status, EOF, or a
    /// transport error); server-sent `closed` frames travel as raw `Event`
    /// frames instead.
    Close { code: u16, reason: &'static str },
}

/// Upper bound on one buffered SSE frame. B caps the projected event
/// payload at 64 KiB (`MAX_AGENT_EVENT_PAYLOAD_BYTES`), so a legitimate
/// frame — payload plus the `event:`/`data:` envelope and cursor — stays
/// far below this; 256 KiB is generous headroom while still bounding the
/// buffer if a server never sends a frame terminator (defense-in-depth:
/// the buffer must not grow without limit even though B is trusted).
const MAX_FRAME_BYTES: usize = 256 * 1024;

/// Incremental SSE frame splitter: buffers raw bytes and yields the raw
/// text of every complete frame as soon as its `\n\n` terminator arrives,
/// mirroring the browser transport's line buffering (M6b spec §4.2).
/// Bytes are buffered raw so a UTF-8 character split across chunks decodes
/// correctly; frames that are empty or not valid UTF-8 are dropped without
/// ever being forwarded (the TS parser would reject them anyway, and the
/// content is untrusted). An unterminated frame beyond `MAX_FRAME_BYTES` is
/// dropped whole — it can never complete — and the splitter resynchronizes
/// at the next terminator.
#[derive(Default)]
pub struct SseFrameSplitter {
    buffer: Vec<u8>,
}

impl SseFrameSplitter {
    pub fn new() -> Self {
        Self::default()
    }

    /// Append one body chunk and return every complete frame it completed.
    /// A partial frame stays buffered until its `\n\n` terminator arrives.
    pub fn push(&mut self, chunk: &[u8]) -> Vec<String> {
        self.buffer.extend_from_slice(chunk);
        let mut frames = Vec::new();
        while let Some(boundary) = self.buffer.windows(2).position(|window| window == b"\n\n") {
            let frame = self.buffer.drain(..boundary).collect::<Vec<u8>>();
            self.buffer.drain(..2); // the `\n\n` separator
            if frame.is_empty() {
                continue;
            }
            if let Ok(text) = String::from_utf8(frame) {
                frames.push(text);
            }
        }
        // An unterminated frame beyond the cap can never be completed as a
        // valid frame: drop the buffered tail so the next terminator
        // resynchronizes and the buffer stays bounded no matter how many
        // chunks a server streams without ever closing a frame.
        if self.buffer.len() > MAX_FRAME_BYTES {
            self.buffer.clear();
        }
        frames
    }
}

/// Upper bound on remembered cancel tombstones. A tombstone only matters
/// until the racing stream command registers — the two IPC commands are
/// dispatched concurrently, so the window is one tokio scheduling turn at
/// most. Anything older is stale by construction: request ids are random
/// UUIDs that are never reused, so only a same-id register could consume a
/// tombstone and that register has long since either run or not. FIFO
/// eviction keeps the set small while the newest cancels — the only ones
/// that can still race a registration — stay remembered.
const CANCELLED_TOMBSTONE_CAP: usize = 32;

/// Cancellation registry for in-flight agent streams. `native_agent_stream`
/// registers a `Notify` under its `request_id` and removes it on exit;
/// `native_agent_stream_cancel` wakes it so the pump loop (and handshake)
/// drop the connection.
///
/// A cancel that arrives before the stream command has registered is
/// recorded as a tombstone so the (late) registration fails closed instead
/// of opening a connection that can never be cancelled — the two IPC
/// commands are scheduled concurrently by tokio, so the ordering is not
/// guaranteed. The tombstone set is bounded (see `CANCELLED_TOMBSTONE_CAP`)
/// so repeated cancels of finished/unknown streams cannot grow the registry
/// without limit.
#[derive(Default)]
pub struct AgentStreamState {
    /// One lock for both maps so register/cancel/unregister are atomic with
    /// respect to each other: a cancel either notifies a registered stream
    /// or records a tombstone, and a register either consumes a tombstone
    /// or opens — a cancel can never be lost in between.
    registry: Mutex<AgentStreamRegistry>,
}

#[derive(Default)]
struct AgentStreamRegistry {
    streams: HashMap<String, Arc<Notify>>,
    /// Cancel tombstones in FIFO insertion order, bounded by
    /// `CANCELLED_TOMBSTONE_CAP`.
    cancelled: VecDeque<String>,
}

impl AgentStreamState {
    fn register(&self, request_id: &str, cancel: Arc<Notify>) -> Result<(), String> {
        let mut registry = self
            .registry
            .lock()
            .map_err(|_| safe_error("stream_state_unavailable"))?;
        if registry.streams.contains_key(request_id) {
            return Err(safe_error("stream_already_running"));
        }
        if let Some(index) = registry.cancelled.iter().position(|id| id == request_id) {
            // A cancel arrived before registration: never open the
            // connection. The JS adapter's terminal-frame guard already
            // absorbed the close frame, so this rejection is silent there.
            registry.cancelled.remove(index);
            return Err(safe_error("stream_cancelled"));
        }
        registry.streams.insert(request_id.to_owned(), cancel);
        Ok(())
    }

    fn unregister(&self, request_id: &str) {
        if let Ok(mut registry) = self.registry.lock() {
            registry.streams.remove(request_id);
            // Defensive: with one lock a registered stream can never have a
            // same-id tombstone, but releasing an id fully keeps the
            // invariant local instead of relying on the callers.
            if let Some(index) = registry.cancelled.iter().position(|id| id == request_id) {
                registry.cancelled.remove(index);
            }
        }
    }

    fn cancel(&self, request_id: &str) {
        let Ok(mut registry) = self.registry.lock() else {
            return;
        };
        if let Some(cancel) = registry.streams.get(request_id) {
            cancel.notify_one();
            return;
        }
        // Not registered (yet): remember a tombstone so a racing register
        // fails closed. The set is bounded: ids are never reused, so a
        // tombstone only matters for the one concurrent register it races;
        // FIFO eviction drops the oldest — guaranteed stale — entries first
        // and duplicate cancels of the same id collapse into one entry.
        if registry.cancelled.iter().any(|id| id == request_id) {
            return;
        }
        if registry.cancelled.len() >= CANCELLED_TOMBSTONE_CAP {
            registry.cancelled.pop_front();
        }
        registry.cancelled.push_back(request_id.to_owned());
    }
}

/// Pure request assembly for the stream GET: DPoP authorization + DPoP
/// proof headers and an event-stream accept header. Exposed for the
/// contract tests under `tests/`.
pub fn build_stream_request(
    http: &reqwest::Client,
    url: &str,
    access_token: &str,
    proof: &str,
) -> reqwest::RequestBuilder {
    http.get(url)
        .header("accept", "text/event-stream")
        .header("Authorization", format!("DPoP {access_token}"))
        .header("DPoP", proof)
}

/// Race one future against the cancel signal. `None` means the stream was
/// cancelled before the future resolved.
async fn cancel_guard<T>(cancel: &Arc<Notify>, future: impl Future<Output = T>) -> Option<T> {
    tokio::select! {
        _ = cancel.notified() => None,
        value = future => Some(value),
    }
}

/// The only WebView-visible Agent stream channel. The command resolves when
/// the connection ends (normally, on transport error, or on cancel); the
/// channel carries `open`/`event`/`close` frames throughout.
#[tauri::command]
pub async fn native_agent_stream(
    state: State<'_, NativeAuthState>,
    stream_state: State<'_, AgentStreamState>,
    issuer: String,
    url_params: AgentStreamParams,
    channel: Channel<AgentStreamFrame>,
    request_id: String,
) -> Result<(), String> {
    let issuer = canonical_issuer(&issuer)?;
    let url = build_stream_url(&issuer, &url_params)?;
    let cancel = Arc::new(Notify::new());
    stream_state.register(&request_id, cancel.clone())?;
    let result = run_stream(state.inner(), &issuer, &url, &channel, &cancel).await;
    stream_state.unregister(&request_id);
    result
}

/// The connection body: DPoP handshake, then frame pumping. Runs entirely
/// inside the command so the command's lifetime is exactly one connection.
async fn run_stream(
    state: &NativeAuthState,
    issuer: &str,
    url: &str,
    channel: &Channel<AgentStreamFrame>,
    cancel: &Arc<Notify>,
) -> Result<(), String> {
    let access_token = match current_access_token(state, issuer).await {
        Ok(token) => token,
        // Unauthenticated is terminal: emit the same 4401 close the HTTP
        // 401 path produces instead of rejecting, which the adapter would
        // surface as a transient 1006 and retry forever.
        Err(error) => match close_for_stream_error(&error) {
            Some((code, reason)) => {
                let _ = channel.send(AgentStreamFrame::Close { code, reason });
                return Ok(());
            }
            None => return Err(error),
        },
    };
    // The shared sender owns the remembered nonce and the bounded
    // `use_dpop_nonce` retry loop, so this handshake cannot drift from the
    // HTTP and token paths (M6b spec: one nonce policy per client).
    let response = match cancel_guard(
        cancel,
        send_with_dpop_nonce(state, issuer, "GET", url, Some(&access_token), |proof| {
            Ok(build_stream_request(&state.http, url, &access_token, proof))
        }),
    )
    .await
    {
        None => return Ok(()), // cancelled before the connection resolved
        Some(response) => response.map_err(|_| safe_error("offline"))?,
    };

    if !response.status().is_success() {
        let status = response.status().as_u16();
        let (code, reason) = if status == 401 && response.headers().get("DPoP-Nonce").is_some() {
            // Retries were exhausted on freshness challenges, not a dead
            // session: the fresh nonce is already remembered, so reconnect
            // (transient 1006) instead of clearing the session (4401).
            (1006, "nonce_retry_exhausted")
        } else {
            close_for_status(status)
        };
        let _ = channel.send(AgentStreamFrame::Close { code, reason });
        return Ok(());
    }
    if channel.send(AgentStreamFrame::Open).is_err() {
        return Ok(());
    }
    pump(channel, cancel, response).await;
    Ok(())
}

/// Read the SSE body, splitting frames and forwarding them over the
/// channel. An explicit cancel is silent (mirror of the browser transport's
/// `close()`); a body error or an EOF without a server `closed` frame is a
/// transient transport error.
async fn pump(
    channel: &Channel<AgentStreamFrame>,
    cancel: &Arc<Notify>,
    response: reqwest::Response,
) {
    let mut stream = response.bytes_stream();
    let mut splitter = SseFrameSplitter::new();
    'pump: loop {
        let chunk = tokio::select! {
            _ = cancel.notified() => None,
            chunk = stream.next() => Some(chunk),
        };
        match chunk {
            None => break, // cancelled
            Some(Some(Ok(bytes))) => {
                for frame in splitter.push(&bytes) {
                    if channel
                        .send(AgentStreamFrame::Event { data: frame })
                        .is_err()
                    {
                        // The JS side dropped the channel: treat as cancel.
                        break 'pump;
                    }
                }
            }
            Some(Some(Err(_))) => {
                let _ = channel.send(AgentStreamFrame::Close {
                    code: 1006,
                    reason: "transport_error",
                });
                break;
            }
            Some(None) => {
                let _ = channel.send(AgentStreamFrame::Close {
                    code: 1006,
                    reason: "transport_error",
                });
                break;
            }
        }
    }
}

/// Cancel an in-flight agent stream by `request_id`. Idempotent and safe
/// for the JS adapter's close() to call unconditionally: a registered
/// stream is notified, and a finished or unknown id has no effect on any
/// live connection (an unknown id is remembered as a bounded tombstone so
/// a racing register fails closed).
#[tauri::command]
pub fn native_agent_stream_cancel(
    stream_state: State<'_, AgentStreamState>,
    request_id: String,
) -> Result<(), String> {
    stream_state.cancel(&request_id);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
    use futures_util::FutureExt;
    use p256::ecdsa::SigningKey;

    use crate::auth::dpop_proof;

    #[test]
    fn stream_url_pins_the_endpoint_and_forces_the_agui_wire() {
        let url = build_stream_url("https://b.example", &AgentStreamParams::default()).unwrap();
        assert_eq!(url, "https://b.example/api/v1/agent/stream?wire=agui");
    }

    #[test]
    fn stream_url_appends_encoded_conversation_and_cursor_values() {
        let params = AgentStreamParams {
            conversation_id: Some("11111111-1111-4111-8111-111111111111".to_owned()),
            cursor: Some("7-3".to_owned()),
        };
        let url = build_stream_url("https://b.example", &params).unwrap();
        assert!(url.contains("wire=agui"));
        assert!(url.contains("conversation_id=11111111-1111-4111-8111-111111111111"));
        assert!(url.contains("cursor=7-3"));
    }

    #[test]
    fn stream_url_percent_encodes_hostile_parameter_values() {
        let params = AgentStreamParams {
            conversation_id: Some("x&y=z".to_owned()),
            cursor: Some("1-2/3+4".to_owned()),
        };
        let url = build_stream_url("https://b.example", &params).unwrap();
        assert!(url.contains("conversation_id=x%26y%3Dz"), "{url}");
        assert!(url.contains("cursor=1-2%2F3%2B4"), "{url}");
        assert!(
            !url.contains("conversation_id=x&y"),
            "raw separators must not survive: {url}"
        );
    }

    #[test]
    fn close_codes_map_http_statuses_like_the_web_transport() {
        assert_eq!(close_for_status(401), (4401, "authentication_required"));
        assert_eq!(close_for_status(403), (4403, "forbidden"));
        assert_eq!(close_for_status(404), (4404, "conversation_not_found"));
        assert_eq!(close_for_status(400), (1006, "http_error"));
        assert_eq!(close_for_status(500), (1006, "http_error"));
        assert_eq!(close_for_status(503), (1006, "http_error"));
        assert_eq!(close_for_status(0), (1006, "http_error"));
    }

    #[test]
    fn unauthenticated_command_failures_map_to_the_terminal_4401_close() {
        // The token failure is the command-level analogue of an HTTP 401:
        // identical terminal close, so the session goes to
        // onAuthenticationRequired instead of the transient-1006 retry loop.
        assert_eq!(
            close_for_stream_error("authorization_required"),
            Some(close_for_status(401))
        );
        // Every other failure stays a command rejection (transient 1006).
        assert_eq!(close_for_stream_error("offline"), None);
        assert_eq!(close_for_stream_error("stream_state_unavailable"), None);
        assert_eq!(close_for_stream_error("issuer_invalid"), None);
        assert_eq!(close_for_stream_error(""), None);
    }

    #[test]
    fn splitter_returns_each_complete_frame_and_keeps_the_rest_buffered() {
        let mut splitter = SseFrameSplitter::new();
        // Two complete frames in one chunk (sticky packets).
        let frames = splitter.push(b"event: a\ndata: {\"x\":1}\n\nevent: b\ndata: {\"x\":2}\n\n");
        assert_eq!(
            frames,
            vec!["event: a\ndata: {\"x\":1}", "event: b\ndata: {\"x\":2}"]
        );
        // A partial frame stays buffered until its terminator arrives.
        assert!(splitter.push(b"event: c\ndata: {}").is_empty());
        assert_eq!(splitter.push(b"\n\n"), vec!["event: c\ndata: {}"]);
    }

    #[test]
    fn splitter_reassembles_frames_split_across_chunks() {
        let mut splitter = SseFrameSplitter::new();
        assert!(splitter.push(b"event: a\nda").is_empty());
        assert!(splitter.push(b"ta: {\"x\":1}\n").is_empty());
        // The `\n\n` separator itself is split across two chunks.
        assert_eq!(splitter.push(b"\n"), vec!["event: a\ndata: {\"x\":1}"]);
    }

    #[test]
    fn splitter_decodes_multibyte_characters_across_chunk_boundaries() {
        let mut splitter = SseFrameSplitter::new();
        let mut received = Vec::new();
        // Worst case: one byte per chunk, splitting UTF-8 sequences.
        for byte in "data: 中文\n\n".as_bytes() {
            received.extend(splitter.push(std::slice::from_ref(byte)));
        }
        assert_eq!(received, vec!["data: 中文"]);
    }

    #[test]
    fn splitter_drops_empty_and_non_utf8_frames() {
        let mut splitter = SseFrameSplitter::new();
        assert_eq!(splitter.push(b"\n\n"), Vec::<String>::new());
        assert_eq!(splitter.push(b"\xff\xfe\n\n"), Vec::<String>::new());
        assert_eq!(
            splitter.push(b"event: ok\ndata: {}\n\n"),
            vec!["event: ok\ndata: {}"]
        );
    }

    #[test]
    fn splitter_keeps_single_newlines_inside_one_frame() {
        let mut splitter = SseFrameSplitter::new();
        let frames = splitter.push(b"event: agent_event\ndata: {\"cursor\":\"7-3\"}\n\n");
        assert_eq!(
            frames,
            vec!["event: agent_event\ndata: {\"cursor\":\"7-3\"}"]
        );
    }

    #[test]
    fn splitter_drops_unterminated_frames_beyond_the_cap_and_resynchronizes() {
        let mut splitter = SseFrameSplitter::new();
        let half = vec![b'x'; MAX_FRAME_BYTES / 2];
        assert!(splitter.push(&half).is_empty());
        assert!(splitter.push(&half).is_empty());
        // The unterminated frame is over the cap: it is dropped whole and
        // the buffer stays bounded no matter how many chunks follow without
        // a terminator.
        for _ in 0..16 {
            assert!(splitter.push(&half).is_empty());
            assert!(splitter.buffer.len() <= MAX_FRAME_BYTES);
        }
        // The next terminator resynchronizes: subsequent frames are intact.
        assert_eq!(
            splitter.push(b"event: a\ndata: {}\n\n"),
            vec!["event: a\ndata: {}"]
        );
    }

    #[test]
    fn channel_frames_serialize_to_the_agent_stream_transport_shape() {
        assert_eq!(
            serde_json::to_value(AgentStreamFrame::Open).unwrap(),
            serde_json::json!({"type": "open"})
        );
        assert_eq!(
            serde_json::to_value(AgentStreamFrame::Event {
                data: "event: a\ndata: {}".to_owned()
            })
            .unwrap(),
            serde_json::json!({"type": "event", "data": "event: a\ndata: {}"})
        );
        assert_eq!(
            serde_json::to_value(AgentStreamFrame::Close {
                code: 4401,
                reason: "authentication_required"
            })
            .unwrap(),
            serde_json::json!({"type": "close", "code": 4401, "reason": "authentication_required"})
        );
    }

    #[test]
    fn cancel_before_register_tombstones_and_the_late_register_fails_closed() {
        let state = AgentStreamState::default();
        // A cancel that races ahead of the stream command is remembered.
        state.cancel("req-1");
        // The late registration must fail closed instead of opening an
        // uncancellable connection.
        let cancel = Arc::new(Notify::new());
        assert_eq!(
            state.register("req-1", cancel),
            Err("stream_cancelled".to_owned())
        );
        // The tombstone is consumed, so a subsequent legit stream with the
        // same id can register.
        assert!(state.register("req-1", Arc::new(Notify::new())).is_ok());
    }

    #[test]
    fn cancel_notifies_a_registered_stream_and_unregister_clears_it() {
        let state = AgentStreamState::default();
        let cancel = Arc::new(Notify::new());
        state.register("req-2", cancel.clone()).unwrap();
        state.cancel("req-2");
        assert!(cancel.notified().now_or_never().is_some());
        state.unregister("req-2");
        // After unregister a cancel becomes a tombstone again (id reused).
        state.cancel("req-2");
        assert!(state.register("req-2", Arc::new(Notify::new())).is_err());
    }

    #[test]
    fn cancel_after_unregister_is_bounded_by_fifo_tombstone_eviction() {
        let state = AgentStreamState::default();
        // Repeated dispose-vs-finished-stream cycles: every cancel lands on
        // an unknown id (fresh UUID, never reused), so each one would leave
        // a permanent tombstone in an unbounded registry.
        for index in 0..(CANCELLED_TOMBSTONE_CAP * 4) {
            let id = format!("req-{index}");
            state.register(&id, Arc::new(Notify::new())).unwrap();
            state.unregister(&id);
            state.cancel(&id);
        }
        let cancelled_len = state.registry.lock().unwrap().cancelled.len();
        assert_eq!(
            cancelled_len, CANCELLED_TOMBSTONE_CAP,
            "the tombstone registry must stay bounded across cancel-after-unregister cycles"
        );
        // Duplicate cancels of one id collapse into a single entry.
        state.cancel("req-dup");
        state.cancel("req-dup");
        assert_eq!(
            state.registry.lock().unwrap().cancelled.len(),
            CANCELLED_TOMBSTONE_CAP
        );
    }

    #[test]
    fn fifo_eviction_keeps_only_the_newest_tombstones() {
        let state = AgentStreamState::default();
        // Fill the tombstone registry to capacity with stale cancels.
        for index in 0..CANCELLED_TOMBSTONE_CAP {
            state.cancel(&format!("req-{index}"));
        }
        // The newest cancel must survive the eviction and still fail the
        // racing register closed — the race protection stays intact.
        state.cancel("req-newest");
        assert_eq!(
            state.register("req-newest", Arc::new(Notify::new())),
            Err("stream_cancelled".to_owned())
        );
        // The oldest tombstone was evicted first: its register is allowed.
        assert!(state.register("req-0", Arc::new(Notify::new())).is_ok());
    }

    #[test]
    fn stream_dpop_proof_binds_get_and_the_query_free_stream_url() {
        let key = SigningKey::random(&mut p256::elliptic_curve::rand_core::OsRng);
        let target = format!("https://b.example{AGENT_STREAM_PATH}?wire=agui");
        let proof = dpop_proof(
            &key,
            "GET",
            &target,
            Some("challenge"),
            Some("access-value"),
        )
        .unwrap();
        let mut segments = proof.split('.');
        let _header = segments.next().unwrap();
        let payload = segments.next().unwrap();
        let _signature = segments.next().unwrap();
        assert!(segments.next().is_none(), "proof is a three-part JWT");
        let claims: serde_json::Value =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(payload).unwrap()).unwrap();
        assert_eq!(claims["htm"], "GET");
        assert_eq!(
            claims["htu"],
            format!("https://b.example{AGENT_STREAM_PATH}"),
            "htu must be the query-free stream URL"
        );
        assert_eq!(claims["nonce"], "challenge");
        assert!(claims["ath"].is_string(), "access-token hash is bound");
        assert!(!proof.contains("access-value"));
    }
}
