//! Contract tests for the Rust `native_upload_audio` command (M7b spec
//! §4.5/§6.3): the upload target is pinned to the issuer origin and the
//! `/api/` surface, the client-side size ceiling matches B's 10 MiB limit
//! with cross-referencing comments, and the outgoing request always carries
//! DPoP authorization. Mirrors the file-based style of
//! `tests/http_capability_scope.rs` where a source pin is the right tool.

use std::{fs, path::PathBuf};

use termflow_client_lib::contract_testing::{
    assert_http_target, build_multipart, build_upload_request, DRAFT_UPLOAD_PATH, MAX_AUDIO_BYTES,
};

#[test]
fn upload_target_is_the_drafts_endpoint_on_the_issuer_origin() {
    assert_eq!(
        DRAFT_UPLOAD_PATH, "/api/v1/agent/transcription/drafts",
        "the upload endpoint literal is part of the B-side API contract"
    );
    let url = assert_http_target("https://b.example", DRAFT_UPLOAD_PATH)
        .expect("the pinned upload path must pass the http target whitelist");
    assert_eq!(
        url.as_str(),
        "https://b.example/api/v1/agent/transcription/drafts"
    );
    assert_eq!(
        url.origin().ascii_serialization(),
        "https://b.example",
        "the upload target stays on the issuer origin"
    );
}

#[test]
fn http_target_whitelist_rejects_foreign_origins_and_schemes() {
    let issuer = "https://b.example";
    // Absolute URLs cannot escape the issuer origin.
    assert!(assert_http_target(issuer, "https://evil.example/api/v1/x").is_err());
    // Protocol-relative URLs and backslashes are smuggler staples.
    assert!(assert_http_target(issuer, "//evil.example/api/v1/x").is_err());
    assert!(assert_http_target(issuer, "/api/v1/..\\..\\secret").is_err());
    assert!(assert_http_target(issuer, "api/v1/x").is_err());
    // Paths outside the /api/ surface are not reachable by commands.
    assert!(assert_http_target(issuer, "/elsewhere").is_err());
}

#[test]
fn http_target_whitelist_keeps_same_origin_api_and_public_paths() {
    let issuer = "https://b.example";
    assert!(assert_http_target(issuer, "/api/v1/dashboard").is_ok());
    assert!(assert_http_target(issuer, "/healthz").is_ok());
    assert!(assert_http_target(issuer, "/.well-known/oauth-authorization-server").is_ok());
}

#[test]
fn client_upload_ceiling_matches_the_b_side_ten_mib_limit() {
    // 10 MiB (B's `MAX_AUDIO_BYTES`) plus 64 KiB of framing headroom.
    assert_eq!(MAX_AUDIO_BYTES, 10 * 1024 * 1024 + 64 * 1024);
    // The constant's doc comment must cross-reference B's authoritative
    // limit so a B-side change is caught here by the comment pin (and by
    // the value assertion above).
    let source =
        fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/audio_upload.rs"))
            .expect("read src/audio_upload.rs");
    let constant = source
        .find("pub const MAX_AUDIO_BYTES")
        .expect("MAX_AUDIO_BYTES is defined in audio_upload.rs");
    let doc_comment = &source[..constant];
    assert!(
        doc_comment.contains("api/transcription.py"),
        "the MAX_AUDIO_BYTES comment must cross-reference B's api/transcription.py limit"
    );
    assert!(
        doc_comment.contains("10 MiB"),
        "the MAX_AUDIO_BYTES comment must state B's 10 MiB limit"
    );
}

#[test]
fn upload_request_always_carries_dpop_headers_and_a_multipart_body() {
    let http = reqwest::Client::new();
    let url = format!("https://b.example{DRAFT_UPLOAD_PATH}");
    let form = build_multipart(b"audio bytes".to_vec(), "audio/webm", "binding-1", "conv-2")
        .expect("whitelisted mime builds a form");
    let request = build_upload_request(&http, &url, "access-token", "dpop-proof", form)
        .build()
        .expect("upload request builds");
    assert_eq!(request.method(), reqwest::Method::POST);
    assert_eq!(request.url().as_str(), url);
    assert_eq!(
        request
            .headers()
            .get("DPoP")
            .expect("DPoP header")
            .to_str()
            .unwrap(),
        "dpop-proof"
    );
    assert_eq!(
        request
            .headers()
            .get("Authorization")
            .expect("Authorization header")
            .to_str()
            .unwrap(),
        "DPoP access-token"
    );
    let content_type = request
        .headers()
        .get("content-type")
        .expect("multipart Content-Type")
        .to_str()
        .unwrap();
    assert!(
        content_type.starts_with("multipart/form-data; boundary="),
        "reqwest must generate the multipart Content-Type: {content_type}"
    );
}
