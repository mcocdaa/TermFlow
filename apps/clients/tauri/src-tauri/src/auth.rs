use std::{
    collections::HashMap,
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
#[cfg(not(any(target_os = "ios", target_os = "android")))]
use keyring::{Entry as KeyringEntry, Error as KeyringError};
#[cfg(any(target_os = "ios", target_os = "android"))]
use keyring_core::{Entry as KeyringEntry, Error as KeyringError};
use p256::{
    ecdsa::{signature::Signer, Signature, SigningKey},
    pkcs8::{DecodePrivateKey, EncodePrivateKey},
};
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::State;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use url::Url;

#[cfg(any(target_os = "ios", target_os = "android"))]
use std::sync::OnceLock;

const KEYRING_SERVICE: &str = "io.termflow.client";
const ACCESS_EARLY_SECONDS: i64 = 60;

/// The protocol only accepts loopback HTTP callbacks on explicit ephemeral
/// ports, so the listener must bind inside that range for the browser handoff
/// to stay within the server-approved surface.
const LOOPBACK_PORT_MIN: u16 = 49_152;
const LOOPBACK_PORT_MAX: u16 = 65_535;
const CALLBACK_ACCEPT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(300);
const CALLBACK_HEAD_MAX_BYTES: usize = 8192;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PublicJwk {
    kty: String,
    crv: String,
    alg: String,
    x: String,
    y: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AccessCredential {
    access_token: String,
    expires_at: String,
    token_type: &'static str,
}

#[derive(Debug, Clone)]
struct AccessState {
    access_token: String,
    expires_at_unix: i64,
}

#[derive(Default)]
pub struct NativeAuthState {
    access: Mutex<HashMap<String, AccessState>>,
    nonces: Mutex<HashMap<String, String>>,
    callback_listeners: Mutex<HashMap<String, PendingCallbackListener>>,
    refresh_gate: tokio::sync::Mutex<()>,
    http: Client,
}

/// A bound loopback callback listener waiting for the browser handoff. The
/// receiver is awaited by the JS session; the task handle lets a cancelled
/// session release the port immediately.
struct PendingCallbackListener {
    receiver: tokio::sync::oneshot::Receiver<String>,
    task: tauri::async_runtime::JoinHandle<()>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TokenResponse {
    access_token: String,
    refresh_token: String,
    expires_in: i64,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NativeAuthorizationExchangeRequest {
    issuer: String,
    transaction_id: String,
    code_verifier: String,
    redirect_uri: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NativeDeviceExchangeRequest {
    issuer: String,
    device_code: String,
    code_verifier: String,
    public_jwk: PublicJwk,
}

#[derive(Debug, Serialize)]
pub struct NativeHeaders {
    pub(crate) authorization: String,
    pub(crate) dpop: String,
}

pub(crate) fn safe_error(code: &str) -> String {
    code.to_owned()
}
fn now_unix() -> Result<i64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs() as i64)
        .map_err(|_| safe_error("clock_invalid"))
}
pub(crate) fn canonical_issuer(value: &str) -> Result<String, String> {
    let url = Url::parse(value).map_err(|_| safe_error("issuer_invalid"))?;
    if !matches!(url.scheme(), "http" | "https")
        || url.username() != ""
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(safe_error("issuer_invalid"));
    }
    if url.scheme() == "http" && !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "::1"))
    {
        return Err(safe_error("https_required"));
    }
    Ok(url.origin().ascii_serialization())
}

fn assert_api_target(issuer: &str, target: &str) -> Result<Url, String> {
    let url = Url::parse(target).map_err(|_| safe_error("url_invalid"))?;
    if url.username() != "" || url.password().is_some() {
        return Err(safe_error("url_not_allowed"));
    }
    let base = Url::parse(issuer).map_err(|_| safe_error("issuer_invalid"))?;
    if url.origin() != base.origin() {
        return Err(safe_error("url_not_allowed"));
    }
    if !url.path().starts_with("/api/") {
        return Err(safe_error("url_not_allowed"));
    }
    Ok(url)
}

fn validate_dpop_signing_input(input: &[u8]) -> Result<(), String> {
    let text = std::str::from_utf8(input).map_err(|_| safe_error("signing_input_invalid"))?;
    let mut parts = text.split('.');
    let header_segment = parts.next().ok_or(safe_error("signing_input_invalid"))?;
    let payload_segment = parts.next().ok_or(safe_error("signing_input_invalid"))?;
    if parts.next().is_some() {
        return Err(safe_error("signing_input_invalid"));
    }
    let header = decode_jwt_segment(header_segment)?;
    if header.get("typ").and_then(Value::as_str) != Some("dpop+jwt")
        || header.get("alg").and_then(Value::as_str) != Some("ES256")
    {
        return Err(safe_error("signing_input_invalid"));
    }
    let payload = decode_jwt_segment(payload_segment)?;
    for field in ["jti", "htm", "htu"] {
        if !payload.get(field).and_then(Value::as_str).is_some() {
            return Err(safe_error("signing_input_invalid"));
        }
    }
    Ok(())
}

