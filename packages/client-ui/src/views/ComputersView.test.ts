import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import ComputersView from './ComputersView.vue'

type Computer = Awaited<ReturnType<ClientRuntime['api']['computers']['list']>>['computers'][number]

const computer: Computer = {
  installation_id: 'machine-1', display_name: '主工作站', hostname: 'devbox', platform: 'Linux x86_64', client_version: '1.4.2', online: true,
  registered_at: '2026-07-20T00:00:00Z', last_seen_at: '2026-08-01T01:00:00Z', terms: [
    { instance_id: 't1', name: 'one', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'sh', last_seen_at: null },
    { instance_id: 't2', name: 'two', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'sh', last_seen_at: null },
    { instance_id: 't3', name: 'three', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'sh', last_seen_at: null },
  ],
}

function mountComputers(runtime: ClientRuntime) {
  return mount(ComputersView, { global: { plugins: [createClientUi(runtime)] } })
}

describe('ComputersView', () => {
  it('renders five Chinese columns, one online Term pill, and saves a validated display name', async () => {
    const list = vi.fn().mockResolvedValue({ computers: [computer] })
    const rename = vi.fn().mockResolvedValue({ ...computer, display_name: '构建主机' })
    const runtime = createFakeRuntime({ api: { computers: { list, rename } } as unknown as ClientRuntime['api'] })
    const wrapper = mountComputers(runtime)
    await flushPromises()

    expect(list).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('[role="columnheader"]').map((header) => header.text())).toEqual(['名称', '终端', '最近在线', '注册时间', '操作'])
    expect(wrapper.text()).toContain('devbox')
    expect(wrapper.text()).not.toContain('操作系统')
    expect(wrapper.text()).not.toContain('Linux x86_64')
    expect(wrapper.text()).not.toContain('1.4.2')
    const row = wrapper.get('[data-computer-id="machine-1"]')
    expect(row.findAll('.status-pill')).toHaveLength(1)
    expect(row.get('.status-pill').text()).toBe('在线 (3)')
    const onlineDelete = row.get('[data-action="delete-computer"]')
    expect(onlineDelete.attributes('disabled')).toBeDefined()
    expect(onlineDelete.attributes('aria-label')).toContain('在线')
    expect(onlineDelete.find('svg').exists()).toBe(true)
    const nameTrigger = wrapper.get('[data-action="edit-name"]')
    expect(nameTrigger.text()).toBe('主工作站')
    expect(nameTrigger.attributes('aria-label')).toBe('修改 Computer 名称：主工作站')
    await nameTrigger.trigger('click')
    await wrapper.get('input[name="display-name"]').setValue('构建主机')
    await wrapper.get('[data-action="save-name"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('构建主机')
    expect(rename).toHaveBeenCalledWith('machine-1', '构建主机')
  })

  it('confirms and removes an offline Computer with the shared dialog and a dismissing bottom success toast', async () => {
    const offlineComputer = {
      ...computer,
      installation_id: 'machine-offline',
      display_name: '离线工作站',
      online: false,
      terms: [{ ...computer.terms[0]!, instance_id: 'offline-term', online: false }],
    }
    const list = vi.fn().mockResolvedValue({ computers: [computer, offlineComputer] })
    const remove = vi.fn().mockResolvedValue(undefined)
    let dismissNotice: (() => void) | undefined
    const clearTimeout = vi.fn()
    const runtime = createFakeRuntime({
      api: { computers: { list, remove } } as unknown as ClientRuntime['api'],
      clock: {
        now: () => 0,
        setTimeout: (callback, delay) => { expect(delay).toBe(3_000); dismissNotice = callback; return 17 },
        clearTimeout,
        setInterval: () => 1,
        clearInterval: () => undefined,
      },
    })
    const wrapper = mountComputers(runtime)
    await flushPromises()

    const action = wrapper.get('[data-computer-id="machine-offline"] [data-action="delete-computer"]')
    expect(action.attributes('disabled')).toBeUndefined()
    await action.trigger('click')
    expect(remove).not.toHaveBeenCalled()
    const dialog = wrapper.get('[role="alertdialog"]')
    expect(dialog.text()).toContain('删除电脑')
    expect(dialog.text()).toContain('离线工作站')
    await dialog.get('[data-action="confirm-delete-computer"]').trigger('click')
    await flushPromises()

    expect(remove).toHaveBeenCalledWith('machine-offline')
    expect(wrapper.find('[data-computer-id="machine-offline"]').exists()).toBe(false)
    const notice = wrapper.get('[data-delete-notice]')
    expect(notice.attributes('role')).toBe('status')
    expect(notice.attributes('data-tone')).toBe('success')
    expect(notice.text()).toBe('已删除')
    expect(wrapper.find('.computers-view > .form-error').exists()).toBe(false)
    dismissNotice?.()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-delete-notice]').exists()).toBe(false)
    wrapper.unmount()
    expect(clearTimeout).not.toHaveBeenCalled()
  })

  it('keeps the confirmation dialog open and shows an inline error when deleting a Computer fails', async () => {
    const offlineComputer = { ...computer, installation_id: 'machine-offline', online: false, terms: [] }
    const list = vi.fn().mockResolvedValue({ computers: [offlineComputer] })
    const remove = vi.fn().mockRejectedValue(new Error('network failure'))
    const clearTimeout = vi.fn()
    const runtime = createFakeRuntime({
      api: { computers: { list, remove } } as unknown as ClientRuntime['api'],
      clock: {
        now: () => 0,
        setTimeout: () => 23,
        clearTimeout,
        setInterval: () => 1,
        clearInterval: () => undefined,
      },
    })
    const wrapper = mountComputers(runtime)
    await flushPromises()
    await wrapper.get('[data-action="delete-computer"]').trigger('click')
    await wrapper.get('[data-action="confirm-delete-computer"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alertdialog"] [role="alert"]').text()).toBe('无法删除电脑。')
    expect(wrapper.find('[data-computer-id="machine-offline"]').exists()).toBe(true)
    wrapper.unmount()
    expect(clearTimeout).not.toHaveBeenCalled()
  })

  it('rejects control characters before sending a rename request', async () => {
    const list = vi.fn().mockResolvedValue({ computers: [computer] })
    const rename = vi.fn()
    const runtime = createFakeRuntime({ api: { computers: { list, rename } } as unknown as ClientRuntime['api'] })
    const wrapper = mountComputers(runtime)
    await flushPromises()
    await wrapper.get('[data-action="edit-name"]').trigger('click')
    await wrapper.get('input[name="display-name"]').setValue('bad\u007fname')
    await wrapper.get('[data-action="save-name"]').trigger('click')
    expect(wrapper.get('[role="alert"]').text()).toContain('1 至 128')
    expect(rename).not.toHaveBeenCalled()
  })

  it('omits absent identity metadata, the old time note, and timezone suffixes', async () => {
    const sparseComputer = { ...computer, installation_id: 'machine-sparse', display_name: 'Computer', hostname: null, platform: null, client_version: null, terms: [] }
    const runtime = createFakeRuntime({ api: { computers: { list: vi.fn().mockResolvedValue({ computers: [sparseComputer] }) } } as unknown as ClientRuntime['api'] })
    const wrapper = mountComputers(runtime)
    await flushPromises()

    const row = wrapper.get('[data-computer-id="machine-sparse"]')
    expect(row.text()).not.toContain('未报告 hostname')
    expect(row.text()).not.toContain('TermFlow null')
    expect(row.text()).not.toContain('·')
    expect(wrapper.text()).not.toContain('由 B 记录，按当前设备时区显示')
    expect(row.get('time').text()).not.toMatch(/UTC|GMT|CST/)
  })

  it('closes enrollment, refreshes the list, and shows the bottom success toast after login succeeds', async () => {
    const callbacks = new Map<number, () => void>()
    const addedComputer = { ...computer, installation_id: 'machine-added', display_name: '刚添加的电脑', online: false, terms: [] }
    const list = vi.fn()
      .mockResolvedValueOnce({ computers: [computer] })
      .mockResolvedValueOnce({ computers: [computer] })
      .mockResolvedValueOnce({ computers: [computer, addedComputer] })
      .mockResolvedValueOnce({ computers: [computer, addedComputer] })
    const createEnrollment = vi.fn().mockResolvedValue({
      token: 'ADD-CODE',
      expires_at: '2026-08-03T12:01:00Z',
      server_url: 'https://relay.example.com',
      login_command: 'termflow login --server https://relay.example.com --code ADD-CODE',
    })
    const runtime = createFakeRuntime({
      api: { computers: { list, createEnrollment, remove: vi.fn() } } as unknown as ClientRuntime['api'],
      clock: {
        now: () => Date.parse('2026-08-03T12:00:00Z'),
        setTimeout: () => 1,
        clearTimeout: () => undefined,
        setInterval: (callback, delay) => { callbacks.set(delay, callback); return delay },
        clearInterval: () => undefined,
      },
    })
    const wrapper = mountComputers(runtime)
    await flushPromises()
    await wrapper.get('.page-heading .primary-button').trigger('click')
    await wrapper.get('input[name="computer-name"]').setValue('刚添加的电脑')
    await wrapper.get('.enrollment-create-form').trigger('submit')
    await flushPromises()

    callbacks.get(1000)?.()
    await flushPromises()

    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    const notice = wrapper.get('[data-delete-notice]')
    expect(notice.attributes('data-tone')).toBe('success')
    expect(notice.attributes('role')).toBe('status')
    expect(notice.text()).toBe('已添加')
    expect(wrapper.get('[data-computer-id="machine-added"]').text()).toContain('刚添加的电脑')
    expect(list).toHaveBeenCalledTimes(4)
  })
})
