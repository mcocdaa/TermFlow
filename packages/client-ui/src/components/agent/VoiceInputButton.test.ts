import { PermissionDeniedError, type TranscriptionUploadResponse } from '@termflow/client-core'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import VoiceInputButton from './VoiceInputButton.vue'
import { createFakeUploadResponse } from '../../test/fakeRuntime'
import { flushAsync, mountVoiceFlow, type VoiceFlowHarness } from '../../test/voiceTestHarness'

/** Press (pointerdown) → recording → release (window pointerup). */
async function holdThenRelease(h: VoiceFlowHarness, holdMs = 1000) {
  await h.button().trigger('pointerdown', { clientY: 300, pointerId: 1 })
  await flushAsync()
  h.clock.advance(holdMs)
  h.firePointerWindow('pointerup', 300)
  await flushAsync()
}

function announcement(h: VoiceFlowHarness): string {
  return h.wrapper.get('[data-voice-announcement]').text()
}

beforeEach(() => {
  sessionStorage.clear()
})

describe('VoiceInputButton', () => {
  it('renders only when all three conditions hold: voice capability ∧ device gate ∧ STT flag', () => {
    const noVoice = mountVoiceFlow({ runtime: { voice: undefined } })
    expect(noVoice.wrapper.find('.voice-input-button').exists()).toBe(false)
    noVoice.wrapper.unmount()

    const deviceDisabled = mountVoiceFlow({ voice: { enabled: () => false } })
    expect(deviceDisabled.wrapper.find('.voice-input-button').exists()).toBe(false)
    deviceDisabled.wrapper.unmount()

    const sttDisabled = mountVoiceFlow({ buttonProps: { speechToTextEnabled: false } })
    expect(sttDisabled.wrapper.find('.voice-input-button').exists()).toBe(false)
    sttDisabled.wrapper.unmount()

    const enabled = mountVoiceFlow()
    const button = enabled.button()
    expect(button.attributes('aria-label')).toBe('按住说话')
    expect(button.attributes('aria-pressed')).toBe('false')
    const hintId = button.attributes('aria-describedby')
    expect(hintId).toBeTruthy()
    expect(enabled.wrapper.get(`#${hintId}`).text()).toBe('按住说话，松开发送，上滑取消')
    enabled.wrapper.unmount()
  })

  it('discards a too-short press with the 录音太短 toast and never uploads', async () => {
    const uploadAudio = vi.fn(async () => createFakeUploadResponse())
    const h = mountVoiceFlow({ voice: { uploadAudio } })
    await h.button().trigger('pointerdown', { clientY: 300, pointerId: 1 })
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('recording')
    // Release without any elapsed time → < 0.5s.
    h.firePointerWindow('pointerup', 300)
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('tooShort')
    const toast = h.wrapper.get('[data-bottom-toast]')
    expect(toast.text()).toBe('录音太短')
    expect(toast.attributes('role')).toBe('alert')
    expect(uploadAudio).not.toHaveBeenCalled()
    h.wrapper.unmount()
  })

  it('slides up into the cancel zone at 96px with 48px hysteresis and cancels without upload', async () => {
    const abort = vi.fn()
    const start = vi.fn(async () => undefined)
    const uploadAudio = vi.fn(async () => createFakeUploadResponse())
    const h = mountVoiceFlow({
      voice: {
        uploadAudio,
        createRecorder: () => ({
          start,
          stop: async () => ({ blob: new Blob(['a']), mimeType: 'audio/webm' as const, durationSeconds: 1 }),
          abort,
        }),
      },
    })
    await h.button().trigger('pointerdown', { clientY: 300, pointerId: 1 })
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('recording')

    // 97px up → enters the cancel zone.
    h.firePointerWindow('pointermove', 203)
    await flushAsync()
    expect(h.bridge.state.value).toMatchObject({ status: 'recording', cancelPending: true })
    expect(h.button().classes()).toContain('cancel-pending')
    expect(h.wrapper.get('.voice-hint').text()).toBe('松手取消')

    // 50px up → inside the [48, 96] hysteresis band: stays in the zone.
    h.firePointerWindow('pointermove', 250)
    await flushAsync()
    expect(h.bridge.state.value).toMatchObject({ status: 'recording', cancelPending: true })

    // 40px up → below the 48px resume threshold: leaves the zone.
    h.firePointerWindow('pointermove', 260)
    await flushAsync()
    expect(h.bridge.state.value).toMatchObject({ status: 'recording', cancelPending: false })
    expect(h.wrapper.get('.voice-hint').text()).toBe('上滑取消')

    // Back into the zone, then release → cancelled, recorder aborted, no upload.
    h.firePointerWindow('pointermove', 200)
    await flushAsync()
    h.firePointerWindow('pointerup', 200)
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('cancelled')
    expect(abort).toHaveBeenCalledTimes(1)
    expect(uploadAudio).not.toHaveBeenCalled()
    expect(h.wrapper.find('[data-bottom-toast]').exists()).toBe(false)
    h.wrapper.unmount()
  })

  it('supports Space as a keyboard equivalent with preventDefault and no double activation', async () => {
    const uploadAudio = vi.fn(async () => createFakeUploadResponse())
    const h = mountVoiceFlow({ voice: { uploadAudio } })
    const buttonEl = h.button().element as HTMLButtonElement

    const spaceDown = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })
    buttonEl.dispatchEvent(spaceDown)
    expect(spaceDown.defaultPrevented).toBe(true)
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('recording')
    expect(h.button().attributes('aria-pressed')).toBe('true')

    h.clock.advance(1000)
    const spaceUp = new KeyboardEvent('keyup', { key: ' ', bubbles: true, cancelable: true })
    buttonEl.dispatchEvent(spaceUp)
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('draft')
    expect(uploadAudio).toHaveBeenCalledTimes(1)
    h.wrapper.unmount()
  })

  it('supports Enter as a keyboard equivalent: keydown starts, keyup releases (independent mount)', async () => {
    const uploadAudio = vi.fn(async () => createFakeUploadResponse())
    const h = mountVoiceFlow({ voice: { uploadAudio } })
    const buttonEl = h.button().element as HTMLButtonElement

    const enterDown = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    buttonEl.dispatchEvent(enterDown)
    expect(enterDown.defaultPrevented).toBe(true)
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('recording')
    expect(h.button().attributes('aria-pressed')).toBe('true')

    h.clock.advance(1000)
    const enterUp = new KeyboardEvent('keyup', { key: 'Enter', bubbles: true, cancelable: true })
    buttonEl.dispatchEvent(enterUp)
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('draft')
    // Exactly one upload: preventDefault on keydown stops the native
    // keyup→click activation from double-triggering the gesture.
    expect(uploadAudio).toHaveBeenCalledTimes(1)
    h.wrapper.unmount()
  })

  it('ignores repeated keydowns and unrelated keys', async () => {
    const start = vi.fn(async () => undefined)
    const h = mountVoiceFlow({ voice: { createRecorder: () => ({ start, stop: async () => ({ blob: new Blob(['a']), mimeType: 'audio/webm' as const, durationSeconds: 1 }), abort: () => undefined }) } })
    const buttonEl = h.button().element as HTMLButtonElement
    buttonEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true }))
    expect(start).not.toHaveBeenCalled()
    buttonEl.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }))
    buttonEl.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', repeat: true, bubbles: true, cancelable: true }))
    buttonEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', repeat: true, bubbles: true, cancelable: true }))
    await flushAsync()
    expect(start).toHaveBeenCalledTimes(1)
    h.wrapper.unmount()
  })

  it('announces 录音中 / 正在转写 / 转写完成，请确认 through the polite live region and shows the red dot', async () => {
    let resolveUpload!: (value: TranscriptionUploadResponse) => void
    const uploadAudio = vi.fn(
      () =>
        new Promise<TranscriptionUploadResponse>((resolve) => {
          resolveUpload = resolve
        }),
    )
    const h = mountVoiceFlow({ voice: { uploadAudio } })

    expect(announcement(h)).toBe('')

    await h.button().trigger('pointerdown', { clientY: 300, pointerId: 1 })
    await flushAsync()
    expect(h.button().attributes('aria-pressed')).toBe('true')
    expect(h.button().find('.voice-recording-dot').exists()).toBe(true)
    expect(h.wrapper.get('.voice-hint').text()).toBe('上滑取消')
    expect(announcement(h)).toBe('录音中')

    h.clock.advance(1000)
    h.firePointerWindow('pointerup', 300)
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('uploading')
    expect(announcement(h)).toBe('正在转写')
    // §4.6: the uploading state shows a spinner, 正在转写…, and elapsed seconds.
    expect(h.wrapper.find('.voice-spinner').exists()).toBe(true)
    expect(h.wrapper.find('[data-voice-upload-seconds]').text()).toBe('0s')

    resolveUpload(createFakeUploadResponse())
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('draft')
    expect(announcement(h)).toBe('转写完成，请确认')
    h.wrapper.unmount()
  })

  it('shows the permission guidance toast on PermissionDeniedError and stays pressable', async () => {
    const start = vi.fn(async () => {
      throw new PermissionDeniedError()
    })
    const uploadAudio = vi.fn(async () => createFakeUploadResponse())
    const h = mountVoiceFlow({ voice: { uploadAudio, createRecorder: () => ({ start, stop: async () => ({ blob: new Blob(['a']), mimeType: 'audio/webm' as const, durationSeconds: 1 }), abort: () => undefined }) } })

    await h.button().trigger('pointerdown', { clientY: 300, pointerId: 1 })
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('denied')
    const toast = h.wrapper.get('[data-bottom-toast]')
    expect(toast.text()).toBe('无法访问麦克风，请在系统设置中允许 TermFlow 使用麦克风后重试')
    expect(toast.attributes('role')).toBe('alert')
    expect(uploadAudio).not.toHaveBeenCalled()
    // The button remains rendered and pressable (denied → re-press allowed).
    expect(h.wrapper.find('.voice-input-button').exists()).toBe(true)
    expect(h.bridge.state.value.status).not.toBe('disabled')
    h.wrapper.unmount()
  })

  it('auto-stops at 240s and uploads (size ceiling protection)', async () => {
    const stop = vi.fn(async () => ({ blob: new Blob(['a']), mimeType: 'audio/webm' as const, durationSeconds: 240 }))
    const uploadAudio = vi.fn(async () => createFakeUploadResponse())
    const h = mountVoiceFlow({ voice: { uploadAudio, createRecorder: () => ({ start: async () => undefined, stop, abort: () => undefined }) } })

    await h.button().trigger('pointerdown', { clientY: 300, pointerId: 1 })
    await flushAsync()
    expect(h.bridge.state.value.status).toBe('recording')
    h.clock.advance(240_000)
    h.clock.fireTimeouts()
    await flushAsync()
    expect(stop).toHaveBeenCalledTimes(1)
    expect(uploadAudio).toHaveBeenCalledTimes(1)
    expect(h.bridge.state.value.status).toBe('draft')
    h.wrapper.unmount()
  })

  it('emits draft-submitted with the draft id after a successful submit sequence', async () => {
    const submit = vi.fn(async () => undefined)
    const h = mountVoiceFlow({ submit })
    await holdThenRelease(h)
    expect(h.bridge.state.value.status).toBe('draft')
    await h.wrapper.get('[data-action="confirm-draft"]').trigger('click')
    await flushAsync()
    const button = h.wrapper.findComponent(VoiceInputButton)
    expect(button.emitted('draft-submitted')?.at(-1)).toEqual(['draft-fake-1'])
    expect(submit).toHaveBeenCalledWith({
      conversationId: 'conversation-1',
      text: '测试转写文本',
      draftRef: 'draft-fake-1',
    })
    h.wrapper.unmount()
  })
})
