import { flushPromises, mount } from '@vue/test-utils'
import { ApiError, type AgentSubmitMessageResponse } from '@termflow/client-core'
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import AgentComposer from './AgentComposer.vue'
import { createClientUi, type ClientRuntime } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'

const CONVERSATION = 'conv-1'
const MAX_AGENT_TEXT_BYTES = 64 * 1024

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

function okResponse(): AgentSubmitMessageResponse {
  return {
    message_id: 'm-1',
    conversation_id: CONVERSATION,
    admission_seq: 1,
    idempotency_key: 'k-1',
    delivery_state: 'accepted',
    submission_state: 'accepted',
  }
}

interface ComposerOverrides {
  props?: Partial<{ conversationId: string; disabled: boolean; backendState: string | null; hasActiveRun: boolean }>
  submitMessage?: ReturnType<typeof vi.fn>
  cancelRun?: ReturnType<typeof vi.fn>
}

function mountComposer(overrides: ComposerOverrides = {}) {
  const submitMessage = overrides.submitMessage ?? vi.fn(async () => okResponse())
  const cancelRun = overrides.cancelRun ?? vi.fn(async () => ({ outcome: 'confirmed', run_state: 'finished' }))
  const runtime = createFakeRuntime({
    api: {
      ...createFakeRuntime().api,
      agents: {
        ...createFakeRuntime().api.agents,
        submitMessage,
        cancelRun,
      },
    } as unknown as ClientRuntime['api'],
  })
  const ui = createClientUi(runtime)
  const wrapper = mount(AgentComposer, {
    props: { conversationId: CONVERSATION, ...overrides.props },
    global: { plugins: [ui] },
    attachTo: document.body,
  })
  return { wrapper, ui, runtime, submitMessage, cancelRun }
}

const composerRoot = (wrapper: ReturnType<typeof mount>) => wrapper.get('[data-agent-composer]')
const input = (wrapper: ReturnType<typeof mount>) => wrapper.get('[data-agent-composer-input]')
const inputValue = (wrapper: ReturnType<typeof mount>) => (input(wrapper).element as HTMLTextAreaElement).value
const send = (wrapper: ReturnType<typeof mount>) => wrapper.get('[data-action="send-message"]')
const cancel = (wrapper: ReturnType<typeof mount>) => wrapper.get('[data-action="cancel-run"]')