fn decode_jwt_segment(segment: &str) -> Result<Value, String> {
    let bytes = URL_SAFE_NO_PAD
        .decode(segment)
        .map_err(|_| safe_error("signing_input_invalid"))?;
    serde_json::from_slice(&bytes).map_err(|_| safe_error("signing_input_invalid"))
}
fn issuer_key(issuer: &str, kind: &str) -> String {
    format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(Sha256::digest(issuer.as_bytes())),
        kind
    )
}

#[cfg(any(target_os = "ios", target_os = "android"))]
fn initialize_mobile_keyring() -> Result<(), String> {
    static INITIALIZED: OnceLock<Result<(), String>> = OnceLock::new();
    INITIALIZED
        .get_or_init(|| {
            #[cfg(target_os = "ios")]
            let store = apple_native_keyring_store::protected::Store::new();
            #[cfg(target_os = "android")]
            let store = android_native_keyring_store::Store::new();

            store
                .map(|store| keyring_core::set_default_store(store))
                .map_err(|_| safe_error("secure_store_unavailable"))
        })
        .clone()
}

#[cfg(not(any(target_os = "ios", target_os = "android")))]
fn initialize_mobile_keyring() -> Result<(), String> {
    Ok(())
}

fn keyring_entry(issuer: &str, kind: &str) -> Result<KeyringEntry, String> {
    initialize_mobile_keyring()?;
    KeyringEntry::new(KEYRING_SERVICE, &issuer_key(issuer, kind))
        .map_err(|_| safe_error("secure_store_unavailable"))
}

fn remember_dpop_nonce(state: &NativeAuthState, issuer: &str, nonce: &str) -> Result<(), String> {
    if nonce.is_empty() || nonce.len() > 1024 || nonce.chars().any(char::is_whitespace) {
        return Err(safe_error("dpop_nonce_invalid"));
    }
    state
        .nonces
        .lock()
        .map_err(|_| safe_error("auth_state_unavailable"))?
        .insert(issuer.to_owned(), nonce.to_owned());
    Ok(())
}

fn remembered_dpop_nonce(state: &NativeAuthState, issuer: &str) -> Result<Option<String>, String> {
    Ok(state
        .nonces
        .lock()
        .map_err(|_| safe_error("auth_state_unavailable"))?
        .get(issuer)
        .cloned())
}

fn token_error_revokes_credentials(body: &[u8]) -> bool {
    serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|value| value.get("error").cloned())
        .and_then(|value| value.get("code").and_then(Value::as_str).map(str::to_owned))
        .is_some_and(|code| code == "invalid_grant")
}

fn token_error_code(body: &[u8]) -> Option<String> {
    serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|value| value.get("error").cloned())
        .and_then(|value| value.get("code").and_then(Value::as_str).map(str::to_owned))
}

fn clear_cached_credentials(state: &NativeAuthState, issuer: &str) -> Result<(), String> {
    state
        .access
        .lock()
        .map_err(|_| safe_error("auth_state_unavailable"))?
        .remove(issuer);
    state
        .nonces
        .lock()
        .map_err(|_| safe_error("auth_state_unavailable"))?
        .remove(issuer);
    Ok(())
}

fn clear_native_credentials(state: &NativeAuthState, issuer: &str) -> Result<(), String> {
    clear_cached_credentials(state, issuer)?;
    match keyring_entry(issuer, "refresh")?.delete_credential() {
        Ok(()) | Err(KeyringError::NoEntry) => Ok(()),
        Err(_) => Err(safe_error("secure_store_unavailable")),
    }
}

