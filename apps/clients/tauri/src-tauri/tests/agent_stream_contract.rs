//! Contract tests for the Rust `native_agent_stream` command (M6 plan line
//! 1017): the stream target is pinned to the issuer origin and the `/api/`
//! surface, the URL always forces the `wire=agui` projection, and the
//! outgoing request always carries DPoP authorization with an event-stream
//! accept header. Mirrors the file-based style of
//! source pin where that is the right tool.

use std::{fs, path::PathBuf};

use termflow_client_lib::contract_testing::{
    assert_http_target, build_stream_request, build_stream_url, close_for_status,
    AgentStreamParams, AGENT_STREAM_PATH,
};

#[test]
fn stream_target_is_the_agent_stream_endpoint_on_the_issuer_origin() {
    assert_eq!(
        AGENT_STREAM_PATH, "/api/v1/agent/stream",
        "the stream endpoint literal is part of the B-side API contract"
    );
    let url = assert_http_target("https://b.example", AGENT_STREAM_PATH)
        .expect("the pinned stream path must pass the http target whitelist");
    assert_eq!(url.as_str(), "https://b.example/api/v1/agent/stream");
    assert_eq!(
        url.origin().ascii_serialization(),
        "https://b.example",
        "the stream target stays on the issuer origin"
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
    assert!(assert_http_target(issuer, AGENT_STREAM_PATH).is_ok());
    assert!(assert_http_target(issuer, "/api/v1/dashboard").is_ok());
    assert!(assert_http_target(issuer, "/healthz").is_ok());
}

#[test]
fn stream_request_always_carries_dpop_headers_and_event_stream_accept() {
    let http = reqwest::Client::new();
    let url = format!("https://b.example{AGENT_STREAM_PATH}?wire=agui");
    let request = build_stream_request(&http, &url, "access-token", "dpop-proof")
        .build()
        .expect("stream request builds");
    assert_eq!(request.method(), reqwest::Method::GET);
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
    assert_eq!(
        request
            .headers()
            .get("accept")
            .expect("accept header")
            .to_str()
            .unwrap(),
        "text/event-stream"
    );
}

#[test]
fn stream_url_forces_the_agui_wire_and_encodes_webview_parameters() {
    let params = AgentStreamParams {
        conversation_id: Some("x&y=z".to_owned()),
        cursor: Some("7-3/2".to_owned()),
    };
    let url = build_stream_url("https://b.example", &params).unwrap();
    assert!(url.starts_with("https://b.example/api/v1/agent/stream?"));
    assert!(
        url.contains("wire=agui"),
        "the agui projection is forced: {url}"
    );
    assert!(
        url.contains("conversation_id=x%26y%3Dz") && url.contains("cursor=7-3%2F2"),
        "webview values must be percent-encoded: {url}"
    );
}

#[test]
fn close_code_mapping_matches_the_browser_transport_contract() {
    assert_eq!(close_for_status(401), (4401, "authentication_required"));
    assert_eq!(close_for_status(403), (4412, "binding_revoked"));
    assert_eq!(close_for_status(404), (4412, "conversation_not_found"));
    assert_eq!(close_for_status(400), (4412, "invalid_cursor"));
    assert_eq!(close_for_status(503), (1006, "http_error"));
}

#[test]
fn stream_endpoint_pin_cross_references_the_b_side_api() {
    let source =
        fs::read_to_string(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/agent_stream.rs"))
            .expect("read src/agent_stream.rs");
    let constant = source
        .find("pub const AGENT_STREAM_PATH")
        .expect("AGENT_STREAM_PATH is defined in agent_stream.rs");
    let doc_comment = &source[..constant];
    assert!(
        doc_comment.contains("api.agent_stream"),
        "the AGENT_STREAM_PATH comment must cross-reference B's api/agent_stream.py endpoint"
    );
}
