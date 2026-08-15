/**
 * Platform audio-capture port for the mobile press/hold voice flow
 * (M7b spec §4.3.1).
 *
 * The adapter lives in the Tauri client (`apps/clients/tauri/src/adapters/`):
 * Android uses MediaRecorder (`audio/webm;codecs=opus`), iOS and the shared
 * fallback use PCM capture + a 16 kHz mono 16-bit WAV encoder. client-core
 * only depends on this port, never on the browser media capture API directly.
 */

export interface RecordedAudio {
  /** In-memory capture only; never persisted to durable storage or logs. */
  readonly blob: Blob
  /** Client whitelist — the B side only accepts webm/wav (M7b spec §4.3.3). */
  readonly mimeType: 'audio/webm' | 'audio/wav'
  /** Measured capture duration in seconds. */
  readonly durationSeconds: number
}

export interface AudioRecorder {
  /**
   * Acquire the microphone and begin capture. Rejects with
   * {@link PermissionDeniedError} when the user (or the OS) denies the
   * microphone permission, or {@link RecorderUnavailableError} when no
   * usable capture device/pipeline exists. Every call acquires a fresh
   * `getUserMedia` session; `stop()`/`abort()` release all tracks.
   */
  start(): Promise<void>
  /**
   * Stop capture and return the in-memory blob. Implementations must be
   * idempotent: a second call resolves without error.
   */
  stop(): Promise<RecordedAudio>
  /** Cancel capture and discard everything, releasing the microphone tracks. */
  abort(): void
}

/**
 * The user or the OS denied microphone access. The UI shows the permission
 * guidance toast and keeps the button re-pressable (M7b spec §4.4).
 */
export class PermissionDeniedError extends Error {
  constructor() {
    super('permission_denied')
    this.name = 'PermissionDeniedError'
  }
}

/**
 * No usable microphone or capture pipeline exists (no device, device in
 * use, unsupported platform). The UI hides the voice button (M7b spec §4.4).
 */
export class RecorderUnavailableError extends Error {
  constructor(message = 'recorder_unavailable') {
    super(message)
    this.name = 'RecorderUnavailableError'
  }
}