fn signing_key(issuer: &str) -> Result<SigningKey, String> {
    let entry = keyring_entry(issuer, "p256")?;
    match entry.get_secret() {
        Ok(bytes) => {
            SigningKey::from_pkcs8_der(&bytes).map_err(|_| safe_error("device_key_invalid"))
        }
        Err(KeyringError::NoEntry) => {
            let key = SigningKey::random(&mut p256::elliptic_curve::rand_core::OsRng);
            let bytes = key
                .to_pkcs8_der()
                .map_err(|_| safe_error("device_key_invalid"))?;
            entry
                .set_secret(bytes.as_bytes())
                .map_err(|_| safe_error("secure_store_unavailable"))?;
            Ok(key)
        }
        Err(_) => Err(safe_error("secure_store_unavailable")),
    }
}
fn public_jwk(key: &SigningKey) -> Result<PublicJwk, String> {
    let point = key.verifying_key().to_encoded_point(false);
    let x = point.x().ok_or_else(|| safe_error("device_key_invalid"))?;
    let y = point.y().ok_or_else(|| safe_error("device_key_invalid"))?;
    Ok(PublicJwk {
        kty: "EC".to_owned(),
        crv: "P-256".to_owned(),
        alg: "ES256".to_owned(),
        x: URL_SAFE_NO_PAD.encode(x),
        y: URL_SAFE_NO_PAD.encode(y),
    })
}
fn thumbprint(jwk: &PublicJwk) -> String {
    let canonical = format!(
        r#"{{"crv":"P-256","kty":"EC","x":"{}","y":"{}"}}"#,
        jwk.x, jwk.y
    );
    URL_SAFE_NO_PAD.encode(Sha256::digest(canonical.as_bytes()))
}
fn jwt_segment(value: &Value) -> Result<String, String> {
    serde_json::to_vec(value)
        .map(|bytes| URL_SAFE_NO_PAD.encode(bytes))
        .map_err(|_| safe_error("dpop_encoding_failed"))
}
fn dpop_proof(
    key: &SigningKey,
    method: &str,
    target: &str,
    nonce: Option<&str>,
    access_token: Option<&str>,
) -> Result<String, String> {
    let mut url = Url::parse(target).map_err(|_| safe_error("dpop_url_invalid"))?;
    url.set_query(None);
    url.set_fragment(None);
    let jwk = public_jwk(key)?;
    let header = jwt_segment(&json!({"typ":"dpop+jwt","alg":"ES256","jwk":jwk}))?;
    let mut claims = json!({"jti":uuid::Uuid::new_v4().to_string(),"htm":method.to_uppercase(),"htu":url.as_str(),"iat":now_unix()?});
    if let Some(value) = nonce {
        claims["nonce"] = Value::String(value.to_owned());
    }
    if let Some(value) = access_token {
        claims["ath"] = Value::String(URL_SAFE_NO_PAD.encode(Sha256::digest(value.as_bytes())));
    }
    let payload = jwt_segment(&claims)?;
    let input = format!("{header}.{payload}");
    let signature: Signature = key.sign(input.as_bytes());
    Ok(format!(
        "{input}.{}",
        URL_SAFE_NO_PAD.encode(signature.to_bytes())
    ))
}
fn access_credential(value: &AccessState) -> AccessCredential {
    let timestamp = time::OffsetDateTime::from_unix_timestamp(value.expires_at_unix)
        .unwrap_or(time::OffsetDateTime::UNIX_EPOCH);
    AccessCredential {
        access_token: value.access_token.clone(),
        expires_at: timestamp
            .format(&time::format_description::well_known::Rfc3339)
            .unwrap_or_default(),
        token_type: "DPoP",
    }
}
async fn token_request(
    state: &NativeAuthState,
    issuer: &str,
    body: Value,
) -> Result<TokenResponse, String> {
    let key = signing_key(issuer)?;
    let endpoint = format!("{issuer}/api/v1/oauth/token");
    let first_proof = dpop_proof(&key, "POST", &endpoint, None, None)?;
    let first = state
        .http
        .post(&endpoint)
        .header("DPoP", first_proof)
        .json(&body)
        .send()
        .await
        .map_err(|_| safe_error("token_exchange_failed"))?;
    let response = if first.status() == StatusCode::UNAUTHORIZED {
        if let Some(nonce) = first
            .headers()
            .get("DPoP-Nonce")
            .and_then(|value| value.to_str().ok())
        {
            let proof = dpop_proof(&key, "POST", &endpoint, Some(nonce), None)?;
            state
                .http
                .post(&endpoint)
                .header("DPoP", proof)
                .json(&body)
                .send()
                .await
                .map_err(|_| safe_error("token_exchange_failed"))?
        } else {
            first
        }
    } else {
        first
    };
    // Remember any nonce the server issued, including on non-success responses
    // such as authorization_pending.  Without this the next poll repeats the
    // 401 nonce challenge, doubling requests per poll and exhausting the
    // server's auth budget before the user can approve the device grant.
    if let Some(nonce) = response
        .headers()
        .get("DPoP-Nonce")
        .and_then(|value| value.to_str().ok())
    {
        remember_dpop_nonce(state, issuer, nonce)?;
    }
    if !response.status().is_success() {
        let body = response.bytes().await.unwrap_or_default();
        if token_error_revokes_credentials(&body) {
            return Err(safe_error("invalid_grant"));
        }
        if let Some(code) = token_error_code(&body) {
            return Err(code);
        }
        return Err(safe_error("authorization_required"));
    }
    response
        .json::<TokenResponse>()
        .await
        .map_err(|_| safe_error("token_response_invalid"))
}
async fn store_token_response(
    state: &NativeAuthState,
    issuer: &str,
    response: TokenResponse,
) -> Result<AccessCredential, String> {
    keyring_entry(issuer, "refresh")?
        .set_secret(response.refresh_token.as_bytes())
        .map_err(|_| safe_error("secure_store_unavailable"))?;
    let access = AccessState {
        access_token: response.access_token,
        expires_at_unix: now_unix()? + response.expires_in,
    };
    let result = access_credential(&access);
    state
        .access
        .lock()
        .map_err(|_| safe_error("auth_state_unavailable"))?
        .insert(issuer.to_owned(), access);
    Ok(result)
}
async fn refresh_access(state: &NativeAuthState, issuer: &str) -> Result<AccessCredential, String> {
    let refresh = keyring_entry(issuer, "refresh")?
        .get_secret()
        .map_err(|_| safe_error("authorization_required"))?;
    let value = String::from_utf8(refresh).map_err(|_| safe_error("authorization_required"))?;
    let response = match token_request(state, issuer, json!({"grant_type":"refresh_token","refresh_token":value,"public_jwk":public_jwk(&signing_key(issuer)?)?})).await {
        Ok(response) => response,
        Err(error) if error == "invalid_grant" => {
            clear_native_credentials(state, issuer)?;
            return Err(safe_error("authorization_required"));
        }
        Err(error) => return Err(error),
    };
    store_token_response(state, issuer, response).await
}

