//! Rust-owned multipart upload for mobile voice transcriptions (M7b spec
//! §4.5). The WebView hands in a base64-encoded recording; this module owns
//! the network authority: it pins the target to the issuer origin, signs
//! DPoP locally, and builds the multipart body — the raw access token and
//! the audio bytes never cross back into JS.
//!
//! The DPoP/nonce retry loop is a line-by-line reuse of `native_http_request`
//! in `auth.rs` (same send closure, 401 + `DPoP-Nonce` retry, nonce
//! remembering), so the two commands stay behaviorally identical.

use std::{collections::HashMap, time::Duration};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use reqwest::{multipart, StatusCode};
use serde_json::Value;
use tauri::State;

use crate::auth::{
    assert_http_target, canonical_issuer, current_access_token, dpop_proof, remember_dpop_nonce,
    remembered_dpop_nonce, safe_error, signing_key, NativeAuthState, NativeHttpResponse,
};

/// The transcription-draft upload endpoint (M7b spec §4.5), pinned at
/// compile time so the WebView cannot point the upload anywhere outside the
/// issuer's `/api/` surface.
pub const DRAFT_UPLOAD_PATH: &str = "/api/v1/agent/transcription/drafts";

/// Client-side ceiling for the decoded audio bytes: the B side rejects
/// anything above 10 MiB
/// (`apps/control-plane/src/termflow_control_plane/api/transcription.py`
/// `MAX_AUDIO_BYTES`, first enforced on the declared Content-Length). The
/// extra 64 KiB is headroom for multipart framing that B does not attribute
/// to the audio, so a file B accepts can never trip this client defense.
/// The client's own 240 s recording cap (~7.68 MB WAV) already stays well
/// below this — the constant is the second line of defense (spec §4.5).
pub const MAX_AUDIO_BYTES: usize = 10 * 1024 * 1024 + 64 * 1024;

/// Longest base64 text that can possibly decode to `MAX_AUDIO_BYTES` or
/// fewer bytes (3 bytes → 4 chars, padded), used to reject oversized
/// payloads before decoding allocates memory for them.
const MAX_AUDIO_BASE64_CHARS: usize = MAX_AUDIO_BYTES.div_ceil(3) * 4;

/// Upload timeout (§4.5): 120 s exceeds B's 60 s transcription wall-clock,
/// so B's own 504 reaches the client before we drop the connection.
const UPLOAD_TIMEOUT: Duration = Duration::from_secs(120);

/// Decode the WebView-supplied base64 payload and enforce the size defense
/// `0 < len <= MAX_AUDIO_BYTES`. Empty, corrupt, and oversized input all
/// collapse to `audio_too_large` so the JS layer maps them onto the §4.6
/// 413 "re-record" semantics (a corrupt payload must never masquerade as
/// the offline/retry case).
pub(crate) fn decode_audio_base64(audio_base64: &str) -> Result<Vec<u8>, String> {
    if audio_base64.len() > MAX_AUDIO_BASE64_CHARS {
        return Err(safe_error("audio_too_large"));
    }
    let bytes = STANDARD
        .decode(audio_base64.as_bytes())
        .map_err(|_| safe_error("audio_too_large"))?;
    if bytes.is_empty() || bytes.len() > MAX_AUDIO_BYTES {
        return Err(safe_error("audio_too_large"));
    }
    Ok(bytes)
}

/// Maps the mime whitelist onto the filename B keys its format checks on
/// (§4.5). The JS side pre-filters the same two entries; this is the Rust
/// side of the defense.
fn audio_mime_filename(mime: &str) -> Result<&'static str, String> {
    match mime {
        "audio/webm" => Ok("speech.webm"),
        "audio/wav" => Ok("speech.wav"),
        _ => Err(safe_error("audio_mime_not_allowed")),
    }
}

/// Pure multipart assembly (`file` bytes + filename + content-type, plus the
/// `binding_id` and `target_conversation_id` text fields) — extracted so the
/// field layout is unit-testable without a live server.
pub fn build_multipart(
    bytes: Vec<u8>,
    mime: &str,
    binding_id: &str,
    conversation_id: &str,
) -> Result<multipart::Form, String> {
    let filename = audio_mime_filename(mime)?;
    let file = multipart::Part::bytes(bytes)
        .file_name(filename.to_owned())
        .mime_str(mime)
        .map_err(|_| safe_error("audio_mime_not_allowed"))?;
    Ok(multipart::Form::new()
        .part("file", file)
        .text("binding_id", binding_id.to_owned())
        .text("target_conversation_id", conversation_id.to_owned()))
}

