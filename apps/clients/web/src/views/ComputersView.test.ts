import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { createAppRouter } from '../router'

const computer = {
  installation_id: 'machine-1', display_name: '主工作站', hostname: 'devbox', platform: 'Linux x86_64', client_version: '1.4.2', online: true,
  registered_at: '2026-07-20T00:00:00Z', last_seen_at: '2026-08-01T01:00:00Z', terms: [
    { instance_id: 't1', name: 'one', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'sh', last_seen_at: null },
    { instance_id: 't2', name: 'two', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'sh', last_seen_at: null },
    { instance_id: 't3', name: 'three', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'sh', last_seen_at: null },
  ],
}

describe('ComputersView', () => {
  it('lists complete metadata and saves a validated display name', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ computers: [computer] }), { status: 200, headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...computer, display_name: '构建主机' }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }), history: createMemoryHistory() })
    await router.push('/computers')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.text()).toContain('devbox')
    expect(wrapper.text()).toContain('操作系统')
    expect(wrapper.text()).not.toContain('平台')
    expect(wrapper.text()).toContain('Linux x86_64')
    expect(wrapper.text()).toContain('1.4.2')
    expect(wrapper.text()).toContain('3 个在线 Term')
    const nameTrigger = wrapper.get('[data-action="edit-name"]')
    expect(nameTrigger.text()).toBe('主工作站')
    expect(nameTrigger.attributes('aria-label')).toBe('修改 Computer 名称：主工作站')
    await nameTrigger.trigger('click')
    await wrapper.get('input[name="display-name"]').setValue('构建主机')
    await wrapper.get('[data-action="save-name"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('构建主机')
    expect(fetchMock.mock.calls[1][1].method).toBe('PATCH')
  })

  it('rejects control characters before sending a rename request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ computers: [computer] }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }), history: createMemoryHistory() })
    await router.push('/computers')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router] } })
    await flushPromises()
    await wrapper.get('[data-action="edit-name"]').trigger('click')
    await wrapper.get('input[name="display-name"]').setValue('bad\u007fname')
    await wrapper.get('[data-action="save-name"]').trigger('click')
    expect(wrapper.get('[role="alert"]').text()).toContain('1 至 128')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('omits absent identity metadata and explains B-recorded local time', async () => {
    const sparseComputer = {
      ...computer,
      installation_id: 'machine-sparse',
      display_name: 'Computer',
      hostname: null,
      platform: null,
      client_version: null,
      terms: [],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ computers: [sparseComputer] }), { status: 200, headers: { 'content-type': 'application/json' } })))
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }), history: createMemoryHistory() })
    await router.push('/computers')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router] } })
    await flushPromises()

    const row = wrapper.get('[data-computer-id="machine-sparse"]')
    expect(row.text()).not.toContain('未报告 hostname')
    expect(row.text()).not.toContain('TermFlow null')
    expect(row.text()).not.toContain('·')
    expect(wrapper.text()).toContain('由 B 记录，按当前设备时区显示')
    expect(row.get('time').text()).toMatch(/UTC|GMT|CST/)
  })
})
