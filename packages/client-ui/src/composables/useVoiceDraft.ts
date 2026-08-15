/**
 * Controller → Vue reactive bridge for the mobile press/hold voice flow
 * (M7b spec §4.2/§4.7.3).
 *
 * The parent that owns the conversation composer (M6b) calls `useVoiceDraft`
 * once and renders `VoiceInputButton` + `TranscriptDraftSheet` below it; both
 * components `inject(voiceDraftKey)` to reach this bridge. Until M6b lands
 * there is no runtime integration point — the components are covered by
 * component tests instead (M7b spec §4.7.3).
 *
 * Responsibilities:
 * - build the client-core `VoiceDraftController` from the injected runtime
 *   (recorder/uploader from `runtime.voice`, draft API from `runtime.api`,
 *   clock/scheduler from `runtime.clock`, the draft slot from
 *   `runtime.voice.draftStore` with an in-memory fallback, console logger);
 * - expose the controller state as a Vue ref;
 * - forward controller toasts to the app-level `BottomToast` (errors land in
 *   `role="alert"`, the submit success in `role="status"`);
 * - forward the submit-success signal as `draft-submitted` events;
 * - cancel recording when the app goes to background (`visibilitychange`);
 * - restore a draft left in the draft store (M7b spec §4.7.4).
 */

import {
  createTranscriptionApi,
  VoiceDraftController,
  VOICE_MESSAGES,
  type AudioRecorder,
  type AudioUploader,
  type SubmitMessage,
  type VoiceDraftApi,
  type VoiceDraftState,
  type VoiceLogger,
  type VoiceStorage,
} from '@termflow/client-core'
import { computed, inject, onScopeDispose, provide, ref, type InjectionKey, type Ref } from 'vue'
import { useClientRuntime } from '../runtime'
import { useBottomToast } from './useBottomToast'

export const voiceDraftKey: InjectionKey<VoiceDraftBridge> = Symbol('termflow-voice-draft')

export interface UseVoiceDraftOptions {
  bindingId: string
  conversationId: string
  /**
   * M4.5 `POST /conversations/{id}/messages` submit. Injected explicitly
   * because the B side endpoint is not implemented yet (M7b spec §4.8) — the
   * runtime cannot provide it.
   */
  submit: SubmitMessage
  /**
   * B side `speech_to_text_enabled` capability flag as a getter (the parent
   * owns the capability fetch; M7b spec §4.9).
   */
  speechToTextEnabled: () => boolean
  /** Test seam — defaults to the runtime's transcription JSON API. */
  draftApi?: VoiceDraftApi
  /** Test seam — defaults to the runtime's draft store, then an in-memory slot. */
  storage?: VoiceStorage
  /** Test seam — defaults to `console.error` (diagnostics only, never text/audio). */
  logger?: VoiceLogger
}

export interface VoiceDraftBridge {
  readonly state: Readonly<Ref<VoiceDraftState>>
  /** `runtime.voice` present ∧ `voice.enabled()` — the platform/device gate. */
  readonly available: Readonly<Ref<boolean>>
  press(): void
  release(): void
  slide(distancePx: number): void
  cancel(): void
  cancelUpload(): void
  retryUpload(): void
  reRecord(): void
  confirm(text: string): void
  retrySubmit(): void
  discard(): void
  restore(): void
  /** Subscribe to submit success; returns an unsubscribe function. */
  onDraftSubmitted(listener: (draftId: string) => void): () => void
}

/**
 * In-memory fallback for the draft slot (M7b spec §4.7.4). Deliberately
 * transient: a draft does not survive a page reload. The real platform
 * adapters are injected through `runtime.voice.draftStore` by
 * `apps/clients/{web,tauri}` — wiring lands in a follow-up task.
 */
function inMemoryVoiceStorage(): VoiceStorage {
  const slots = new Map<string, string>()
  return {
    getItem: (key) => slots.get(key) ?? null,
    setItem: (key, value) => {
      slots.set(key, value)
    },
    removeItem: (key) => {
      slots.delete(key)
    },
  }
}

const defaultLogger: VoiceLogger = {
  error: (message, details) => {
    // eslint-disable-next-line no-console
    console.error(message, details)
  },
}

