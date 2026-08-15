/**
 * Shared harness for M7b voice component tests (spec §6.2).
 *
 * Mounts the whole voice flow — `VoiceInputButton` + `TranscriptDraftSheet` +
 * `BottomToast` — above a real `useVoiceDraft` bridge wired to a fake runtime,
 * exactly like the future M6b composer will. Tests drive the button through
 * DOM pointer/keyboard events and reach the draft sheet through the fake
 * recorder/uploader.
 */

import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import type { SubmitMessage, VoiceDraftApi, VoiceLogger, VoiceStorage } from '@termflow/client-core'
import type { ClientRuntime, ClientVoiceRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import type { VoiceDraftBridge } from '../composables/useVoiceDraft'
import { useVoiceDraft } from '../composables/useVoiceDraft'
import BottomToast from '../components/common/BottomToast.vue'
import TranscriptDraftSheet from '../components/agent/TranscriptDraftSheet.vue'
import VoiceInputButton from '../components/agent/VoiceInputButton.vue'
import { createFakeRuntime, createFakeVoice } from './fakeRuntime'

export interface ControllableClock {
  clock: ClientRuntime['clock']
  /** Advance the monotonic now() used by the controller. */
  advance(ms: number): void
  /** Invoke every currently-registered timeout callback (e.g. the 240s auto-stop). */
  fireTimeouts(): void
  pendingTimeoutCount(): number
}

export function createControllableClock(startMs = Date.now()): ControllableClock {
  let now = startMs
  let nextId = 0
  const timeouts = new Map<unknown, () => void>()
  const clock: ClientRuntime['clock'] = {
    now: () => now,
    setTimeout: (callback: () => void) => {
      const id = ++nextId
      timeouts.set(id, callback)
      return id
    },
    clearTimeout: (handle: unknown) => {
      timeouts.delete(handle)
    },
    setInterval: () => ++nextId,
    clearInterval: () => undefined,
  }
  return {
    clock,
    advance: (ms) => {
      now += ms
    },
    fireTimeouts: () => {
      for (const callback of [...timeouts.values()]) callback()
    },
    pendingTimeoutCount: () => timeouts.size,
  }
}

export interface VoiceFlowOverrides {
  /** `voice: undefined` explicitly removes the capability (render-condition tests). */
  runtime?: Omit<Partial<ClientRuntime>, 'voice'> & { voice?: ClientVoiceRuntime | undefined }

  voice?: Partial<ReturnType<typeof createFakeVoice>>
  draftApi?: VoiceDraftApi
  submit?: SubmitMessage
  storage?: VoiceStorage
  logger?: VoiceLogger
  speechToTextEnabled?: boolean
  buttonProps?: Partial<{ bindingId: string; conversationId: string; speechToTextEnabled: boolean }>
}

export interface VoiceFlowHarness {
  wrapper: ReturnType<typeof mount>
  bridge: VoiceDraftBridge
  clock: ControllableClock
  runtime: ClientRuntime
  button(): ReturnType<ReturnType<typeof mount>['get']>
  firePointerWindow(type: string, clientY: number, pointerId?: number): void
}

const DEFAULT_BUTTON_PROPS = { bindingId: 'binding-1', conversationId: 'conversation-1', speechToTextEnabled: true }

export function mountVoiceFlow(overrides: VoiceFlowOverrides = {}): VoiceFlowHarness {
  const clock = createControllableClock()
  const runtime = createFakeRuntime({
    clock: clock.clock,
    voice: createFakeVoice(overrides.voice),
    ...overrides.runtime,
  })
  const buttonProps = { ...DEFAULT_BUTTON_PROPS, ...overrides.buttonProps }
  let bridge: VoiceDraftBridge | undefined

  const wrapper = mount(
    defineComponent({
      setup() {
        bridge = useVoiceDraft({
          bindingId: buttonProps.bindingId,
          conversationId: buttonProps.conversationId,
          submit: overrides.submit ?? (async () => undefined),
          speechToTextEnabled: () => buttonProps.speechToTextEnabled,
          ...(overrides.draftApi !== undefined ? { draftApi: overrides.draftApi } : {}),
          ...(overrides.storage !== undefined ? { storage: overrides.storage } : {}),
          ...(overrides.logger !== undefined ? { logger: overrides.logger } : {}),
        })
        return () =>
          h('div', [
            h(VoiceInputButton, buttonProps),
            h(TranscriptDraftSheet),
            h(BottomToast),
          ])
      },
    }),
    { global: { plugins: [createClientUi(runtime)] }, attachTo: document.body },
  )

  return {
    wrapper,
    get bridge() {
      if (bridge === undefined) throw new Error('voice bridge was not created during setup')
      return bridge
    },
    clock,
    runtime,
    button: () => wrapper.get('.voice-input-button'),
    firePointerWindow(type, clientY, pointerId = 1) {
      const event = new Event(type, { bubbles: true, cancelable: true })
      Object.assign(event, { clientY, pointerId })
      window.dispatchEvent(event)
    },
  }
}

/** Flush microtasks + timers so async controller transitions settle. */
export function flushAsync(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}
