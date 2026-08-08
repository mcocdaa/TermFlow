mod auth;
mod diagnostics;
mod terminal_socket;

use auth::NativeAuthState;
use tauri::Manager;

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
        .plugin(tauri_plugin_os::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(NativeAuthState::default())
        .manage(terminal_socket::TerminalSocketState::default())
        .setup(|app| {
            let log_dir = app
                .path()
                .app_log_dir()
                .unwrap_or_else(|_| app.path().app_data_dir().unwrap_or_default());
            app.manage(diagnostics::NativeLogger::new(log_dir));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            auth::native_public_jwk,
            auth::native_key_thumbprint,
            auth::native_sign_jwt,
            auth::native_exchange_authorization,
            auth::native_exchange_device_code,
            auth::native_refresh_access,
            auth::native_clear_credentials,
            auth::native_request_headers,
            auth::native_remember_dpop_nonce,
            auth::native_http_request,
            terminal_socket::native_terminal_connect,
            terminal_socket::native_terminal_send,
            terminal_socket::native_terminal_close,
            diagnostics::native_log,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run TermFlow client");
}
