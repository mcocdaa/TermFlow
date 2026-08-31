use serde_json::Value;
use std::{fs, path::PathBuf};

fn capability_document(capability: &str) -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("capabilities")
        .join(capability);
    serde_json::from_str(&fs::read_to_string(path).expect("read capability JSON"))
        .expect("parse capability JSON")
}

#[test]
fn native_network_capabilities_are_removed_because_rust_owns_all_webview_traffic() {
    for capability in ["default.json", "mobile.json"] {
        let document = capability_document(capability);
        let permissions = document["permissions"]
            .as_array()
            .expect("permissions array");
        assert!(
            !permissions
                .iter()
                .any(|permission| permission == "websocket:default"),
            "{capability} must not expose the unrestricted websocket permission set"
        );
        for permission in permissions {
            if let Some(identifier) = permission.get("identifier") {
                assert_ne!(
                    identifier.as_str(),
                    Some("http:default"),
                    "{capability} must not expose the http plugin permission set"
                );
            }
        }
    }
}

#[test]
fn native_opener_capabilities_allow_the_authorization_browser_urls() {
    for capability in ["default.json", "mobile.json"] {
        let document = capability_document(capability);
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
