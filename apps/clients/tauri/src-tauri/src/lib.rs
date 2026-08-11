mod auth;
mod diagnostics;
mod terminal_socket;

use auth::NativeAuthState;
use tauri::Manager;

#[cfg(target_os = "android")]
fn initialize_android_context() {
    // android-native-keyring-store resolves its Android application context
    // through ndk-context, which panics when it was never initialized, and
    // Tauri's stack (tauri/tao/wry) does not initialize it. tao registers the
    // activity as a process-lifetime JNI GlobalRef in its AndroidContext and
    // publishes the raw VM and context pointers through
    // main_android_context(); adopt them into ndk-context so the keyring can
    // resolve the application context before the first keyring-backed command
    // runs. The registration happens on the JVM thread during
    // onActivityCreate, which can race with our setup, so wait briefly for it.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        if let Some(ctx) = tauri::tao::platform::android::prelude::main_android_context() {
            // SAFETY: tao keeps the activity GlobalRef alive for the whole
            // process and only publishes the pointer once the activity exists;
            // the JavaVM pointer likewise lives as long as the app.
            unsafe {
                ndk_context::initialize_android_context(ctx.java_vm, ctx.context_jobject);
            }
            return;
        }
        if std::time::Instant::now() >= deadline {
            let tag = c"termflow";
            let message = c"android context was not initialized in time";
            unsafe {
                tauri::tao::platform::android::prelude::android_log(log::Level::Error, tag, message)
            }
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
}

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
            #[cfg(target_os = "android")]
            initialize_android_context();
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
            auth::native_bind_authorization_listener,
            auth::native_wait_authorization_callback,
            auth::native_cancel_authorization_listener,
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
