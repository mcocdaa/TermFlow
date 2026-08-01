import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import EnrollmentDialog from './EnrollmentDialog.vue'

describe('EnrollmentDialog', () => {
  it('creates a one-time code only on request, builds the login command, copies it, and clears on close', async () => {
    const code = 'JOIN-7P4W-SECRET'
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ token: code, expires_at: new Date(Date.now() + 600_000).toISOString() }), { status: 201, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const wrapper = mount(EnrollmentDialog)

    expect(fetchMock).not.toHaveBeenCalled()
    await wrapper.get('[data-action="create-code"]').trigger('click')
    await flushPromises()

    const command = `termflow login --server ${window.location.origin} --code ${code}`
    expect(wrapper.text()).not.toContain('一次 termflow login 代表一台 Computer')
    expect(wrapper.get('[data-enrollment-field="code"] h3').text()).toBe('注册码')
    expect(wrapper.get('[data-enrollment-field="command"] h3').text()).toBe('终端执行命令')
    expect(wrapper.get('[data-help="login-command"]').attributes('aria-label')).toBe('终端执行命令说明')
    expect(wrapper.get('[role="tooltip"]').text()).toContain('复制到安装有 TermFlow 的电脑上')
    expect(wrapper.get('[data-action="copy-command"]').text()).toContain('复制命令')
    expect(wrapper.text()).toContain(code)
    expect(wrapper.text()).toContain(command)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
    await wrapper.get('[data-action="copy-command"]').trigger('click')
    expect(wrapper.get('[data-action="copy-command"]').text()).toContain('已复制')
    expect(writeText).toHaveBeenCalledWith(command)
    await wrapper.get('[data-action="close-enrollment"]').trigger('click')
    expect(wrapper.html()).not.toContain(code)
  })

  it('automatically replaces an expired code while the dialog remains open', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-01T00:00:00Z'))
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: 'EXPIRES-NOW', expires_at: '2026-08-01T00:00:01Z' }), { status: 201, headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: 'FRESH-CODE', expires_at: '2026-08-01T00:01:01Z' }), { status: 201, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(EnrollmentDialog)
    await wrapper.get('[data-action="create-code"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('EXPIRES-NOW')
    await vi.advanceTimersByTimeAsync(1_100)
    await flushPromises()
    expect(wrapper.html()).not.toContain('EXPIRES-NOW')
    expect(wrapper.text()).toContain('FRESH-CODE')
    expect(wrapper.text()).toContain(`termflow login --server ${window.location.origin} --code FRESH-CODE`)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('moves focus into the modal, traps Tab, closes with Escape, and restores the invoker', async () => {
    const invoker = document.createElement('button')
    document.body.append(invoker)
    invoker.focus()
    const wrapper = mount(EnrollmentDialog, { attachTo: document.body })
    await nextTick()

    const first = wrapper.get('[data-action="close-enrollment"]').element as HTMLButtonElement
    const last = wrapper.get('[data-action="create-code"]').element as HTMLButtonElement
    expect(document.activeElement).toBe(first)
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
})