/// Pure request assembly for the upload POST: DPoP authorization + DPoP
/// proof headers and the multipart body (whose Content-Type reqwest
/// generates with its own boundary — never hand-set).
pub fn build_upload_request(
    http: &reqwest::Client,
    url: &str,
    access_token: &str,
    proof: &str,
    form: multipart::Form,
) -> reqwest::RequestBuilder {
    http.post(url)
        .header("Authorization", format!("DPoP {access_token}"))
        .header("DPoP", proof)
        .multipart(form)
}

/// The only WebView-visible upload channel. Reuses `native_http_request`'s
/// DPoP/nonce retry loop line for line; the response envelope is passed back
/// to JS untouched for §4.6 classification.
#[tauri::command]
pub async fn native_upload_audio(
    state: State<'_, NativeAuthState>,
    issuer: String,
    audio_base64: String,
    mime: String,
    binding_id: String,
    conversation_id: String,
    nonce: Option<String>,
) -> Result<NativeHttpResponse, String> {
    let issuer = canonical_issuer(&issuer)?;
    let target = assert_http_target(&issuer, DRAFT_UPLOAD_PATH)?;
    let url = target.as_str().to_owned();
    let bytes = decode_audio_base64(&audio_base64)?;

    let access_token = current_access_token(&state, &issuer).await?;
    let remembered = remembered_dpop_nonce(&state, &issuer)?;
    let effective_nonce = nonce.or(remembered);

    // Line-by-line reuse of native_http_request's send closure: DPoP signs
    // only htu/htm (body-independent), so the multipart form is rebuilt per
    // attempt and attached after the headers are computed.
    let send = |request_nonce: Option<String>| -> Result<reqwest::RequestBuilder, String> {
        let key = signing_key(&issuer)?;
        let proof = dpop_proof(
            &key,
            "POST",
            &url,
            request_nonce.as_deref(),
            Some(&access_token),
        )?;
        let form = build_multipart(bytes.clone(), &mime, &binding_id, &conversation_id)?;
        Ok(
            build_upload_request(&state.http, &url, &access_token, &proof, form)
                .timeout(UPLOAD_TIMEOUT),
        )
    };

    let first = send(effective_nonce.clone())?
        .send()
        .await
        .map_err(|_| safe_error("offline"))?;
    let response = if first.status() == StatusCode::UNAUTHORIZED {
        if let Some(nonce) = first
            .headers()
            .get("DPoP-Nonce")
            .and_then(|value| value.to_str().ok())
        {
            send(Some(nonce.to_owned()))?
                .send()
                .await
                .map_err(|_| safe_error("offline"))?
        } else {
            first
        }
    } else {
        first
    };
    // Remember any nonce the server issued, including on non-success
    // responses, mirroring native_http_request.
    if let Some(nonce) = response
        .headers()
        .get("DPoP-Nonce")
        .and_then(|value| value.to_str().ok())
    {
        remember_dpop_nonce(&state, &issuer, nonce)?;
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
    let body = if response_headers
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
        body,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use p256::ecdsa::SigningKey;

    fn collect_multipart_body(form: multipart::Form) -> Vec<u8> {
        tauri::async_runtime::block_on(async move {
            use futures_util::StreamExt;
            let mut collected = Vec::new();
            let mut stream = Box::pin(form.into_stream());
            while let Some(chunk) = stream.next().await {
                collected.extend_from_slice(&chunk.expect("multipart serialization"));
            }
            collected
        })
    }

    #[test]
    fn base64_decode_rejects_empty_payload() {
        assert_eq!(decode_audio_base64(""), Err(safe_error("audio_too_large")));
    }

    #[test]
    fn base64_decode_rejects_corrupt_input() {
        assert_eq!(
            decode_audio_base64("!!!not-base64!!!"),
            Err(safe_error("audio_too_large"))
        );
    }

    #[test]
    fn base64_decode_rejects_text_that_cannot_fit_the_limit() {
        let oversized = "A".repeat(MAX_AUDIO_BASE64_CHARS + 1);
        assert_eq!(
            decode_audio_base64(&oversized),
            Err(safe_error("audio_too_large"))
        );
    }

    #[test]
    fn base64_decode_accepts_the_limit_and_rejects_one_byte_more() {
        assert_eq!(
            decode_audio_base64(&STANDARD.encode(vec![0u8; MAX_AUDIO_BYTES]))
                .unwrap()
                .len(),
            MAX_AUDIO_BYTES
        );
        let one_too_many = STANDARD.encode(vec![0u8; MAX_AUDIO_BYTES + 1]);
        assert_eq!(
            decode_audio_base64(&one_too_many),
            Err(safe_error("audio_too_large"))
        );
    }

    #[test]
    fn base64_decode_round_trips_a_small_payload() {
        let payload = b"fake webm bytes";
        assert_eq!(
            decode_audio_base64(&STANDARD.encode(payload)).unwrap(),
            payload
        );
    }

    #[test]
    fn mime_whitelist_maps_the_two_allowed_types_to_filenames() {
        assert_eq!(audio_mime_filename("audio/webm").unwrap(), "speech.webm");
        assert_eq!(audio_mime_filename("audio/wav").unwrap(), "speech.wav");
    }

    #[test]
    fn mime_whitelist_rejects_everything_else() {
        for mime in [
            "audio/mpeg",
            "application/octet-stream",
            "",
            "audio/webm;codecs=opus",
        ] {
            assert_eq!(
                audio_mime_filename(mime),
                Err(safe_error("audio_mime_not_allowed")),
                "mime {mime:?} must be rejected"
            );
        }
    }

    #[test]
    fn multipart_form_carries_the_file_and_both_text_fields() {
        let audio = b"fake webm bytes";
        let form = build_multipart(audio.to_vec(), "audio/webm", "binding-1", "conv-2").unwrap();
        let body = collect_multipart_body(form);
        let text = String::from_utf8_lossy(&body);
        let boundary = text.lines().next().unwrap().trim_end_matches('\r');
        assert!(boundary.starts_with("--"), "body starts with the boundary");
        assert!(text
            .contains("Content-Disposition: form-data; name=\"file\"; filename=\"speech.webm\""));
        assert!(text.contains("Content-Type: audio/webm"));
        assert!(text.contains("Content-Disposition: form-data; name=\"binding_id\""));
        assert!(text.contains("Content-Disposition: form-data; name=\"target_conversation_id\""));
        assert!(text.contains("binding-1"));
        assert!(text.contains("conv-2"));
        assert!(
            body.windows(audio.len()).any(|window| window == audio),
            "raw audio bytes are the file part body"
        );
        assert!(text.ends_with(&format!("{boundary}--\r\n")));
    }

    #[test]
    fn multipart_form_uses_the_wav_filename_for_wav_mime() {
        let form = build_multipart(vec![1, 2, 3], "audio/wav", "b", "c").unwrap();
        let body = collect_multipart_body(form);
        let text = String::from_utf8_lossy(&body);
        assert!(text.contains("filename=\"speech.wav\""));
        assert!(text.contains("Content-Type: audio/wav"));
        assert!(!text.contains("speech.webm"));
    }

    #[test]
    fn multipart_form_rejects_a_disallowed_mime() {
        let result = build_multipart(vec![1, 2, 3], "audio/mpeg", "b", "c");
        assert_eq!(result.err().as_deref(), Some("audio_mime_not_allowed"));
    }

    #[test]
    fn upload_request_carries_dpop_authorization_and_multipart_content_type() {
        let http = reqwest::Client::new();
        let url = "https://b.example/api/v1/agent/transcription/drafts";
        let form = build_multipart(b"audio".to_vec(), "audio/webm", "b", "c").unwrap();
        let request = build_upload_request(&http, url, "access-token", "dpop-proof", form)
            .build()
            .unwrap();
        assert_eq!(request.method(), reqwest::Method::POST);
        assert_eq!(request.url().as_str(), url);
        assert_eq!(
            request.headers().get("DPoP").unwrap().to_str().unwrap(),
            "dpop-proof"
        );
        assert_eq!(
            request
                .headers()
                .get("Authorization")
                .unwrap()
                .to_str()
                .unwrap(),
            "DPoP access-token"
        );
        let content_type = request
            .headers()
            .get("content-type")
            .unwrap()
            .to_str()
            .unwrap();
        assert!(
            content_type.starts_with("multipart/form-data; boundary="),
            "Content-Type must be reqwest-generated, never hand-set: {content_type}"
        );
    }

    #[test]
    fn upload_dpop_proof_is_a_post_to_the_drafts_endpoint() {
        let key = SigningKey::random(&mut p256::elliptic_curve::rand_core::OsRng);
        let target = format!("https://b.example{DRAFT_UPLOAD_PATH}");
        let proof = dpop_proof(
            &key,
            "POST",
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
        let claims: Value =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(payload).unwrap()).unwrap();
        assert_eq!(claims["htm"], "POST");
        assert_eq!(claims["htu"], target);
        assert_eq!(claims["nonce"], "challenge");
        assert!(claims["ath"].is_string(), "access-token hash is bound");
        assert!(!proof.contains("access-value"));
    }

    #[test]
    fn upload_timeout_outlasts_the_b_side_transcription_wall_clock() {
        assert_eq!(UPLOAD_TIMEOUT, Duration::from_secs(120));
        assert!(
            UPLOAD_TIMEOUT > Duration::from_secs(60),
            "client must wait for B's 60 s transcription timeout to fire first"
        );
    }

    #[test]
    fn draft_upload_path_passes_the_http_target_whitelist() {
        let url = assert_http_target("https://b.example", DRAFT_UPLOAD_PATH).unwrap();
        assert_eq!(
            url.as_str(),
            format!("https://b.example{DRAFT_UPLOAD_PATH}")
        );
    }
}