describe('AgentComposer', () => {
  it('rejects empty input: the send button is disabled and nothing is submitted', () => {
    const { wrapper, submitMessage } = mountComposer()
    expect(send(wrapper).attributes('disabled')).toBeDefined()
    expect(submitMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('rejects over-long input with a byte-based hint and keeps the text', async () => {
    const { wrapper, submitMessage } = mountComposer()
    const long = '中'.repeat(22_000) // 66 000 UTF-8 bytes > 64 KiB
    await input(wrapper).setValue(long)
    expect(send(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-agent-composer-hint]').text()).toContain('文本超出长度限制')
    expect(wrapper.get('[data-agent-composer-hint]').text()).toContain(String(MAX_AGENT_TEXT_BYTES))
    expect(submitMessage).not.toHaveBeenCalled()
    expect(inputValue(wrapper)).toBe(long)
    wrapper.unmount()
  })

  it('accepts control characters by stripping them (except newlines) before submit', async () => {
    const { wrapper, submitMessage } = mountComposer()
    await input(wrapper).setValue('你好\u0000世界\tline\n换行')
    await send(wrapper).trigger('click')
    await flushPromises()
    expect(submitMessage).toHaveBeenCalledTimes(1)
    expect(submitMessage).toHaveBeenCalledWith(CONVERSATION, { text: '你好世界line\n换行' })
    wrapper.unmount()
  })

  it('marks the composer aria-busy while submitting and blocks double submits', async () => {
    const gate = deferred<AgentSubmitMessageResponse>()
    const submitMessage = vi.fn(() => gate.promise)
    const { wrapper } = mountComposer({ submitMessage })
    await input(wrapper).setValue('你好')
    await send(wrapper).trigger('click')
    await nextTick()
    expect(composerRoot(wrapper).attributes('aria-busy')).toBe('true')
    expect(send(wrapper).attributes('disabled')).toBeDefined()
    expect(send(wrapper).text()).toBe('发送中…')

    // A second activation while in flight must not reach the API.
    await send(wrapper).trigger('click')
    expect(submitMessage).toHaveBeenCalledTimes(1)

    gate.resolve(okResponse())
    await flushPromises()
    expect(composerRoot(wrapper).attributes('aria-busy')).toBeUndefined()
    wrapper.unmount()
  })

  it('echoes the accepted text, clears the input and keeps focus after 202', async () => {
    const { wrapper, submitMessage } = mountComposer()
    const textarea = input(wrapper).element as HTMLTextAreaElement
    textarea.focus()
    await input(wrapper).setValue('第一条消息')
    // Focus moves away (e.g. the user clicked send with the mouse) — the
    // composer must restore it after the 202.
    ;(send(wrapper).element as HTMLElement).focus()
    expect(document.activeElement).not.toBe(textarea)

    await send(wrapper).trigger('click')
    await flushPromises()
    expect(submitMessage).toHaveBeenCalledWith(CONVERSATION, { text: '第一条消息' })
    expect(wrapper.emitted('submitted')).toEqual([['第一条消息']])
    expect(inputValue(wrapper)).toBe('')
    expect(document.activeElement).toBe(textarea)
    wrapper.unmount()
  })

  it('shows the cancel button only while a run is active', () => {
    const idle = mountComposer()
    expect(idle.wrapper.find('[data-action="cancel-run"]').exists()).toBe(false)
    idle.wrapper.unmount()

    const active = mountComposer({ props: { hasActiveRun: true } })
    expect(active.wrapper.find('[data-action="cancel-run"]').exists()).toBe(true)
    active.wrapper.unmount()
  })

  it('posts /cancel and emits cancelled on 202 without a toast', async () => {
    const cancelRun = vi.fn(async () => ({ outcome: 'confirmed', run_state: 'finished' }))
    const { wrapper, ui } = mountComposer({ props: { hasActiveRun: true }, cancelRun })
    await cancel(wrapper).trigger('click')
    await flushPromises()
    expect(cancelRun).toHaveBeenCalledWith(CONVERSATION)
    expect(wrapper.emitted('cancelled')).toHaveLength(1)
    expect(ui.toast.current.value).toBeNull()
    wrapper.unmount()
  })

  it('treats a 409 no_active_run silently and still emits cancelled for the stale UI', async () => {
    const cancelRun = vi.fn(async () => {
      throw new ApiError('validation', { status: 409, code: 'no_active_run' })
    })
    const { wrapper, ui } = mountComposer({ props: { hasActiveRun: true }, cancelRun })
    await cancel(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.emitted('cancelled')).toHaveLength(1)
    expect(ui.toast.current.value).toBeNull()
    wrapper.unmount()
  })

  it('toasts other cancel failures without emitting cancelled', async () => {
    const cancelRun = vi.fn(async () => {
      throw new ApiError('server', { status: 503 })
    })
    const { wrapper, ui } = mountComposer({ props: { hasActiveRun: true }, cancelRun })
    await cancel(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.emitted('cancelled')).toBeUndefined()
    expect(ui.toast.current.value?.tone).toBe('error')
    expect(ui.toast.current.value?.text).toBe('服务暂时不可用，请稍后重试。')
    wrapper.unmount()
  })

  it('marks the cancel button busy while the request is in flight and prevents double cancels', async () => {
    const gate = deferred<{ outcome: string; run_state: string }>()
    const cancelRun = vi.fn(() => gate.promise)
    const { wrapper } = mountComposer({ props: { hasActiveRun: true }, cancelRun })
    await cancel(wrapper).trigger('click')
    await nextTick()
    expect(cancel(wrapper).attributes('aria-busy')).toBe('true')
    expect(cancel(wrapper).attributes('disabled')).toBeDefined()
    expect(cancel(wrapper).text()).toBe('正在取消…')
    await cancel(wrapper).trigger('click')
    expect(cancelRun).toHaveBeenCalledTimes(1)
    gate.resolve({ outcome: 'confirmed', run_state: 'finished' })
    await flushPromises()
    expect(cancel(wrapper).attributes('aria-busy')).toBeUndefined()
    wrapper.unmount()
  })

  it('greys out with a runtime hint after a 503 and recovers when the backend reports ready', async () => {
    const submitMessage = vi.fn(async () => {
      throw new ApiError('server', { status: 503 })
    })
    const { wrapper, ui } = mountComposer({ submitMessage })
    await input(wrapper).setValue('触发 503')
    await send(wrapper).trigger('click')
    await flushPromises()

    expect(ui.toast.current.value).toBeNull()
    expect(wrapper.get('[data-agent-composer-unavailable]').text()).toContain('后端运行时未就绪')
    expect(input(wrapper).attributes('disabled')).toBeDefined()
    expect(send(wrapper).attributes('disabled')).toBeDefined()
    expect(inputValue(wrapper)).toBe('触发 503')

    // A later STATE_DELTA reporting ready lifts the fail-closed latch.
    await wrapper.setProps({ backendState: 'ready' })
    expect(wrapper.find('[data-agent-composer-unavailable]').exists()).toBe(false)
    expect(input(wrapper).attributes('disabled')).toBeUndefined()
    expect(send(wrapper).attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('greys out on the binding_runtime_unavailable error code (fail-closed)', async () => {
    const submitMessage = vi.fn(async () => {
      throw new ApiError('server', { status: 503, code: 'binding_runtime_unavailable' })
    })
    const { wrapper } = mountComposer({ submitMessage })
    await input(wrapper).setValue('x')
    await send(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-agent-composer-unavailable]').exists()).toBe(true)
    expect(input(wrapper).attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('toasts non-503 submit failures and retains the text for retry', async () => {
    const submitMessage = vi.fn(async () => {
      throw new ApiError('validation', { status: 422 })
    })
    const { wrapper, ui } = mountComposer({ submitMessage })
    await input(wrapper).setValue('非法内容')
    await send(wrapper).trigger('click')
    await flushPromises()
    expect(ui.toast.current.value?.text).toBe('提交的内容不符合要求。')
    expect(inputValue(wrapper)).toBe('非法内容')
    expect(input(wrapper).attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-agent-composer-unavailable]').exists()).toBe(false)
    expect(wrapper.emitted('submitted')).toBeUndefined()
    wrapper.unmount()
  })

  it('keeps textarea focus across streaming prop updates', async () => {
    const { wrapper } = mountComposer()
    const textarea = input(wrapper).element as HTMLTextAreaElement
    textarea.focus()
    expect(document.activeElement).toBe(textarea)

    // Simulate live STATE_DELTA and run transitions re-rendering the parent.
    await wrapper.setProps({ backendState: 'ready' })
    await wrapper.setProps({ hasActiveRun: true })
    await wrapper.setProps({ backendState: 'reconciling', hasActiveRun: false })
    await nextTick()
    expect(document.activeElement).toBe(textarea)
    wrapper.unmount()
  })

  it('disables the composer when the backend reports unavailable or the parent disables it', () => {
    const backendDown = mountComposer({ props: { backendState: 'unavailable' } })
    expect(backendDown.wrapper.find('[data-agent-composer-unavailable]').exists()).toBe(true)
    expect(input(backendDown.wrapper).attributes('disabled')).toBeDefined()
    expect(send(backendDown.wrapper).attributes('disabled')).toBeDefined()
    backendDown.wrapper.unmount()

    const parentDisabled = mountComposer({ props: { disabled: true } })
    expect(input(parentDisabled.wrapper).attributes('disabled')).toBeDefined()
    // The parent owns the banner for its own disable reasons.
    expect(parentDisabled.wrapper.find('[data-agent-composer-unavailable]').exists()).toBe(false)
    parentDisabled.wrapper.unmount()
  })
})
