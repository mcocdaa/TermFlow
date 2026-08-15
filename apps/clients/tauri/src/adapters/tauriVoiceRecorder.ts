/**
 * Tauri voice recorder adapter (M7b spec §4.3.2): the platform audio-capture
 * port implementation for the mobile WebView.
 *
 * Platform ladder:
 * - Android: MediaRecorder `audio/webm;codecs=opus` when supported, else the
 *   shared PCM/WAV pipeline (spec §3.3.6 — both paths must exist as the
 *   recorder is the fallback for the other one).
 * - iOS: straight to the PCM/WAV pipeline (iOS MediaRecorder only produces
 *   audio/mp4, which the B side rejects with 415 — spec §3.3.3), using
 *   `AudioWorklet` with a `ScriptProcessorNode` fallback when the worklet
 *   module fails to load.
 * - Any platform: `getUserMedia` failures are classified per §4.3.2 —
 *   `NotAllowedError`/`SecurityError` → `PermissionDeniedError`, everything
 *   else (`NotFoundError`/`NotReadableError`/...) → `RecorderUnavailableError`.
 *
 * Every session acquires a fresh `getUserMedia` stream; `stop()`/`abort()`
 * release all tracks so the OS "recording" indicator never lingers.
 * Real-device capture behavior remains marked unverified until the M7 exit
 * device matrix (spec §3.3); the pure-JS selection/classification logic is
 * fully unit tested with injected fakes.
 */

import {
  PermissionDeniedError,
  RecorderUnavailableError,
  type AudioRecorder,
  type RecordedAudio,
  type VoiceClock,
} from '@termflow/client-core'
import {
  encodePcmToWav,
  PCM_WAV_MIME_TYPE,
  PCM_WAV_SAMPLE_RATE,
} from './pcmWavEncoder'

/** Android MediaRecorder output type (spec §4.3.2). */
export const ANDROID_WEBM_MIME_TYPE = 'audio/webm;codecs=opus'
export const WEBM_MIME_TYPE = 'audio/webm'

/** AudioWorklet processor name — keep in sync with `pcmCaptureWorklet.js`. */
export const PCM_CAPTURE_PROCESSOR_NAME = 'termflow-pcm-capture'

/** ScriptProcessorNode buffer size for the iOS fallback path. */
const PCM_SCRIPT_BUFFER_SIZE = 4096

/** Voice capability is scoped to the mobile targets (spec §4.2). */
export function isMobileVoicePlatform(platform: string): boolean {
  return platform === 'android' || platform === 'ios'
}

const AUDIO_CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
}

// ---------------------------------------------------------------------------
// Structural seams — the real DOM classes satisfy these; tests inject fakes.
// ---------------------------------------------------------------------------

export interface MediaRecorderLike {
  readonly state: RecordingState
  ondataavailable: ((event: { data: Blob }) => void) | null
  onerror: (() => void) | null
  onstop: (() => void) | null
  start(timeslice?: number): void
  stop(): void
}

export interface MediaRecorderCtorLike {
  readonly isTypeSupported: (type: string) => boolean
  new (stream: MediaStream, options?: MediaRecorderOptions): MediaRecorderLike
}

export interface PcmGraphNodeLike {
  connect(destination: unknown): unknown
  disconnect(): void
}

export interface PcmAudioProcessEventLike {
  readonly inputBuffer: { getChannelData(channel: number): Float32Array }
}

export interface PcmScriptProcessorNodeLike extends PcmGraphNodeLike {
  onaudioprocess: ((event: PcmAudioProcessEventLike) => void) | null
}

export interface PcmWorkletNodeLike extends PcmGraphNodeLike {
  readonly port: { onmessage: ((event: MessageEvent) => void) | null }
}

export interface PcmAudioContextLike {
  readonly sampleRate: number
  readonly destination: unknown
  readonly audioWorklet: { addModule(moduleUrl: string): Promise<void> }
  createMediaStreamSource(stream: MediaStream): PcmGraphNodeLike
  createScriptProcessor(
    bufferSize?: number,
    numberOfInputChannels?: number,
    numberOfOutputChannels?: number,
  ): PcmScriptProcessorNodeLike
  resume(): Promise<void>
  close(): Promise<void>
}

