import { invoke } from '@tauri-apps/api/core'

export type NativeLogEvent = {
  event: string
  level?: 'info' | 'warn' | 'error' | undefined
  issuer?: string | undefined
  requestId?: string | undefined
  errorCode?: string | undefined
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
    })
  } catch {
    // Logging is intentionally non-fatal, especially during first launch.
  }
}
