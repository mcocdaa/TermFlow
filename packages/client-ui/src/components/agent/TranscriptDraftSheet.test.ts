import { VOICE_DRAFT_STORAGE_KEY } from '@termflow/client-core'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TranscriptDraftSheet from './TranscriptDraftSheet.vue'
import { createFakeUploadResponse } from '../../test/fakeRuntime'
import { flushAsync, mountVoiceFlow, type VoiceFlowHarness } from '../../test/voiceTestHarness'

/** Drive the fake press/hold flow into the draft state. */
async function reachDraft(h: VoiceFlowHarness) {
  await h.button().trigger('pointerdown', { clientY: 300, pointerId: 1 })
  await flushAsync()
  h.clock.advance(1000)
  h.firePointerWindow('pointerup', 300)
  await flushAsync()
}

function sheetEvents(h: VoiceFlowHarness) {
  return h.wrapper.findComponent(TranscriptDraftSheet).emitted()
}

beforeEach(() => {
  sessionStorage.clear()
})

describe('TranscriptDraftSheet', () => {
  it('submits the edited text: confirm 204 → submit 202 with the edited transcript, then closes with 已发送', async () => {
    const confirm = vi.fn(async () => undefined)
    const cancel = vi.fn(async () => undefined)
    const submit = vi.fn(async () => undefined)
    const h = mountVoiceFlow({ draftApi: { confirm, cancel }, submit })
    await reachDraft(h)

    const sheet = h.wrapper.get('[role="dialog"]')
    expect(sheet.attributes('aria-modal')).toBe('true')
    const textarea = h.wrapper.get('[data-voice-draft-text]')
    expect((textarea.element as HTMLTextAreaElement).value).toBe('测试转写文本')
    expect((textarea.element as HTMLTextAreaElement).maxLength).toBe(64 * 1024)

    await textarea.setValue('编辑后的文本')
    await h.wrapper.get('[data-action="confirm-draft"]').trigger('click')
    await flushAsync()

    expect(confirm).toHaveBeenCalledWith('draft-fake-1')
    expect(cancel).not.toHaveBeenCalled()
    expect(submit).toHaveBeenCalledWith({
      conversationId: 'conversation-1',
      text: '编辑后的文本',
      draftRef: 'draft-fake-1',
    })
    expect(h.wrapper.find('[role="dialog"]').exists()).toBe(false)
    const toast = h.wrapper.get('[data-bottom-toast]')
    expect(toast.text()).toBe('已发送')
    expect(toast.attributes('role')).toBe('status')
    expect(sheetEvents(h).closed?.length).toBe(1)
    expect(sheetEvents(h).submitted?.at(-1)).toEqual(['draft-fake-1'])
    expect(sessionStorage.getItem(VOICE_DRAFT_STORAGE_KEY)).toBeNull()
    h.wrapper.unmount()
  })

  it('discard calls cancel only (never confirm/submit) and closes the sheet silently', async () => {
    const confirm = vi.fn(async () => undefined)
    const cancel = vi.fn(async () => undefined)
    const submit = vi.fn(async () => undefined)
    const h = mountVoiceFlow({ draftApi: { confirm, cancel }, submit })
    await reachDraft(h)

    await h.wrapper.get('[data-action="discard-draft"]').trigger('click')
    await flushAsync()

    expect(cancel).toHaveBeenCalledWith('draft-fake-1')
    expect(confirm).not.toHaveBeenCalled()
    expect(submit).not.toHaveBeenCalled()
    expect(h.wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(sheetEvents(h).closed?.length).toBe(1)
    expect(sessionStorage.getItem(VOICE_DRAFT_STORAGE_KEY)).toBeNull()
    h.wrapper.unmount()
  })

  it('Escape opens the 放弃本次转写？ confirmation; both branches behave', async () => {
    const confirm = vi.fn(async () => undefined)
    const cancel = vi.fn(async () => undefined)
    const submit = vi.fn(async () => undefined)
    const h = mountVoiceFlow({ draftApi: { confirm, cancel }, submit })
    await reachDraft(h)

    const panel = h.wrapper.get('[role="dialog"]')
    await panel.trigger('keydown', { key: 'Escape' })
    const exitDialog = h.wrapper.get('[role="alertdialog"]')
    expect(exitDialog.find('h2').text()).toBe('放弃本次转写？')
    expect(document.activeElement?.textContent).toContain('取消')

    // Escape inside the exit dialog closes just the dialog — sheet stays open.
    await exitDialog.trigger('keydown', { key: 'Escape' })
    expect(h.wrapper.find('[role="alertdialog"]').exists()).toBe(false)
    expect(h.wrapper.find('[role="dialog"]').exists()).toBe(true)

    // Branch 1: 取消 keeps the sheet and returns focus to the textarea.
    await panel.trigger('keydown', { key: 'Escape' })
    await h.wrapper.get('[data-action="stay-in-sheet"]').trigger('click')
    await flushAsync()
    expect(h.wrapper.find('[role="alertdialog"]').exists()).toBe(false)
    expect(h.wrapper.find('[role="dialog"]').exists()).toBe(true)
    expect(document.activeElement?.id).toBe('voice-draft-text')
    expect(cancel).not.toHaveBeenCalled()

    // Branch 2: 放弃 discards via the controller — cancel called, never submit.
    await panel.trigger('keydown', { key: 'Escape' })
    await h.wrapper.get('[data-action="confirm-discard"]').trigger('click')
    await flushAsync()
    expect(cancel).toHaveBeenCalledWith('draft-fake-1')
    expect(confirm).not.toHaveBeenCalled()
    expect(submit).not.toHaveBeenCalled()
    expect(h.wrapper.find('[role="dialog"]').exists()).toBe(false)
    h.wrapper.unmount()
  })

  it('renders the provider disclosure with region/language/duration, and the provider-only form when they are empty', async () => {
    const full = mountVoiceFlow()
    await reachDraft(full)
    const fullDisclosure = full.wrapper.get('[data-voice-disclosure]').text()
    expect(fullDisclosure).toContain('由本部署语音转写服务转写 · 提供方 fake-speeches')
    expect(fullDisclosure).toContain('区域 cn-beijing')
    expect(fullDisclosure).toContain('语言 zh')
    expect(fullDisclosure).toContain('时长 3 秒')
    full.wrapper.unmount()
    // The first mount persisted its draft to sessionStorage; clear it so the
    // second mount does not restore the old draft instead of uploading anew.
    sessionStorage.clear()

    const sparse = mountVoiceFlow({
      voice: {
        uploadAudio: async () =>
          createFakeUploadResponse({ region: '', language: null, duration_seconds: null }),
      },
    })
    await reachDraft(sparse)
    const sparseDisclosure = sparse.wrapper.get('[data-voice-disclosure]').text()
    expect(sparseDisclosure).toContain('由本部署语音转写服务转写 · 提供方 fake-speeches')
    expect(sparseDisclosure).not.toContain('区域')
    expect(sparseDisclosure).not.toContain('语言')
    expect(sparseDisclosure).not.toContain('时长')
    sparse.wrapper.unmount()
  })

  it('shows the 410 转写已过期 toast, closes, and never calls submit', async () => {
    const confirm = vi.fn(async () => {
      throw { status: 410 }
    })
    const cancel = vi.fn(async () => undefined)
    const submit = vi.fn(async () => undefined)
    const h = mountVoiceFlow({ draftApi: { confirm, cancel }, submit })
    await reachDraft(h)

    await h.wrapper.get('[data-action="confirm-draft"]').trigger('click')
    await flushAsync()

    const toast = h.wrapper.get('[data-bottom-toast]')
    expect(toast.text()).toBe('转写已过期，请重新录音')
    expect(toast.attributes('role')).toBe('alert')
    expect(submit).not.toHaveBeenCalled()
    expect(h.wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(sessionStorage.getItem(VOICE_DRAFT_STORAGE_KEY)).toBeNull()
    h.wrapper.unmount()
  })

  it('restores a pending draft from sessionStorage on mount and discards it through cancel', async () => {
    sessionStorage.setItem(
      VOICE_DRAFT_STORAGE_KEY,
      JSON.stringify({
        draftId: 'draft-restored',
        text: '恢复的转写文本',
        bindingId: 'binding-1',
        conversationId: 'conversation-1',
        provider: 'speaches',
        region: 'cn-beijing',
        expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
      }),
    )
    const cancel = vi.fn(async () => undefined)
    const submit = vi.fn(async () => undefined)
    const h = mountVoiceFlow({ draftApi: { confirm: vi.fn(async () => undefined), cancel }, submit })

    // The sheet opens on mount with the restored transcript.
    const textarea = h.wrapper.get('[data-voice-draft-text]')
    expect((textarea.element as HTMLTextAreaElement).value).toBe('恢复的转写文本')
    expect(h.wrapper.get('[data-voice-disclosure]').text()).toContain('提供方 speaches')

    await h.wrapper.get('[data-action="discard-draft"]').trigger('click')
    await flushAsync()
    expect(cancel).toHaveBeenCalledWith('draft-restored')
    expect(submit).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(VOICE_DRAFT_STORAGE_KEY)).toBeNull()
    h.wrapper.unmount()
  })

  it('drops an expired stored draft with the 转写已过期 toast instead of reopening', async () => {
    sessionStorage.setItem(
      VOICE_DRAFT_STORAGE_KEY,
      JSON.stringify({
        draftId: 'draft-expired',
        text: '过期文本',
        bindingId: 'binding-1',
        conversationId: 'conversation-1',
        provider: 'speaches',
        region: '',
        expiresAt: '2020-01-01T00:00:00.000Z',
      }),
    )
    const h = mountVoiceFlow()
    expect(h.wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(h.wrapper.get('[data-bottom-toast]').text()).toBe('转写已过期，请重新录音')
    expect(sessionStorage.getItem(VOICE_DRAFT_STORAGE_KEY)).toBeNull()
    h.wrapper.unmount()
  })

  it('traps focus inside the dialog: initial textarea focus and Tab wrapping', async () => {
    const h = mountVoiceFlow()
    await reachDraft(h)

    const textarea = h.wrapper.get('[data-voice-draft-text]').element as HTMLTextAreaElement
    const confirmButton = h.wrapper.get('[data-action="confirm-draft"]').element as HTMLButtonElement
    expect(document.activeElement).toBe(textarea)

    confirmButton.focus()
    await h.wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Tab' })
    expect(document.activeElement).toBe(textarea)

    await h.wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(confirmButton)
    h.wrapper.unmount()
  })

  it('flags text exceeding MAX_AGENT_TEXT_BYTES with a hint and blocks confirm', async () => {
    const confirm = vi.fn(async () => undefined)
    const h = mountVoiceFlow({ draftApi: { confirm, cancel: vi.fn(async () => undefined) } })
    await reachDraft(h)

    await h.wrapper.get('[data-voice-draft-text]').setValue('中'.repeat(22_000)) // 66 000 UTF-8 bytes > 64 KiB
    const hint = h.wrapper.get('[role="alert"]')
    expect(hint.text()).toContain('文本超出长度限制')
    const confirmButton = h.wrapper.get('[data-action="confirm-draft"]').element as HTMLButtonElement
    expect(confirmButton.disabled).toBe(true)
    confirmButton.click() // native click on a disabled button must not dispatch
    expect(confirm).not.toHaveBeenCalled()
    h.wrapper.unmount()
  })
})