fn access_is_stale(state: &NativeAuthState, issuer: &str) -> Result<bool, String> {
    let values = state
        .access
        .lock()
        .map_err(|_| safe_error("auth_state_unavailable"))?;
    Ok(values
        .get(issuer)
        .map(|value| value.expires_at_unix - ACCESS_EARLY_SECONDS <= now_unix().unwrap_or(i64::MAX))
        .unwrap_or(true))
}

async fn current_access(state: &NativeAuthState, issuer: &str) -> Result<AccessState, String> {
    if access_is_stale(state, issuer)? {
        let _refresh_guard = state.refresh_gate.lock().await;
        if access_is_stale(state, issuer)? {
            refresh_access(state, issuer).await?;
        }
    }
    state
        .access
        .lock()
        .map_err(|_| safe_error("auth_state_unavailable"))?
        .get(issuer)
        .cloned()
        .ok_or_else(|| safe_error("authorization_required"))
}

#[tauri::command]
pub fn native_public_jwk(issuer: String) -> Result<PublicJwk, String> {
    let issuer = canonical_issuer(&issuer)?;
    public_jwk(&signing_key(&issuer)?)
}
#[tauri::command]
pub fn native_key_thumbprint(issuer: String) -> Result<String, String> {
    let issuer = canonical_issuer(&issuer)?;
    Ok(thumbprint(&public_jwk(&signing_key(&issuer)?)?))
}
#[tauri::command]
pub fn native_sign_jwt(issuer: String, signing_input: Vec<u8>) -> Result<Vec<u8>, String> {
    let issuer = canonical_issuer(&issuer)?;
    validate_dpop_signing_input(&signing_input)?;
    let signature: Signature = signing_key(&issuer)?.sign(&signing_input);
    Ok(signature.to_bytes().to_vec())
}

#[tauri::command]
pub async fn native_exchange_authorization(
    state: State<'_, NativeAuthState>,
    request: NativeAuthorizationExchangeRequest,
) -> Result<AccessCredential, String> {
    let NativeAuthorizationExchangeRequest {
        issuer,
        transaction_id,
        code_verifier,
        redirect_uri,
    } = request;
    let issuer = canonical_issuer(&issuer)?;
    let _ = redirect_uri;
    let response = token_request(&state, &issuer, json!({"grant_type":"authorization_code","transaction_id":transaction_id,"code_verifier":code_verifier,"public_jwk":public_jwk(&signing_key(&issuer)?)?})).await?;
    store_token_response(&state, &issuer, response).await
}

/// Exchange a device code before the client has an access token. The Rust
/// side signs the required DPoP proof with the key held by the native keyring.
#[tauri::command]
pub async fn native_exchange_device_code(
    state: State<'_, NativeAuthState>,
    request: NativeDeviceExchangeRequest,
) -> Result<AccessCredential, String> {
    let NativeDeviceExchangeRequest {
        issuer,
        device_code,
        code_verifier,
        public_jwk,
    } = request;
    let issuer = canonical_issuer(&issuer)?;
    let key = signing_key(&issuer)?;
    let key_public = self::public_jwk(&key)?;
    if thumbprint(&public_jwk) != thumbprint(&key_public) {
        return Err(safe_error("device_key_invalid"));
    }
    let response = token_request(
        &state,
        &issuer,
        json!({
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "code_verifier": code_verifier,
            "public_jwk": key_public,
        }),
    )
    .await?;
    store_token_response(&state, &issuer, response).await
}

/// Bind a one-shot loopback HTTP listener for the same-device browser
/// handoff and return the ephemeral port to use as the redirect URI. The
/// server only approves loopback ports in the 49152-65535 range, so binding
/// port zero may be retried when the OS hands out a port below that range.
#[tauri::command]
pub async fn native_bind_authorization_listener(
    state: State<'_, NativeAuthState>,
    expected_state: String,
    issuer: String,
) -> Result<u16, String> {
    let issuer = canonical_issuer(&issuer)?;
    if expected_state.is_empty() {
        return Err(safe_error("listener_state_invalid"));
    }
    if state
        .callback_listeners
        .lock()
        .unwrap()
        .contains_key(&expected_state)
    {
        return Err(safe_error("listener_already_bound"));
    }
    for _ in 0..32 {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .map_err(|_| safe_error("listener_bind_failed"))?;
        let port = listener
            .local_addr()
            .map_err(|_| safe_error("listener_bind_failed"))?
            .port();
        if !(LOOPBACK_PORT_MIN..=LOOPBACK_PORT_MAX).contains(&port) {
            continue;
        }
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let task = tauri::async_runtime::spawn(run_callback_listener(
            listener,
            expected_state.clone(),
            issuer,
            port,
            sender,
        ));
        state
            .callback_listeners
            .lock()
            .unwrap()
            .insert(expected_state, PendingCallbackListener { receiver, task });
        return Ok(port);
    }
    Err(safe_error("listener_bind_failed"))
}

/// Wait for the browser to hit the bound loopback callback and return the
/// strict callback URL (state + transaction_id only) for the JS session.
#[tauri::command]
pub async fn native_wait_authorization_callback(
    state: State<'_, NativeAuthState>,
    expected_state: String,
) -> Result<String, String> {
    let listener = state
        .callback_listeners
        .lock()
        .unwrap()
        .remove(&expected_state)
        .ok_or_else(|| safe_error("authorization_listener_missing"))?;
    listener.task.abort();
    listener
        .receiver
        .await
        .map_err(|_| safe_error("authorization_callback_timeout"))
}

