use regex::Regex;
use serde_json::Value;
use std::{fs, path::PathBuf};
use url::Url;
use urlpattern::{UrlPattern, UrlPatternInit, UrlPatternMatchInput};

fn parse_pattern(value: &str) -> UrlPattern {
    let mut init = UrlPatternInit::parse_constructor_string::<Regex>(value, None)
        .unwrap_or_else(|error| panic!("invalid HTTP capability pattern {value}: {error}"));
    if init
        .search
        .as_ref()
        .map(|value| value.is_empty())
        .unwrap_or(true)
    {
        init.search.replace("*".to_string());
    }
    if init
        .hash
        .as_ref()
        .map(|value| value.is_empty())
        .unwrap_or(true)
    {
        init.hash.replace("*".to_string());
    }
    if init
        .pathname
        .as_ref()
        .map(|value| value.is_empty() || value == "/")
        .unwrap_or(true)
    {
        init.pathname.replace("*".to_string());
    }
    UrlPattern::parse(init, Default::default())
        .unwrap_or_else(|error| panic!("invalid HTTP capability pattern {value}: {error}"))
}

fn configured_patterns(capability: &str) -> Vec<UrlPattern> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("capabilities")
        .join(capability);
    let document: Value =
        serde_json::from_str(&fs::read_to_string(path).expect("read capability JSON"))
            .expect("parse capability JSON");
    document["permissions"]
        .as_array()
        .expect("permissions array")
        .iter()
        .find(|permission| permission["identifier"] == "http:default")
        .expect("http:default permission")["allow"]
        .as_array()
        .expect("HTTP allow array")
        .iter()
        .map(|entry| parse_pattern(entry["url"].as_str().expect("HTTP scope URL")))
        .collect()
}

fn is_allowed(patterns: &[UrlPattern], value: &str) -> bool {
    let url = Url::parse(value).expect("valid test URL");
    patterns.iter().any(|pattern| {
        pattern
            .test(UrlPatternMatchInput::Url(url.clone()))
            .unwrap_or(false)
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
            assert!(
                is_allowed(&patterns, allowed),
                "{capability} rejected {allowed}"
            );
        }
        assert!(!is_allowed(&patterns, "http://relay.example.com/healthz"));
    }
}

#[test]
fn native_opener_capabilities_allow_the_authorization_browser_urls() {
    for capability in ["default.json", "mobile.json"] {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("capabilities")
            .join(capability);
        let document: Value =
            serde_json::from_str(&fs::read_to_string(path).expect("read capability JSON"))
                .expect("parse capability JSON");
        let permissions = document["permissions"]
            .as_array()
            .expect("permissions array");
        assert!(
            permissions
                .iter()
                .any(|permission| permission == "opener:default"),
            "{capability} must enable the opener command permission set",
        );
    }
}
