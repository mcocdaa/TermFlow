import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PermissionDeniedError, RecorderUnavailableError, type RecordedAudio } from '@termflow/client-core'
import { PCM_WAV_HEADER_BYTES } from './pcmWavEncoder'
import {
  createTauriVoiceRecorder,
  isMobileVoicePlatform,
  type MediaRecorderCtorLike,
  type MediaRecorderLike,
  type PcmAudioContextCtorLike,
  type PcmAudioContextLike,
  type PcmAudioProcessEventLike,
  type PcmGraphNodeLike,
  type PcmScriptProcessorNodeLike,
  type PcmWorkletNodeCtorLike,
  type PcmWorkletNodeLike,
} from './tauriVoiceRecorder'

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

function fakeStream(): { stream: MediaStream; trackStop: ReturnType<typeof vi.fn> } {
  const trackStop = vi.fn()
  const stream = { getTracks: () => [{ stop: trackStop }] } as unknown as MediaStream
  return { stream, trackStop }
}

class FakeMediaRecorder {
  static isTypeSupported = vi.fn<(type: string) => boolean>()
  static instances: FakeMediaRecorder[] = []
  state: RecordingState = 'inactive'
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onerror: (() => void) | null = null
  onstop: (() => void) | null = null
  chunks: Blob[] = []
  /** When true, stop() only flips the state — the test fires the events. */
  manualStop = false
  constructor(
    public readonly stream: MediaStream,
    public readonly options: MediaRecorderOptions,
  ) {
    FakeMediaRecorder.instances.push(this)
  }
  start(): void {
    this.state = 'recording'
  }
  stop(): void {
    this.state = 'inactive'
    if (this.manualStop) return
    for (const chunk of this.chunks) this.ondataavailable?.({ data: chunk })
    this.onstop?.()
  }
}

class FakeSourceNode implements PcmGraphNodeLike {
  destination: unknown = null
  disconnected = false
  connect(destination: unknown): unknown {
    this.destination = destination
    return this
  }
  disconnect(): void {
    this.disconnected = true
  }
}

class FakeScriptNode implements PcmScriptProcessorNodeLike {
  onaudioprocess: ((event: PcmAudioProcessEventLike) => void) | null = null
  destination: unknown = null
  disconnected = false
  connect(destination: unknown): unknown {
    this.destination = destination
    return this
  }
  disconnect(): void {
    this.disconnected = true
  }
}

class FakeWorkletNode implements PcmWorkletNodeLike {
  readonly port: { onmessage: ((event: MessageEvent) => void) | null } = { onmessage: null }
  destination: unknown = null
  disconnected = false
  connect(destination: unknown): unknown {
    this.destination = destination
    return this
  }
  disconnect(): void {
    this.disconnected = true
  }
}

class FakeAudioContext implements PcmAudioContextLike {
  static instances: FakeAudioContext[] = []
  static addModule = vi.fn<(moduleUrl: string) => Promise<void>>()
  readonly sampleRate = 16000
  readonly destination = {}
  readonly audioWorklet = { addModule: FakeAudioContext.addModule }
  readonly sourceNodes: FakeSourceNode[] = []
  readonly scriptNodes: FakeScriptNode[] = []
  closed = false
  resumed = false
  constructor(public readonly options: AudioContextOptions) {
    FakeAudioContext.instances.push(this)
  }
  createMediaStreamSource(_stream: MediaStream): PcmGraphNodeLike {
    const node = new FakeSourceNode()
    this.sourceNodes.push(node)
    return node
  }
  createScriptProcessor(): PcmScriptProcessorNodeLike {
    const node = new FakeScriptNode()
    this.scriptNodes.push(node)
    return node
  }
  async resume(): Promise<void> {
    this.resumed = true
  }
  async close(): Promise<void> {
    this.closed = true
  }
}

function createFakeWorkletNodeCtor(): { ctor: PcmWorkletNodeCtorLike; nodes: FakeWorkletNode[] } {
  const nodes: FakeWorkletNode[] = []
  function ctor(_context: PcmAudioContextLike, _name: string): PcmWorkletNodeLike {
    const node = new FakeWorkletNode()
    nodes.push(node)
    return node
  }
  return { ctor: ctor as unknown as PcmWorkletNodeCtorLike, nodes }
}

class ThrowingMediaRecorderCtor {
  static isTypeSupported = () => true
  constructor(_stream: MediaStream, _options?: MediaRecorderOptions) {
    throw new Error('NotSupportedError')
  }
}

