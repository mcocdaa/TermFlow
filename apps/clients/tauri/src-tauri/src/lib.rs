mod auth;

use auth::NativeAuthState;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default();

    // Must be registered first so Windows/Linux deep-link process launches
    // are forwarded to the instance that owns the in-memory PKCE verifier.
    #[cfg(desktop)]
    let builder = builder.plugin(tauri_plugin_single_instance::init(
        |_app, _argv, _working_directory| {},
    ));

    builder
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_websocket::init())
        .plugin(tauri_plugin_os::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(NativeAuthState::default())
        .invoke_handler(tauri::generate_handler![
            auth::native_public_jwk,
            auth::native_key_thumbprint,
            auth::native_sign_jwt,
            auth::native_exchange_authorization,
            auth::native_refresh_access,
            auth::native_request_headers,
            auth::native_remember_dpop_nonce,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run TermFlow client");
}
