use serde::Serialize;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tauri::State;
use time::{format_description::well_known::Rfc3339, OffsetDateTime};

const MAX_BYTES: u64 = 10 * 1024 * 1024;
const BACKUPS: usize = 5;

struct LoggerInner {
    path: PathBuf,
    file: Option<File>,
}

pub struct NativeLogger {
    inner: Mutex<LoggerInner>,
}

#[derive(Serialize)]
struct Event<'a> {
    timestamp: String,
    component: &'static str,
    event: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    level: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    issuer: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    request_id: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error_code: Option<&'a str>,
}

impl NativeLogger {
    pub fn new(dir: PathBuf) -> Self {
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("termflow-client.log");
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .ok();
        Self {
            inner: Mutex::new(LoggerInner { path, file }),
        }
    }

    fn rotate(inner: &mut LoggerInner) {
        let Some(file) = inner.file.as_ref() else {
            return;
        };
        let Ok(size) = file.metadata().map(|m| m.len()) else {
            return;
        };
        if size < MAX_BYTES {
            return;
        }
        inner.file.take();
        for index in (1..=BACKUPS).rev() {
            let from = if index == 1 {
                inner.path.clone()
            } else {
                PathBuf::from(format!("{}.{}", inner.path.display(), index - 1))
            };
            let to = PathBuf::from(format!("{}.{}", inner.path.display(), index));
            let _ = fs::rename(from, to);
        }
        inner.file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&inner.path)
            .ok();
    }

    pub fn event(
        &self,
        event: &str,
        level: Option<&str>,
        issuer: Option<&str>,
        request_id: Option<&str>,
        error_code: Option<&str>,
    ) {
        let Ok(mut inner) = self.inner.lock() else {
            return;
        };
        Self::rotate(&mut inner);
        let Some(file) = inner.file.as_mut() else {
            return;
        };
        let timestamp = OffsetDateTime::now_utc()
            .format(&Rfc3339)
            .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string());
        let safe_issuer = issuer
            .and_then(|value| url::Url::parse(value).ok())
            .map(|mut value| {
                value.set_path("");
                value.set_query(None);
                value.set_fragment(None);
                value.to_string()
            });
        let safe_event: String = event
            .chars()
            .filter(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'))
            .take(64)
            .collect();
        if safe_event.is_empty() {
            return;
        }
        let safe_level = level.filter(|value| matches!(*value, "info" | "warn" | "error"));
        let safe_request_id = request_id.map(|value| value.chars().take(128).collect::<String>());
        let safe_error_code = error_code.map(|value| value.chars().take(64).collect::<String>());
        let record = Event {
            timestamp,
            component: "tauri",
            event: &safe_event,
            level: safe_level,
            issuer: safe_issuer,
            request_id: safe_request_id.as_deref(),
            error_code: safe_error_code.as_deref(),
        };
        if let Ok(mut line) = serde_json::to_vec(&record) {
            line.push(b'\n');
            let _ = file.write_all(&line);
            let _ = file.flush();
        }
    }
}

#[tauri::command]
pub fn native_log(
    logger: State<'_, NativeLogger>,
    event: String,
    level: Option<String>,
    issuer: Option<String>,
    request_id: Option<String>,
    error_code: Option<String>,
) {
    logger.event(
        &event,
        level.as_deref(),
        issuer.as_deref(),
        request_id.as_deref(),
        error_code.as_deref(),
    );
}

#[allow(dead_code)]
pub fn logger_for_dir(path: &Path) -> NativeLogger {
    NativeLogger::new(path.to_path_buf())
}