class ThrowingAudioContextCtor {
  constructor(_options?: AudioContextOptions) {
    throw new Error('context unavailable')
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface Harness {
  recorder: ReturnType<typeof createTauriVoiceRecorder>
  getUserMedia: ReturnType<typeof vi.fn>
  stream: MediaStream
  trackStop: ReturnType<typeof vi.fn>
  clock: { now: () => number }
  setNow(ms: number): void
  worklet: { ctor: PcmWorkletNodeCtorLike; nodes: FakeWorkletNode[] }
}

function createHarness(platform: string, overrides: Partial<Parameters<typeof createTauriVoiceRecorder>[0]> = {}): Harness {
  let nowMs = 5000
  const getUserMedia = vi.fn()
  const { stream, trackStop } = fakeStream()
  getUserMedia.mockResolvedValue(stream)
  const worklet = createFakeWorkletNodeCtor()
  const recorder = createTauriVoiceRecorder({
    platform,
    mediaDevices: { getUserMedia } as unknown as Pick<MediaDevices, 'getUserMedia'>,
    MediaRecorderCtor: FakeMediaRecorder as unknown as MediaRecorderCtorLike,
    AudioContextCtor: FakeAudioContext as unknown as PcmAudioContextCtorLike,
    AudioWorkletNodeCtor: worklet.ctor,
    clock: { now: () => nowMs },
    ...overrides,
  })
  return {
    recorder,
    getUserMedia,
    stream,
    trackStop,
    clock: { now: () => nowMs },
    setNow: (ms) => {
      nowMs = ms
    },
    worklet,
  }
}

function asMessageEvent(data: Float32Array): MessageEvent {
  return { data } as unknown as MessageEvent
}

function asAudioProcessEvent(samples: Float32Array): PcmAudioProcessEventLike {
  return { inputBuffer: { getChannelData: () => samples } }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('createTauriVoiceRecorder platform selection (§4.3.2)', () => {
  beforeEach(() => {
    FakeMediaRecorder.isTypeSupported.mockReset()
    FakeMediaRecorder.instances = []
    FakeAudioContext.instances = []
    FakeAudioContext.addModule.mockReset().mockResolvedValue(undefined)
  })

  it('requests a fresh mono echo-cancelled stream and records webm/opus on Android', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('android')
    await harness.recorder.start()

    expect(harness.getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    })
    const mediaRecorder = FakeMediaRecorder.instances[0]
    expect(mediaRecorder).toBeDefined()
    expect(mediaRecorder?.options.mimeType).toBe('audio/webm;codecs=opus')
    expect(mediaRecorder?.state).toBe('recording')

    mediaRecorder?.chunks.push(new Blob(['a']), new Blob(['bc']))
    harness.setNow(8000)
    const result = await harness.recorder.stop()

    expect(result).toMatchObject({ mimeType: 'audio/webm', durationSeconds: 3 })
    expect(result.blob.type).toBe('audio/webm')
    expect(result.blob.size).toBe(3)
    expect(harness.trackStop).toHaveBeenCalledTimes(1)
  })

  it('falls back to PCM/WAV on Android when webm/opus is unsupported', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(false)
    const harness = createHarness('android')
    await harness.recorder.start()

    expect(FakeMediaRecorder.instances).toHaveLength(0)
    expect(FakeAudioContext.instances).toHaveLength(1)
    expect(FakeAudioContext.instances[0]?.options.sampleRate).toBe(16000)

    harness.worklet.nodes[0]?.port.onmessage?.(asMessageEvent(new Float32Array([0.5, -0.5])))
    const result = await harness.recorder.stop()

    expect(result.mimeType).toBe('audio/wav')
    expect(result.blob.size).toBe(PCM_WAV_HEADER_BYTES + 4)
    expect(result.durationSeconds).toBe(2 / 16000)
  })

  it('uses the PCM/WAV pipeline on iOS even when MediaRecorder supports webm', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('ios')
    await harness.recorder.start()

    expect(FakeMediaRecorder.instances).toHaveLength(0)
    expect(FakeAudioContext.instances).toHaveLength(1)
    harness.worklet.nodes[0]?.port.onmessage?.(asMessageEvent(new Float32Array([0.1])))
    const result = await harness.recorder.stop()
    expect(result.mimeType).toBe('audio/wav')
  })

  it('falls back to ScriptProcessorNode when the AudioWorklet module fails to load', async () => {
    FakeAudioContext.addModule.mockRejectedValue(new Error('AbortError'))
    const harness = createHarness('ios')
    await harness.recorder.start()

    const context = FakeAudioContext.instances[0]
    expect(context?.audioWorklet.addModule).toHaveBeenCalledWith(expect.stringContaining('pcmCaptureWorklet.js'))
    expect(harness.worklet.nodes).toHaveLength(0)
    expect(context?.scriptNodes).toHaveLength(1)

    context?.scriptNodes[0]?.onaudioprocess?.(asAudioProcessEvent(new Float32Array([0.25, 0, -0.25])))
    const result = await harness.recorder.stop()

    expect(result.mimeType).toBe('audio/wav')
    expect(result.blob.size).toBe(PCM_WAV_HEADER_BYTES + 6)
    expect(result.durationSeconds).toBe(3 / 16000)
    expect(context?.closed).toBe(true)
  })

  it('falls back to PCM/WAV when the MediaRecorder constructor rejects the mime', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('android', {
      MediaRecorderCtor: ThrowingMediaRecorderCtor as unknown as MediaRecorderCtorLike,
    })
    await harness.recorder.start()

    expect(FakeAudioContext.instances).toHaveLength(1)
    await harness.recorder.stop()
  })

  it('releases the granted stream when the capture pipeline fails after permission', async () => {
    const harness = createHarness('ios', {
      AudioContextCtor: ThrowingAudioContextCtor as unknown as PcmAudioContextCtorLike,
    })
    await expect(harness.recorder.start()).rejects.toBeInstanceOf(RecorderUnavailableError)
    expect(harness.trackStop).toHaveBeenCalledTimes(1)
  })
})

