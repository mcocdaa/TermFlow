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
    #[serde(skip_serializing_if = "Option::is_none")]
    error_detail: Option<&'a str>,
}

fn redact_json_values(text: &str, key: &str) -> String {
    let lower = text.to_lowercase();
    let needle = key.to_lowercase();
    if needle.is_empty() {
        return text.to_owned();
    }
    let mut output = String::with_capacity(text.len());
    let mut search_from = 0;
    let mut scan = 0;
    let bytes = text.as_bytes();
    while let Some(found) = lower[scan..]
        .find(&needle)
        .map(|offset| offset + scan)
    {
        let after_key = found + needle.len();
        let mut probe = after_key;
        while probe < bytes.len() && bytes[probe] != b':' {
            probe += 1;
        }
        if probe < bytes.len() {
            probe += 1;
            while probe < bytes.len() && bytes[probe].is_ascii_whitespace() {
                probe += 1;
            }
            if probe < bytes.len() && bytes[probe] == b'"' {
                let start = probe + 1;
                let mut end = start;
                while end < bytes.len() && bytes[end] != b'"' {
                    if bytes[end] == b'\\' {
                        end += 2;
                    } else {
                        end += 1;
                    }
                }
                if end < bytes.len() {
                    output.push_str(&text[search_from..start - 1]);
                    output.push_str("<redacted>");
                    output.push_str(&text[end..]);
                    return output;
                }
            }
        }
        output.push_str(&text[search_from..after_key]);
        search_from = after_key;
        scan = after_key;
    }
    if search_from < text.len() {
        output.push_str(&text[search_from..]);
    }
    output
}

fn redact_authorization_values(text: &str) -> String {
    let lower = text.to_lowercase();
    let needle = "authorization";
    let mut output = String::with_capacity(text.len());
    let mut search_from = 0;
    let mut scan = 0;
    let bytes = text.as_bytes();
    while let Some(found) = lower[scan..]
        .find(needle)
        .map(|offset| offset + scan)
    {
        let mut probe = found + needle.len();
        while probe < bytes.len()
            && (bytes[probe].is_ascii_whitespace() || bytes[probe] == b':')
        {
            probe += 1;
        }
        if probe < bytes.len() && bytes[probe] == b'"' {
            probe += 1;
        }
        let scheme_start = probe;
        while probe < bytes.len() && bytes[probe].is_ascii_alphanumeric() {
            probe += 1;
        }
        let scheme_present = probe > scheme_start;
        while probe < bytes.len() && bytes[probe].is_ascii_whitespace() {
            probe += 1;
        }
        let value_start = probe;
        while probe < bytes.len()
            && !bytes[probe].is_ascii_whitespace()
            && !matches!(bytes[probe], b',' | b'"' | b'}' | b')')
        {
            probe += 1;
        }
        if scheme_present && probe > value_start {
            output.push_str(&text[search_from..value_start]);
            output.push_str("<redacted>");
            search_from = probe;
            scan = probe;
            continue;
        }
        output.push_str(&text[search_from..probe]);
        search_from = probe;
        scan = probe;
    }
    if search_from < text.len() {
        output.push_str(&text[search_from..]);
    }
    output
}

fn redact_jwt_segments(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut output = String::with_capacity(text.len());
    let mut index = 0;
    while index < chars.len() {
        if chars[index] == 'e' || chars[index] == 'E' {
            let candidate: String = chars
                .iter()
                .skip(index)
                .take(3)
                .map(|character| character.to_ascii_lowercase())
                .collect();
            if candidate == "eyj" {
                let mut end = index + 3;
                while end < chars.len() && (chars[end].is_ascii_alphanumeric() || matches!(chars[end], '-' | '_' | '.')) {
                    end += 1;
                }
                let token: String = chars[index..end].iter().collect();
                if token.matches('.').count() >= 2 {
                    output.push_str("<redacted>");
                    index = end;
                    continue;
                }
            }
        }
        output.push(chars[index]);
        index += 1;
    }
    output
}

fn sanitize_error_detail(value: &str) -> String {
    let mut text = value.to_owned();
    for key in [
        "accessToken",
        "access_token",
        "refreshToken",
        "refresh_token",
        "token",
        "secret",
        "verifier",
        "code_verifier",
        "client_secret",
    ] {
        text = redact_json_values(&text, key);
    }
    text = redact_authorization_values(&text);
    redact_jwt_segments(&text)
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
        error_detail: Option<&str>,
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
        let safe_error_detail = error_detail
            .map(|value| sanitize_error_detail(&value))
            .map(|value| value.chars().take(256).collect::<String>());
        let record = Event {
            timestamp,
            component: "tauri",
            event: &safe_event,
            level: safe_level,
            issuer: safe_issuer,
            request_id: safe_request_id.as_deref(),
            error_code: safe_error_code.as_deref(),
            error_detail: safe_error_detail.as_deref(),
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
    error_detail: Option<String>,
) {
    logger.event(
        &event,
        level.as_deref(),
        issuer.as_deref(),
        request_id.as_deref(),
        error_code.as_deref(),
        error_detail.as_deref(),
    );
}

#[allow(dead_code)]
pub fn logger_for_dir(path: &Path) -> NativeLogger {
    NativeLogger::new(path.to_path_buf())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizer_redacts_authorization_headers_case_insensitively() {
        let text = "Authorization: DPoP eyJtoken.eyJpayload.sig and authorization: Bearer secret-value";
        let sanitized = sanitize_error_detail(text);
        assert!(!sanitized.contains("secret-value"));
        assert!(!sanitized.contains("eyJtoken"));
        assert!(sanitized.contains("<redacted>"));
    }

    #[test]
    fn sanitizer_redacts_token_keys_in_json_shapes() {
        let text = r#"{"accessToken":"abc123","refreshToken":"def456"}"#;
        let sanitized = sanitize_error_detail(text);
        assert!(!sanitized.contains("abc123"));
        assert!(!sanitized.contains("def456"));
        assert!(sanitized.contains("<redacted>"));
    }

    #[test]
    fn sanitizer_redacts_jwt_segments() {
        let text = "jwt=eyJhbGciOiJFUzI1NiJ9.eyJqdGkiOiJ4In0.abc123def456";
        let sanitized = sanitize_error_detail(text);
        assert!(!sanitized.contains("eyJhbGciOiJFUzI1NiJ9"));
        assert!(sanitized.contains("<redacted>"));
    }
}