export function useVoiceDraft(options: UseVoiceDraftOptions): VoiceDraftBridge {
  const runtime = useClientRuntime()
  const toast = useBottomToast()
  const state = ref<VoiceDraftState>({ status: 'idle' })
  const submittedListeners = new Set<(draftId: string) => void>()
  /** Draft id of the most recent draft state — captured so the submit-success
   *  toast can be forwarded with the right id (the controller emits the toast
   *  after it has already transitioned back to idle). */
  let lastDraftId: string | null = null

  const voice = runtime.voice
  const voiceEnabled = voice !== undefined && voice.enabled()
  const available = computed(() => runtime.voice !== undefined && runtime.voice.enabled())

  // Recorder/uploader are unreachable while the controller is disabled
  // (press() no-ops when `speechToTextEnabled` is false) — the stubs exist
  // only to satisfy the constructor in runtimes without a voice capability.
  const recorderStub = (): AudioRecorder => {
    throw new Error('voice recorder requested but the runtime has no voice capability')
  }
  const uploaderStub: AudioUploader = () => {
    throw new Error('voice uploader requested but the runtime has no voice capability')
  }

  const transcriptionApi = createTranscriptionApi(runtime.api.request)
  const draftApi: VoiceDraftApi = options.draftApi ?? {
    confirm: (draftId, signal) => transcriptionApi.confirmDraft(draftId, signal),
    cancel: (draftId, signal) => transcriptionApi.cancelDraft(draftId, signal),
  }

  const controller = new VoiceDraftController({
    createRecorder: voice?.createRecorder ?? recorderStub,
    uploader: voice?.uploadAudio ?? uploaderStub,
    draftApi,
    submit: options.submit,
    clock: runtime.clock,
    // VoiceScheduler speaks the codebase `set`/`clear` port shape; adapt the
    // runtime clock (which mirrors the browser timer names) onto it.
    scheduler: {
      set: (fn, ms) => runtime.clock.setTimeout(fn, ms),
      clear: (handle) => runtime.clock.clearTimeout(handle),
    },
    storage: options.storage ?? voice?.draftStore ?? inMemoryVoiceStorage(),
    logger: options.logger ?? defaultLogger,
    bindingId: options.bindingId,
    conversationId: options.conversationId,
    // Snapshot at construction — the controller has no notion of the flag
    // changing mid-session (M7b-1 design); the button re-checks the runtime
    // gate at render time instead.
    speechToTextEnabled: voiceEnabled && options.speechToTextEnabled(),
    callbacks: {
      onState: (next) => {
        if (next.status === 'draft') lastDraftId = next.draft.draftId
        state.value = next
      },
      onToast: ({ message }) => {
        toast.show({ text: message, tone: message === VOICE_MESSAGES.submitted ? 'success' : 'error' })
        if (message === VOICE_MESSAGES.submitted && lastDraftId !== null) {
          for (const listener of submittedListeners) listener(lastDraftId)
        }
      },
    },
  })

  // Session recovery: reopen a draft left in the draft store (M7b spec §4.7.4).
  controller.restore()

  const unsubscribeVisibility = runtime.visibility.subscribe(() => {
    if (runtime.visibility.isHidden()) controller.cancel()
  })

  onScopeDispose(() => {
    unsubscribeVisibility()
    // Hygiene: release the microphone if the composer unmounts mid-press.
    controller.cancel()
  })

  const bridge: VoiceDraftBridge = {
    state,
    available,
    press: () => controller.press(),
    release: () => controller.release(),
    slide: (distancePx: number) => controller.slide(distancePx),
    cancel: () => controller.cancel(),
    cancelUpload: () => controller.cancelUpload(),
    retryUpload: () => controller.retryUpload(),
    reRecord: () => controller.reRecord(),
    confirm: (text: string) => controller.confirm(text),
    retrySubmit: () => controller.retrySubmit(),
    discard: () => controller.discard(),
    restore: () => controller.restore(),
    onDraftSubmitted: (listener) => {
      submittedListeners.add(listener)
      return () => submittedListeners.delete(listener)
    },
  }

  provide(voiceDraftKey, bridge)
  return bridge
}

/** Components inject the bridge; a missing provider renders no voice UI. */
export function useInjectedVoiceDraft(): VoiceDraftBridge | undefined {
  return inject(voiceDraftKey, undefined)
}
