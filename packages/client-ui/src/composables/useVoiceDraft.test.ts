import {
  PermissionDeniedError,
  VOICE_DRAFT_STORAGE_KEY,
  VOICE_MESSAGES,
  type SubmitMessage,
  type VoiceDraftApi,
  type VoiceStorage,
} from '@termflow/client-core'
import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { createClientUi, type ClientRuntime } from '../runtime'
import { useVoiceDraft, type VoiceDraftBridge } from './useVoiceDraft'
import { useBottomToast, type BottomToastController } from './useBottomToast'
import { createFakeRuntime, createFakeVoice, createFakeVoiceStorage } from '../test/fakeRuntime'
import { createControllableClock, flushAsync } from '../test/voiceTestHarness'

interface BridgeOverrides {
  runtime?: Omit<Partial<ClientRuntime>, 'voice'> & { voice?: ClientRuntime['voice'] | undefined }
  voice?: Partial<ReturnType<typeof createFakeVoice>>
  submit?: SubmitMessage
  draftApi?: VoiceDraftApi
  storage?: VoiceStorage
  speechToTextEnabled?: boolean
}

function mountBridge(overrides: BridgeOverrides = {}) {
  const clock = createControllableClock()
  const runtime = createFakeRuntime({
    clock: clock.clock,
    voice: createFakeVoice(overrides.voice),
    ...overrides.runtime,
  })
  let toast: BottomToastController | undefined
  let bridge: VoiceDraftBridge | undefined
  const wrapper = mount(
    defineComponent({
      setup() {
        toast = useBottomToast()
        bridge = useVoiceDraft({
          bindingId: 'binding-1',
          conversationId: 'conversation-1',
          submit: overrides.submit ?? (async () => undefined),
          speechToTextEnabled: () => overrides.speechToTextEnabled ?? true,
          ...(overrides.draftApi !== undefined ? { draftApi: overrides.draftApi } : {}),
          ...(overrides.storage !== undefined ? { storage: overrides.storage } : {}),
        })
        return () => h('div')
      },
    }),
    { global: { plugins: [createClientUi(runtime)] }, attachTo: document.body },
  )
  return {
    clock,
    wrapper,
    get toast() {
      if (toast === undefined) throw new Error('toast controller was not captured')
      return toast
    },
    get bridge() {
      if (bridge === undefined) throw new Error('voice bridge was not created')
      return bridge
    },
  }
}

/** Press → recording → release → draft through the fake recorder/uploader. */
async function reachDraft(h: ReturnType<typeof mountBridge>) {
  h.bridge.press()
  await flushAsync()
  h.clock.advance(1000)
  h.bridge.release()
  await flushAsync()
}

describe('useVoiceDraft', () => {
  it('forwards controller toasts to BottomToast: 已发送 as success tone', async () => {
    const h = mountBridge()
    await reachDraft(h)
    expect(h.bridge.state.value.status).toBe('draft')
    h.bridge.confirm('确认文本')
    await flushAsync()
    expect(h.toast.current.value).toEqual({ text: VOICE_MESSAGES.submitted, tone: 'success' })
    expect(h.bridge.state.value.status).toBe('idle')
    h.wrapper.unmount()
  })

  it('maps permission denial to an error toast (role=alert semantics)', async () => {
    const h = mountBridge({
      voice: {
        createRecorder: () => ({
          start: async () => {
            throw new PermissionDeniedError()
          },
          stop: async () => ({ blob: new Blob(['a']), mimeType: 'audio/webm' as const, durationSeconds: 1 }),
          abort: () => undefined,
        }),
      },
    })
    h.bridge.press()
    await flushAsync()
    expect(h.toast.current.value).toEqual({ text: VOICE_MESSAGES.permissionDenied, tone: 'error' })
    expect(h.bridge.state.value.status).toBe('denied')
    h.wrapper.unmount()
  })

  it('fires onDraftSubmitted with the draft id on submit success', async () => {
    const submitted: string[] = []
    const h = mountBridge()
    h.bridge.onDraftSubmitted((draftId) => submitted.push(draftId))
    await reachDraft(h)
    h.bridge.confirm('文本')
    await flushAsync()
    expect(submitted).toEqual(['draft-fake-1'])
    h.wrapper.unmount()
  })

  it('never fires onDraftSubmitted when the draft is discarded', async () => {
    const submitted: string[] = []
    const confirm = vi.fn(async () => undefined)
    const cancel = vi.fn(async () => undefined)
    const submit = vi.fn(async () => undefined)
    const h = mountBridge({ draftApi: { confirm, cancel }, submit })
    h.bridge.onDraftSubmitted((draftId) => submitted.push(draftId))
    await reachDraft(h)
    h.bridge.discard()
    await flushAsync()
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(confirm).not.toHaveBeenCalled()
    expect(submit).not.toHaveBeenCalled()
    expect(submitted).toEqual([])
    expect(h.bridge.state.value.status).toBe('idle')
    h.wrapper.unmount()
  })

  it('cancels an active recording when the app goes to background', async () => {
    let hidden = false
    let listener: (() => void) | undefined
    const abort = vi.fn()
    const h = mountBridge({
      runtime: {
        visibility: {
          isHidden: () => hidden,
          subscribe: (l) => {
            listener = l
            return () => {
              listener = undefined
            }
          },
        },
      },
      voice: {
        createRecorder: () => ({
          start: async () => undefined,
          stop: async () => ({ blob: new Blob(['a']), mimeType: 'audio/webm' as const, durationSeconds: 1 }),
          abort,
        }),
      },
    })
    h.bridge.press()
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('recording')
    hidden = true
    listener?.()
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('cancelled')
    expect(abort).toHaveBeenCalledTimes(1)
    h.wrapper.unmount()
  })

  it('restores a stored draft on init', async () => {
    const storage = createFakeVoiceStorage()
    storage.setItem(
      VOICE_DRAFT_STORAGE_KEY,
      JSON.stringify({
        draftId: 'draft-restored',
        text: '恢复文本',
        bindingId: 'binding-1',
        conversationId: 'conversation-1',
        provider: 'speaches',
        region: '',
        expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
      }),
    )
    const h = mountBridge({ storage })
    expect(h.bridge.state.value).toMatchObject({ status: 'draft' })
    if (h.bridge.state.value.status === 'draft') {
      expect(h.bridge.state.value.draft.text).toBe('恢复文本')
    }
    h.wrapper.unmount()
  })

  it('reports unavailable and stays idle when the runtime has no voice capability', async () => {
    const h = mountBridge({ runtime: { voice: undefined } })
    expect(h.bridge.available.value).toBe(false)
    h.bridge.press()
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('idle')
    h.wrapper.unmount()
  })
})