/// Release a bound listener early when the authorization is cancelled, so
/// the loopback port is not held until the accept timeout expires.
#[tauri::command]
pub fn native_cancel_authorization_listener(
    state: State<'_, NativeAuthState>,
    expected_state: String,
) {
    if let Some(listener) = state
        .callback_listeners
        .lock()
        .unwrap()
        .remove(&expected_state)
    {
        listener.task.abort();
    }
}

async fn run_callback_listener(
    listener: tokio::net::TcpListener,
    expected_state: String,
    issuer: String,
    port: u16,
    sender: tokio::sync::oneshot::Sender<String>,
) {
    let deadline = tokio::time::Instant::now() + CALLBACK_ACCEPT_TIMEOUT;
    loop {
        let accepted = tokio::time::timeout_at(deadline, listener.accept()).await;
        let Ok(Ok((mut stream, _peer))) = accepted else {
            return;
        };
        let Some(request_head) = read_request_head(&mut stream).await else {
            let _ = respond_http(&mut stream, "400 Bad Request", b"Bad Request").await;
            continue;
        };
        let Some(callback) = callback_request_url(&request_head, &expected_state, port) else {
            let _ = respond_http(&mut stream, "404 Not Found", b"Not Found").await;
            continue;
        };
        let body = redirect_home_page(&issuer);
        let _ = respond_http(&mut stream, "200 OK", body.as_bytes()).await;
        let _ = sender.send(callback);
        return;
    }
}

