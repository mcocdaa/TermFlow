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
    expect(wrapper.get('h2').text()).toBe('添加电脑')
    expect(wrapper.text()).not.toContain('一次性注册')
    const nameInput = wrapper.get('input[name="computer-name"]')
    expect(nameInput.attributes('placeholder')).toBe('输入电脑名称')
    expect(wrapper.get('label[for="enrollment-computer-name"]').text()).toBe('电脑名称')
    expect(wrapper.get('[data-action="create-code"]').text()).toBe('创建')
    await nameInput.setValue('跑步工作站')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({ display_name: '跑步工作站' })
    const command = `termflow login --server ${window.location.origin} --code ${code}`
    expect(wrapper.get('h2').text()).toBe('添加电脑')
    expect(wrapper.text()).not.toContain('一次 termflow login 代表一台 Computer')
    expect(wrapper.get('[data-enrollment-field="code"] h3').text()).toBe('注册码')
    expect(wrapper.get('[data-enrollment-field="command"] h3').text()).toBe('终端执行')
    expect(wrapper.get('[data-help="login-command"]').attributes('aria-label')).toBe('终端执行说明')
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
    await wrapper.get('input[name="computer-name"]').setValue('自动刷新工作站')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('EXPIRES-NOW')
    await vi.advanceTimersByTimeAsync(1_100)
    await flushPromises()
    expect(wrapper.html()).not.toContain('EXPIRES-NOW')
    expect(wrapper.text()).toContain('FRESH-CODE')
    expect(wrapper.text()).toContain(`termflow login --server ${window.location.origin} --code FRESH-CODE`)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls.map((call) => JSON.parse(call[1].body as string))).toEqual([
      { display_name: '自动刷新工作站' },
      { display_name: '自动刷新工作站' },
    ])
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
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(EnrollmentDialog)

    await wrapper.get('input[name="computer-name"]').setValue('bad\u007fname')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.get('[role="alert"]').text()).toContain('1 至 128')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