export interface PcmAudioContextCtorLike {
  new (options: AudioContextOptions): PcmAudioContextLike
}

export interface PcmWorkletNodeCtorLike {
  new (context: PcmAudioContextLike, name: string, options?: AudioWorkletNodeOptions): PcmWorkletNodeLike
}

export interface TauriVoiceRecorderOptions {
  /** `@tauri-apps/plugin-os` platform() value: 'android' | 'ios' | ...
   *  Picks the capture path (§4.3.2). */
  platform: string
  /** Test seam — defaults to navigator.mediaDevices. */
  mediaDevices?: Pick<MediaDevices, 'getUserMedia'>
  /** Test seam — defaults to globalThis.MediaRecorder. */
  MediaRecorderCtor?: MediaRecorderCtorLike
  /** Test seam — defaults to globalThis.AudioContext. */
  AudioContextCtor?: PcmAudioContextCtorLike
  /** Test seam — defaults to globalThis.AudioWorkletNode. */
  AudioWorkletNodeCtor?: PcmWorkletNodeCtorLike
  /** Test seam — defaults to a Date.now clock. */
  clock?: VoiceClock
}

// ---------------------------------------------------------------------------
// Error classification (spec §4.3.2)
// ---------------------------------------------------------------------------

function classifyGetUserMediaError(error: unknown): Error {
  const name = typeof error === 'object' && error !== null ? (error as { name?: unknown }).name : undefined
  if (name === 'NotAllowedError' || name === 'SecurityError') return new PermissionDeniedError()
  return new RecorderUnavailableError('get_user_media_failed')
}

// ---------------------------------------------------------------------------
// Capture engines
// ---------------------------------------------------------------------------

interface CaptureEngine {
  start(): Promise<void>
  stop(): Promise<RecordedAudio>
  abort(): void
}

class MediaRecorderEngine implements CaptureEngine {
  private readonly chunks: Blob[] = []
  private readonly recorder: MediaRecorderLike
  private pendingStop: Promise<RecordedAudio> | null = null

  constructor(
    stream: MediaStream,
    ctor: MediaRecorderCtorLike,
    private readonly startedAtMs: number,
    private readonly clock: VoiceClock,
  ) {
    this.recorder = new ctor(stream, { mimeType: ANDROID_WEBM_MIME_TYPE })
    this.recorder.ondataavailable = (event) => {
      if (event.data.size > 0) this.chunks.push(event.data)
    }
  }

  async start(): Promise<void> {
    this.recorder.start()
  }

  stop(): Promise<RecordedAudio> {
    if (this.pendingStop !== null) return this.pendingStop
    this.pendingStop = new Promise<RecordedAudio>((resolve, reject) => {
      this.recorder.onstop = () => {
        resolve({
          blob: new Blob(this.chunks, { type: WEBM_MIME_TYPE }),
          mimeType: WEBM_MIME_TYPE,
          durationSeconds: Math.max(0, (this.clock.now() - this.startedAtMs) / 1000),
        })
      }
      this.recorder.onerror = () => reject(new RecorderUnavailableError('media_recorder_failed'))
      try {
        this.recorder.stop()
      } catch {
        reject(new RecorderUnavailableError('media_recorder_failed'))
      }
    })
    return this.pendingStop
  }

  abort(): void {
    // A stop already in flight must keep its handlers so the pending promise
    // can settle; only an actively recording engine is torn down silently.
    if (this.pendingStop === null && this.recorder.state !== 'inactive') {
      this.recorder.onstop = null
      this.recorder.onerror = null
      try {
        this.recorder.stop()
      } catch {
        // Already inactive — nothing to tear down.
      }
    }
    this.chunks.length = 0
  }
}