/// Read the request head up to the blank line, capped to the head size limit.
async fn read_request_head(stream: &mut tokio::net::TcpStream) -> Option<String> {
    let mut buffer = Vec::with_capacity(512);
    let mut chunk = [0_u8; 256];
    loop {
        let read = stream.read(&mut chunk).await.ok()?;
        if read == 0 {
            break;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if buffer.len() >= CALLBACK_HEAD_MAX_BYTES
            || buffer.windows(4).any(|window| window == b"\r\n\r\n")
        {
            break;
        }
    }
    Some(String::from_utf8_lossy(&buffer).into_owned())
}

/// Rebuild the strict callback URL from the raw request head. Returns None
/// for any method, path, or state mismatch so only the exact approved handoff
/// is ever forwarded to the waiting session. Mirrors the server-side
/// ``validate_callback_uri`` contract: state and transaction_id only.
fn callback_request_url(request_head: &str, expected_state: &str, port: u16) -> Option<String> {
    let request_line = request_head.lines().next()?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?;
    if !matches!(method, "GET" | "HEAD") {
        return None;
    }
    let target = parts.next()?;
    let url = Url::parse(&format!("http://127.0.0.1:{port}{target}")).ok()?;
    if url.path() != "/oauth/callback" || !url.username().is_empty() {
        return None;
    }
    let pairs: Vec<(String, String)> = url
        .query_pairs()
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    if pairs.len() != 2
        || pairs
            .iter()
            .any(|(key, _value)| key != "state" && key != "transaction_id")
        || pairs
            .iter()
            .any(|(key, value)| key == "transaction_id" && uuid::Uuid::parse_str(value).is_err())
    {
        return None;
    }
    let states: Vec<&String> = pairs
        .iter()
        .filter(|(key, _)| key == "state")
        .map(|(_, value)| value)
        .collect();
    if states.len() != 1 || states[0] != expected_state {
        return None;
    }
    Some(url.into())
}

/// A tiny self-closing page that returns the browser to the web home page
/// after the callback has been delivered to the native client.
fn redirect_home_page(issuer: &str) -> String {
    let mut page = String::with_capacity(256);
    page.push_str("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">");
    page.push_str("<meta http-equiv=\"refresh\" content=\"0;url=");
    page.push_str(issuer);
    page.push_str("/\"><title>TermFlow</title></head><body>");
    page.push_str("<script>location.replace(\"");
    page.push_str(issuer);
    page.push_str("/\");</script>");
    page.push_str("<p>已授权，正在返回 TermFlow…</p></body></html>");
    page
}

async fn respond_http(
    stream: &mut tokio::net::TcpStream,
    status: &str,
    body: &[u8],
) -> std::io::Result<()> {
    let headers = format!(
        "HTTP/1.1 {status}\r\ncontent-type: text/html; charset=utf-8\r\ncontent-length: {}\r\nconnection: close\r\ncache-control: no-store\r\n\r\n",
        body.len()
    );
    stream.write_all(headers.as_bytes()).await?;
    stream.write_all(body).await?;
    stream.flush().await
}

#[tauri::command]
pub async fn native_refresh_access(
    state: State<'_, NativeAuthState>,
    issuer: String,
) -> Result<AccessCredential, String> {
    let issuer = canonical_issuer(&issuer)?;
    let _refresh_guard = state.refresh_gate.lock().await;
    refresh_access(&state, &issuer).await
}

/// Removes the refresh credential for one canonical issuer. The device P-256
/// key is intentionally retained so a later authorization keeps the same
/// device identity without exposing that key to the WebView.
#[tauri::command]
pub fn native_clear_credentials(
    state: State<'_, NativeAuthState>,
    issuer: String,
) -> Result<(), String> {
    let issuer = canonical_issuer(&issuer)?;
    clear_native_credentials(&state, &issuer)
}

#[tauri::command]
pub async fn native_request_headers(
    state: State<'_, NativeAuthState>,
    issuer: String,
    method: String,
    url: String,
    nonce: Option<String>,
) -> Result<NativeHeaders, String> {
    let issuer = canonical_issuer(&issuer)?;
    assert_api_target(&issuer, &url)?;
    let access = current_access(&state, &issuer).await?;
    let nonce = match nonce {
        Some(value) => {
            remember_dpop_nonce(&state, &issuer, &value)?;
            Some(value)
        }
        None => remembered_dpop_nonce(&state, &issuer)?,
    };
    let proof = dpop_proof(
        &signing_key(&issuer)?,
        &method,
        &url,
        nonce.as_deref(),
        Some(&access.access_token),
    )?;
    Ok(NativeHeaders {
        authorization: format!("DPoP {}", access.access_token),
        dpop: proof,
    })
}

#[tauri::command]
pub fn native_remember_dpop_nonce(
    state: State<'_, NativeAuthState>,
    issuer: String,
    nonce: String,
) -> Result<(), String> {
    let issuer = canonical_issuer(&issuer)?;
    remember_dpop_nonce(&state, &issuer, &nonce)
}

fn is_public_api_path(path: &str) -> bool {
    let path = path.split('?').next().unwrap_or(path);
    matches!(
        path,
        "/.well-known/oauth-authorization-server" | "/healthz" | "/api/v1/oauth/device/code"
    )
}

fn assert_http_target(issuer: &str, path: &str) -> Result<Url, String> {
    if !path.starts_with('/')
        || path.starts_with("//")
        || path.contains("://")
        || path.contains('\\')
    {
        return Err(safe_error("url_not_allowed"));
    }
    let url = Url::parse(&format!("{issuer}{path}")).map_err(|_| safe_error("url_invalid"))?;
    let base = Url::parse(issuer).map_err(|_| safe_error("issuer_invalid"))?;
    if url.origin() != base.origin() || url.username() != "" || url.password().is_some() {
        return Err(safe_error("url_not_allowed"));
    }
    if !is_public_api_path(path) && !url.path().starts_with("/api/") {
        return Err(safe_error("url_not_allowed"));
    }
    Ok(url)
}

pub(crate) async fn request_auth_headers(
    state: &NativeAuthState,
    issuer: &str,
    method: &str,
    url: &str,
) -> Result<NativeHeaders, String> {
    let access = current_access(state, issuer).await?;
    let nonce = remembered_dpop_nonce(state, issuer)?;
    let proof = dpop_proof(
        &signing_key(issuer)?,
        method,
        url,
        nonce.as_deref(),
        Some(&access.access_token),
    )?;
    Ok(NativeHeaders {
        authorization: format!("DPoP {}", access.access_token),
        dpop: proof,
    })
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NativeHttpResponse {
    status: u16,
    headers: HashMap<String, String>,
    body: Option<Value>,
}

/// The only WebView-visible HTTP channel. The Rust side pins the target to
/// the configured issuer origin and `/api/` prefix (or the public bootstrap
/// paths), signs DPoP locally, and never hands the raw access token to JS.
#[tauri::command]
pub async fn native_http_request(
    state: State<'_, NativeAuthState>,
    issuer: String,
    path: String,
    method: String,
    headers: Option<HashMap<String, String>>,
    body: Option<Value>,
    nonce: Option<String>,
) -> Result<NativeHttpResponse, String> {
    let issuer = canonical_issuer(&issuer)?;
    let _ = assert_http_target(&issuer, &path)?;
    let url = format!("{issuer}{path}");
    let method = method.to_uppercase();
    let is_public = is_public_api_path(&path);
    let access = if is_public {
        None
    } else {
        Some(current_access(&state, &issuer).await?)
    };
    let remembered = if is_public {
        None
    } else {
        remembered_dpop_nonce(&state, &issuer)?
    };
    let effective_nonce = nonce.or(remembered);

    let send = |request_nonce: Option<String>| -> Result<reqwest::RequestBuilder, String> {
        let mut builder = match method.as_str() {
            "GET" => state.http.get(&url),
            "POST" => state.http.post(&url),
            "PATCH" => state.http.patch(&url),
            "DELETE" => state.http.delete(&url),
            _ => return Err(safe_error("method_not_allowed")),
        };
        for (name, value) in headers.iter().flatten() {
            builder = builder.header(name, value);
        }
        if !is_public {
            let key = signing_key(&issuer)?;
            let proof = dpop_proof(
                &key,
                &method,
                &url,
                request_nonce.as_deref(),
                access.as_ref().map(|value| value.access_token.as_str()),
            )?;
            builder = builder
                .header(
                    "Authorization",
                    format!("DPoP {}", access.as_ref().unwrap().access_token),
                )
                .header("DPoP", proof);
        }
        if let Some(value) = &body {
            builder = builder.json(value);
        }
        Ok(builder)
    };

    let first = send(effective_nonce.clone())?
        .send()
        .await
        .map_err(|_| safe_error("request_failed"))?;
    let response = if first.status() == StatusCode::UNAUTHORIZED && !is_public {
        if let Some(nonce) = first
            .headers()
            .get("DPoP-Nonce")
            .and_then(|value| value.to_str().ok())
        {
            send(Some(nonce.to_owned()))?
                .send()
                .await
                .map_err(|_| safe_error("request_failed"))?
        } else {
            first
        }
    } else {
        first
    };
    if !is_public {
        if let Some(nonce) = response
            .headers()
            .get("DPoP-Nonce")
            .and_then(|value| value.to_str().ok())
        {
            remember_dpop_nonce(&state, &issuer, nonce)?;
        }
    }
    let status = response.status().as_u16();
    let response_headers = response
        .headers()
        .iter()
        .map(|(name, value)| {
            (
                name.as_str().to_owned(),
                value.to_str().unwrap_or_default().to_owned(),
            )
        })
        .collect::<HashMap<String, String>>();
    let body_value = if response_headers
        .get("content-type")
        .unwrap_or(&String::new())
        .contains("application/json")
    {
        response.json::<Value>().await.map(Some).unwrap_or(None)
    } else {
        None
    };
    Ok(NativeHttpResponse {
        status,
        headers: response_headers,
        body: body_value,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_jwk_accepts_owned_ipc_payload() {
        let public: PublicJwk =
            serde_json::from_str(r#"{"kty":"EC","crv":"P-256","alg":"ES256","x":"x","y":"y"}"#)
                .unwrap();
        assert_eq!(public.kty, "EC");
        assert_eq!(public.crv, "P-256");
    }

    #[test]
    fn public_material_and_debug_output_never_include_private_key() {
        let key = SigningKey::random(&mut p256::elliptic_curve::rand_core::OsRng);
        let private = key.to_pkcs8_der().unwrap();
        let public = public_jwk(&key).unwrap();
        let serialized = serde_json::to_string(&public).unwrap();
        assert!(!serialized
            .as_bytes()
            .windows(8)
            .any(|window| private.as_bytes().windows(8).any(|secret| secret == window)));
        assert!(!format!("{public:?}").contains(&URL_SAFE_NO_PAD.encode(private.as_bytes())));
    }
    #[test]
    fn dpop_contains_only_public_jwk_and_raw_signature() {
        let key = SigningKey::random(&mut p256::elliptic_curve::rand_core::OsRng);
        let proof = dpop_proof(
            &key,
            "GET",
            "https://b.example/api/v1/dashboard?ignored=1",
            None,
            Some("access-value"),
        )
        .unwrap();
        assert_eq!(proof.split('.').count(), 3);
        assert!(!proof.contains("access-value"));
        assert!(!proof.contains("ignored=1"));
    }

    #[test]
    fn remembered_dpop_nonce_is_issuer_scoped_and_validated() {
        let state = NativeAuthState::default();
        remember_dpop_nonce(&state, "https://one.example", "nonce-one").unwrap();
        remember_dpop_nonce(&state, "https://two.example", "nonce-two").unwrap();

        assert_eq!(
            remembered_dpop_nonce(&state, "https://one.example").unwrap(),
            Some("nonce-one".to_owned())
        );
        assert_eq!(
            remembered_dpop_nonce(&state, "https://two.example").unwrap(),
            Some("nonce-two".to_owned())
        );
        assert!(remember_dpop_nonce(&state, "https://one.example", "bad nonce").is_err());
    }

    #[test]
    fn api_target_must_match_issuer_origin_and_api_prefix() {
        let issuer = "https://b.example";
        assert!(assert_api_target(issuer, "https://b.example/api/v1/dashboard").is_ok());
        assert!(assert_api_target(issuer, "https://attacker.example/api/v1/dashboard").is_err());
        assert!(assert_api_target(issuer, "https://b.example/other").is_err());
        assert!(assert_api_target(issuer, "https://b.example/").is_err());
        assert!(assert_api_target(issuer, "https://user:pass@b.example/api/v1/dashboard").is_err());
    }

    #[test]
    fn http_target_allows_public_paths_without_api_prefix() {
        let issuer = "https://b.example";
        assert!(assert_http_target(issuer, "/api/v1/dashboard").is_ok());
        assert!(assert_http_target(issuer, "/.well-known/oauth-authorization-server").is_ok());
        assert!(assert_http_target(issuer, "/healthz").is_ok());
        assert!(assert_http_target(issuer, "/api/v1/oauth/device/code").is_ok());
        assert!(assert_http_target(issuer, "/not-api").is_err());
        assert!(assert_http_target(issuer, "//attacker.example/api/v1/x").is_err());
        assert!(assert_http_target(issuer, "/api/v1/../secret").is_ok());
    }

    #[test]
    fn dpop_signing_input_must_be_two_part_dpop_jwt() {
        let key = SigningKey::random(&mut p256::elliptic_curve::rand_core::OsRng);
        let jwk = public_jwk(&key).unwrap();
        let header = jwt_segment(&json!({"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk})).unwrap();
        let claims =
            jwt_segment(&json!({"jti": "abc", "htm": "GET", "htu": "https://b.example/api"}))
                .unwrap();
        let valid = format!("{header}.{claims}");
        assert!(validate_dpop_signing_input(valid.as_bytes()).is_ok());
        assert!(validate_dpop_signing_input(b"not-a-jwt".as_slice()).is_err());
        let missing_htu = jwt_segment(&json!({"jti": "abc", "htm": "GET"})).unwrap();
        assert!(validate_dpop_signing_input(format!("{header}.{missing_htu}").as_bytes()).is_err());
        let wrong_alg =
            jwt_segment(&json!({"typ": "dpop+jwt", "alg": "RS256", "jwk": {}})).unwrap();
        assert!(validate_dpop_signing_input(format!("{wrong_alg}.{claims}").as_bytes()).is_err());
        assert!(
            validate_dpop_signing_input(format!("{header}.{claims}.extra").as_bytes()).is_err()
        );
    }

    #[test]
    fn oauth_invalid_grant_is_the_only_token_error_that_revokes_local_credentials() {
        assert!(token_error_revokes_credentials(
            br#"{"error":{"code":"invalid_grant","message":"The grant is invalid."}}"#
        ));
        assert!(!token_error_revokes_credentials(
            br#"{"error":{"code":"invalid_dpop_proof","message":"Authentication failed."}}"#
        ));
        assert!(!token_error_revokes_credentials(b"not-json"));
        assert_eq!(
            token_error_code(br#"{"error":{"code":"authorization_pending","message":"Pending."}}"#),
            Some("authorization_pending".to_owned())
        );
    }

    #[test]
    fn clearing_cached_credentials_drops_access_and_nonce_for_only_one_issuer() {
        let state = NativeAuthState::default();
        state.access.lock().unwrap().insert(
            "https://one.example".to_owned(),
            AccessState {
                access_token: "one".to_owned(),
                expires_at_unix: 1,
            },
        );
        state.access.lock().unwrap().insert(
            "https://two.example".to_owned(),
            AccessState {
                access_token: "two".to_owned(),
                expires_at_unix: 2,
            },
        );
        remember_dpop_nonce(&state, "https://one.example", "nonce-one").unwrap();
        remember_dpop_nonce(&state, "https://two.example", "nonce-two").unwrap();

        clear_cached_credentials(&state, "https://one.example").unwrap();

        assert!(!state
            .access
            .lock()
            .unwrap()
            .contains_key("https://one.example"));
        assert!(state
            .access
            .lock()
            .unwrap()
            .contains_key("https://two.example"));
        assert_eq!(
            remembered_dpop_nonce(&state, "https://one.example").unwrap(),
            None
        );
        assert_eq!(
            remembered_dpop_nonce(&state, "https://two.example").unwrap(),
            Some("nonce-two".to_owned())
        );
    }

    #[test]
    fn callback_request_url_accepts_only_the_exact_browser_handoff() {
        let port = 51_234;
        let expected = "state-1";
        let valid = format!(
            "GET /oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111 HTTP/1.1\r\nhost: 127.0.0.1:{port}\r\n\r\n"
        );
        assert_eq!(
            callback_request_url(&valid, expected, port).as_deref(),
            Some(
                "http://127.0.0.1:51234/oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111"
            )
        );
        assert_eq!(callback_request_url(&valid, "state-2", port), None);
        for (head, label) in [
            (format!("POST {valid}"), "POST method"),
            ("GET /other?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111 HTTP/1.1\r\n\r\n".to_owned(), "wrong path"),
            ("GET /oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111&code=extra HTTP/1.1\r\n\r\n".to_owned(), "extra query"),
            ("GET /oauth/callback?state=state-1&state=state-1 HTTP/1.1\r\n\r\n".to_owned(), "duplicate state"),
            ("GET /oauth/callback?state=state-1 HTTP/1.1\r\n\r\n".to_owned(), "missing transaction"),
            ("GET /oauth/callback?state=state-1&transaction_id=not-a-uuid HTTP/1.1\r\n\r\n".to_owned(), "bad transaction"),
            ("not an http request".to_owned(), "garbage"),
        ] {
            assert_eq!(callback_request_url(&head, expected, port), None, "{label}");
        }
    }

    #[test]
    fn redirect_home_page_returns_to_the_issuer_root() {
        let page = redirect_home_page("https://termflow.example");
        assert!(page.contains("content=\"0;url=https://termflow.example/\""));
        assert!(page.contains("location.replace(\"https://termflow.example/\");"));
    }

    #[test]
    fn loopback_listener_round_trip() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
                .await
                .unwrap();
            let port = listener.local_addr().unwrap().port();
            let (sender, receiver) = tokio::sync::oneshot::channel();
            let task = tokio::task::spawn(run_callback_listener(
                listener,
                "state-1".to_owned(),
                "https://termflow.example".to_owned(),
                port,
                sender,
            ));
            let mut stream = tokio::net::TcpStream::connect(("127.0.0.1", port))
                .await
                .unwrap();
            stream
                .write_all(
                    b"GET /oauth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111 HTTP/1.1\r\nhost: 127.0.0.1\r\n\r\n",
                )
                .await
                .unwrap();
            let callback = tokio::time::timeout(std::time::Duration::from_secs(5), receiver)
                .await
                .unwrap()
                .unwrap();
            assert!(callback.contains("transaction_id=11111111-1111-4111-8111-111111111111"));
            task.abort();
        });
    }
}
