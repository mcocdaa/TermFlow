import { invoke } from '@tauri-apps/api/core'

export type NativeLogEvent = {
  event: string
  level?: 'info' | 'warn' | 'error' | undefined
  issuer?: string | undefined
  requestId?: string | undefined
  errorCode?: string | undefined
  errorDetail?: string | undefined
}

/** Keep native error logs useful while excluding credentials and request data. */
export function sanitizeNativeDetail(error: unknown): string {
  const raw = error instanceof Error ? `${error.name}: ${error.message}` : String(error)
  return raw
    .replace(/https?:\/\/[^\s]+/gi, '<url>')
    .replace(/termflow:\/\/[^\s]+/gi, '<callback>')
    .replace(/(authorization\s*:\s*(?:bearer|dpop)\s+)[^\s,]+/gi, '$1<redacted>')
    .replace(/(["']?(?:access_token|refresh_token|client_secret|code_verifier|device_code|user_code|dpop(?:_proof)?|authorization|token|secret)["']?\s*[=:]\s*["']?)[^"',}\s]+["']?/gi, '$1<redacted>')
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '<jwt>')
    .slice(0, 256)
}

/** Best-effort native diagnostics; failures must never block authorization. */
export async function logNativeEvent(entry: NativeLogEvent): Promise<void> {
  try {
    await invoke('native_log', {
      event: entry.event,
      level: entry.level,
      issuer: entry.issuer,
      requestId: entry.requestId,
      errorCode: entry.errorCode,
      errorDetail: entry.errorDetail,
    })
  } catch {
    // Logging is intentionally non-fatal, especially during first launch.
  }
}