class PcmEngine implements CaptureEngine {
  private context: PcmAudioContextLike | null = null
  private source: PcmGraphNodeLike | null = null
  private node: PcmGraphNodeLike | null = null
  private readonly chunks: Float32Array[] = []
  private sampleCount = 0
  private stopPromise: Promise<RecordedAudio> | null = null

  constructor(
    private readonly stream: MediaStream,
    private readonly audioContextCtor: PcmAudioContextCtorLike | undefined,
    private readonly workletNodeCtor: PcmWorkletNodeCtorLike | undefined,
  ) {}

  static async create(
    stream: MediaStream,
    audioContextCtor: PcmAudioContextCtorLike | undefined,
    workletNodeCtor: PcmWorkletNodeCtorLike | undefined,
  ): Promise<PcmEngine> {
    const engine = new PcmEngine(stream, audioContextCtor, workletNodeCtor)
    await engine.attach()
    return engine
  }

  private async attach(): Promise<void> {
    if (this.audioContextCtor === undefined) throw new RecorderUnavailableError('audio_context_unavailable')
    let context: PcmAudioContextLike
    try {
      // Request the encoder's sample rate up front: the WebAudio graph then
      // resamples the hardware stream to 16 kHz before the worklet sees it.
      context = new this.audioContextCtor({ sampleRate: PCM_WAV_SAMPLE_RATE })
      await context.resume()
    } catch {
      throw new RecorderUnavailableError('audio_context_unavailable')
    }
    this.context = context
    const source = context.createMediaStreamSource(this.stream)
    this.source = source

    let node: PcmGraphNodeLike | null = null
    if (this.workletNodeCtor !== undefined) {
      try {
        // Bundled same-origin asset (spec §4.10: no blob/worker scripts);
        // `?no-inline` keeps Vite from inlining it as a data: URI.
        const moduleUrl = new URL('./pcmCaptureWorklet.js?no-inline', import.meta.url).href
        await context.audioWorklet.addModule(moduleUrl)
        const worklet = new this.workletNodeCtor(context, PCM_CAPTURE_PROCESSOR_NAME)
        worklet.port.onmessage = (event) => this.accumulateMessage(event)
        node = worklet
      } catch {
        node = null // fall through to the ScriptProcessorNode path
      }
    }
    if (node === null) {
      const script = context.createScriptProcessor(PCM_SCRIPT_BUFFER_SIZE, 1, 1)
      script.onaudioprocess = (event) => this.accumulate(event.inputBuffer.getChannelData(0))
      node = script
    }
    this.node = node
    // The node writes silence to its output, so connecting to the destination
    // only keeps the graph pulling — the user never hears their own mic.
    source.connect(node)
    node.connect(context.destination)
  }

  private accumulateMessage(event: MessageEvent): void {
    if (event.data instanceof Float32Array) this.accumulate(event.data)
  }

  async start(): Promise<void> {
    // The graph is attached and running by the time `create()` resolves.
  }

  private accumulate(data: Float32Array): void {
    // Copy: worklet buffers are transferred in and ScriptProcessor buffers
    // are reused by the engine, so ownership must not be shared.
    this.chunks.push(new Float32Array(data))
    this.sampleCount += data.length
  }

  stop(): Promise<RecordedAudio> {
    if (this.stopPromise === null) this.stopPromise = this.settle()
    return this.stopPromise
  }

  private async settle(): Promise<RecordedAudio> {
    this.disconnectGraph()
    if (this.context !== null) {
      const context = this.context
      this.context = null
      try {
        await context.close()
      } catch {
        // Already closed — nothing to do.
      }
    }
    const buffer = encodePcmToWav(this.flatten())
    return {
      blob: new Blob([buffer], { type: PCM_WAV_MIME_TYPE }),
      mimeType: PCM_WAV_MIME_TYPE,
      durationSeconds: this.sampleCount / PCM_WAV_SAMPLE_RATE,
    }
  }

  abort(): void {
    this.chunks.length = 0
    this.sampleCount = 0
    this.disconnectGraph()
    if (this.context !== null) {
      const context = this.context
      this.context = null
      void context.close().catch(() => {})
    }
  }