describe('createTauriVoiceRecorder error classification (§4.3.2)', () => {
  function rejectStart(platform: string, error: unknown) {
    const harness = createHarness(platform)
    harness.getUserMedia.mockRejectedValue(error)
    return harness.recorder.start()
  }

  it.each(['NotAllowedError', 'SecurityError'])('maps %s to PermissionDeniedError', async (name) => {
    await expect(rejectStart('android', Object.assign(new Error('denied'), { name }))).rejects.toBeInstanceOf(PermissionDeniedError)
  })

  it.each(['NotFoundError', 'NotReadableError', 'OverconstrainedError', 'TypeError'])('maps %s to RecorderUnavailableError', async (name) => {
    await expect(rejectStart('android', Object.assign(new Error('unavailable'), { name }))).rejects.toBeInstanceOf(RecorderUnavailableError)
  })

  it('maps missing mediaDevices to RecorderUnavailableError', async () => {
    const harness = createHarness('android', { mediaDevices: {} as unknown as Pick<MediaDevices, 'getUserMedia'> })
    await expect(harness.recorder.start()).rejects.toBeInstanceOf(RecorderUnavailableError)
  })
})

describe('createTauriVoiceRecorder lifecycle (§4.3.1)', () => {
  beforeEach(() => {
    FakeMediaRecorder.isTypeSupported.mockReset()
    FakeMediaRecorder.instances = []
    FakeAudioContext.instances = []
    FakeAudioContext.addModule.mockReset().mockResolvedValue(undefined)
  })

  it('makes start idempotent while recording', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('android')
    await harness.recorder.start()
    await harness.recorder.start()

    expect(harness.getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('makes stop idempotent after settling', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('android')
    await harness.recorder.start()
    FakeMediaRecorder.instances[0]?.chunks.push(new Blob(['a']))

    const first = await harness.recorder.stop()
    const second = await harness.recorder.stop()

    expect(second).toBe(first)
    expect(harness.trackStop).toHaveBeenCalledTimes(1)
  })

  it('rejects stop when the recorder was never started', async () => {
    const harness = createHarness('android')
    await expect(harness.recorder.stop()).rejects.toBeInstanceOf(RecorderUnavailableError)
  })

  it('releases all tracks on abort and discards the capture', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('android')
    await harness.recorder.start()

    harness.recorder.abort()

    expect(harness.trackStop).toHaveBeenCalledTimes(1)
    await expect(harness.recorder.stop()).rejects.toBeInstanceOf(RecorderUnavailableError)
  })

  it('releases all tracks when stop fails mid-capture', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('android')
    await harness.recorder.start()
    const instance = FakeMediaRecorder.instances[0]
    if (instance === undefined) throw new Error('no MediaRecorder instance created')
    instance.manualStop = true

    const pending = harness.recorder.stop()
    instance.onerror?.()

    await expect(pending).rejects.toBeInstanceOf(RecorderUnavailableError)
    expect(harness.trackStop).toHaveBeenCalledTimes(1)
  })

  it('keeps the captured chunks when abort races an in-flight stop', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('android')
    await harness.recorder.start()
    const instance = FakeMediaRecorder.instances[0]
    if (instance === undefined) throw new Error('no MediaRecorder instance created')
    instance.ondataavailable?.({ data: new Blob(['a']) }) // timeslice delivery during recording
    instance.manualStop = true

    const pending = harness.recorder.stop()
    harness.recorder.abort()
    instance.ondataavailable?.({ data: new Blob(['b']) }) // final dataavailable on stop
    instance.onstop?.()

    // The in-flight stop settles with the full capture; the wrapper then
    // discards it because the abort won the race.
    const result = await pending
    expect(result).toMatchObject({ mimeType: 'audio/webm' })
    expect(result.blob.size).toBe(2)
    await expect(harness.recorder.stop()).rejects.toBeInstanceOf(RecorderUnavailableError)
  })

  it('keeps the recording blob in memory only and never touches storage', async () => {
    FakeMediaRecorder.isTypeSupported.mockReturnValue(true)
    const harness = createHarness('android')
    await harness.recorder.start()
    FakeMediaRecorder.instances[0]?.chunks.push(new Blob(['webm-bytes']))

    const result: RecordedAudio = await harness.recorder.stop()
    expect(result.blob).toBeInstanceOf(Blob)
    expect(result.mimeType).toBe('audio/webm')
  })
})

describe('isMobileVoicePlatform', () => {
  it('enables voice capture only on android and ios', () => {
    expect(isMobileVoicePlatform('android')).toBe(true)
    expect(isMobileVoicePlatform('ios')).toBe(true)
    expect(isMobileVoicePlatform('windows')).toBe(false)
    expect(isMobileVoicePlatform('linux')).toBe(false)
    expect(isMobileVoicePlatform('macos')).toBe(false)
  })
})
