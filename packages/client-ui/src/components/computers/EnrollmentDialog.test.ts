import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../../runtime'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import EnrollmentDialog from './EnrollmentDialog.vue'

function mountEnrollment(runtime = createFakeRuntime()) {
  return mount(EnrollmentDialog, { global: { plugins: [createClientUi(runtime)] } })
}

describe('EnrollmentDialog', () => {
  it('creates through runtime, builds from the canonical URL, copies through runtime, and clears on close', async () => {
    const code = 'JOIN-7P4W-SECRET'
    const createEnrollment = vi.fn().mockResolvedValue({ token: code, expires_at: '2026-08-01T00:10:00Z' })
    const writeText = vi.fn().mockResolvedValue(undefined)
    const runtime = createFakeRuntime({
      api: { computers: { createEnrollment } } as unknown as ClientRuntime['api'],
      clipboard: { writeText },
      clock: { now: () => Date.parse('2026-08-01T00:00:00Z'), setTimeout: () => 1, clearTimeout: () => undefined, setInterval: () => 2, clearInterval: () => undefined },
      canonicalServerUrl: 'https://b.termflow.test',
    })
    const wrapper = mountEnrollment(runtime)

    expect(createEnrollment).not.toHaveBeenCalled()
    expect(wrapper.get('h2').text()).toBe('添加电脑')
    const nameInput = wrapper.get('input[name="computer-name"]')
    expect(nameInput.attributes('placeholder')).toBe('输入电脑名称')
    expect(wrapper.get('label[for="enrollment-computer-name"]').text()).toBe('电脑名称')
    await nameInput.setValue('跑步工作站')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(createEnrollment).toHaveBeenCalledWith('跑步工作站')
    const command = `termflow login --server https://b.termflow.test --code ${code}`
    expect(wrapper.get('[data-enrollment-field="code"] h3').text()).toBe('注册码')
    expect(wrapper.get('[data-enrollment-field="command"] h3').text()).toBe('终端执行')
    expect(wrapper.get('[data-help="login-command"]').attributes('aria-label')).toBe('终端执行说明')
    expect(wrapper.get('[role="tooltip"]').text()).toContain('复制到安装有 TermFlow 的电脑上')
    expect(wrapper.text()).toContain(code)
    expect(wrapper.text()).toContain(command)
    await wrapper.get('[data-action="copy-command"]').trigger('click')
    expect(wrapper.get('[data-action="copy-command"]').text()).toContain('已复制')
    expect(writeText).toHaveBeenCalledWith(command)
    await wrapper.get('[data-action="close-enrollment"]').trigger('click')
    expect(wrapper.html()).not.toContain(code)
  })

  it('automatically replaces an expired code using the runtime clock', async () => {
    let now = Date.parse('2026-08-01T00:00:00Z')
    let tick: (() => void) | undefined
    const createEnrollment = vi.fn()
      .mockResolvedValueOnce({ token: 'EXPIRES-NOW', expires_at: '2026-08-01T00:00:01Z' })
      .mockResolvedValueOnce({ token: 'FRESH-CODE', expires_at: '2026-08-01T00:01:01Z' })
    const runtime = createFakeRuntime({
      api: { computers: { createEnrollment } } as unknown as ClientRuntime['api'],
      clock: {
        now: () => now,
        setTimeout: () => 1,
        clearTimeout: () => undefined,
        setInterval: (callback) => { tick = callback; return 2 },
        clearInterval: () => undefined,
      },
      canonicalServerUrl: 'https://b.termflow.test',
    })
    const wrapper = mountEnrollment(runtime)
    await wrapper.get('input[name="computer-name"]').setValue('自动刷新工作站')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('EXPIRES-NOW')

    now += 1_100
    tick?.()
    await flushPromises()

    expect(wrapper.html()).not.toContain('EXPIRES-NOW')
    expect(wrapper.text()).toContain('FRESH-CODE')
    expect(wrapper.text()).toContain('termflow login --server https://b.termflow.test --code FRESH-CODE')
    expect(createEnrollment).toHaveBeenCalledTimes(2)
    expect(createEnrollment.mock.calls).toEqual([['自动刷新工作站'], ['自动刷新工作站']])
    wrapper.unmount()
  })

  it('moves focus into the modal, traps Tab, closes with Escape, and restores the invoker', async () => {
    const invoker = document.createElement('button')
    document.body.append(invoker)
    invoker.focus()
    const wrapper = mount(EnrollmentDialog, { attachTo: document.body, global: { plugins: [createClientUi(createFakeRuntime())] } })
    await nextTick()

    const first = wrapper.get('[data-action="close-enrollment"]').element as HTMLButtonElement
    const last = wrapper.get('[data-action="create-code"]').element as HTMLButtonElement
    expect(document.activeElement).toBe(wrapper.get('input[name="computer-name"]').element)
    last.focus()
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Tab' })
    expect(document.activeElement).toBe(first)
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.emitted('closed')).toHaveLength(1)
    expect(document.activeElement).toBe(invoker)

    wrapper.unmount()
    invoker.remove()
  })

  it('rejects unsafe Computer names before requesting a code', async () => {
    const createEnrollment = vi.fn()
    const runtime = createFakeRuntime({ api: { computers: { createEnrollment } } as unknown as ClientRuntime['api'] })
    const wrapper = mountEnrollment(runtime)

    await wrapper.get('input[name="computer-name"]').setValue('bad\u007fname')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.get('[role="alert"]').text()).toContain('1 至 128')
    expect(createEnrollment).not.toHaveBeenCalled()
  })
})