  private disconnectGraph(): void {
    if (this.node !== null) {
      try {
        this.node.disconnect()
      } catch {
        // Node may already be torn down.
      }
      this.node = null
    }
    if (this.source !== null) {
      try {
        this.source.disconnect()
      } catch {
        // Source may already be torn down.
      }
      this.source = null
    }
  }

  private flatten(): Float32Array {
    const result = new Float32Array(this.sampleCount)
    let offset = 0
    for (const chunk of this.chunks) {
      result.set(chunk, offset)
      offset += chunk.length
    }
    return result
  }
}

// ---------------------------------------------------------------------------
// Recorder
// ---------------------------------------------------------------------------

export function createTauriVoiceRecorder(options: TauriVoiceRecorderOptions): AudioRecorder {
  const mediaDevices = options.mediaDevices ?? navigator.mediaDevices
  // The structural seams exist so tests can inject fakes; the real DOM
  // constructors are cast across the boundary (their event-handler property
  // types are contravariant-incompatible with the minimal seams).
  const mediaRecorderCtor = options.MediaRecorderCtor ?? (globalThis.MediaRecorder as unknown as MediaRecorderCtorLike | undefined)
  const audioContextCtor = options.AudioContextCtor ?? (globalThis.AudioContext as unknown as PcmAudioContextCtorLike | undefined)
  const workletNodeCtor = options.AudioWorkletNodeCtor ?? (globalThis.AudioWorkletNode as unknown as PcmWorkletNodeCtorLike | undefined)
  const clock = options.clock ?? { now: () => Date.now() }

  let stream: MediaStream | null = null
  let engine: CaptureEngine | null = null
  let settled: RecordedAudio | null = null
  let startedAtMs = 0

  function releaseStream(): void {
    for (const track of stream?.getTracks() ?? []) track.stop()
    stream = null
  }

  function tryMediaRecorderEngine(acquired: MediaStream): CaptureEngine | null {
    if (typeof mediaRecorderCtor === 'undefined') return null
    if (!mediaRecorderCtor.isTypeSupported(ANDROID_WEBM_MIME_TYPE)) return null
    try {
      return new MediaRecorderEngine(acquired, mediaRecorderCtor, startedAtMs, clock)
    } catch {
      // Constructor rejected the mime despite isTypeSupported — use PCM/WAV.
      return null
    }
  }

  async function start(): Promise<void> {
    if (engine !== null || settled !== null) return // idempotent
    if (mediaDevices === undefined || typeof mediaDevices.getUserMedia !== 'function') {
      throw new RecorderUnavailableError('media_devices_unavailable')
    }
    let acquired: MediaStream
    try {
      acquired = await mediaDevices.getUserMedia(AUDIO_CONSTRAINTS)
    } catch (error) {
      throw classifyGetUserMediaError(error)
    }
    stream = acquired
    startedAtMs = clock.now()
    try {
      if (options.platform === 'android') {
        const webm = tryMediaRecorderEngine(acquired)
        if (webm !== null) {
          engine = webm
        }
      }
      if (engine === null) {
        engine = await PcmEngine.create(acquired, audioContextCtor, workletNodeCtor)
      }
      await engine.start()
    } catch (error) {
      // Permission was granted but the capture pipeline failed — release the
      // mic, drop the half-built engine and report unavailability (the UI
      // hides the button, spec §4.4).
      engine = null
      releaseStream()
      throw error instanceof RecorderUnavailableError
        ? error
        : new RecorderUnavailableError('capture_pipeline_unavailable')
    }
  }

  async function stop(): Promise<RecordedAudio> {
    if (settled !== null) return settled // idempotent
    const current = engine
    if (current === null) throw new RecorderUnavailableError('recorder_not_started')
    const result = await current.stop()
    if (engine === current) engine = null
    settled = result
    releaseStream()
    return result
  }

  function abort(): void {
    engine?.abort()
    engine = null
    settled = null
    releaseStream()
  }

  return { start, stop, abort }
}
