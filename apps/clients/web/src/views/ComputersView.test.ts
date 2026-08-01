import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { createAppRouter } from '../router'

const computer = {
  computer_id: 'machine-1', display_name: '主工作站', hostname: 'devbox', platform: 'Linux x86_64', client_version: '1.4.2', online: true,
  online_term_count: 3, registered_at: '2026-07-20T00:00:00Z', last_seen_at: '2026-08-01T01:00:00Z', terms: [],
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
    expect(wrapper.text()).toContain('Linux x86_64')
    expect(wrapper.text()).toContain('1.4.2')
    expect(wrapper.text()).toContain('3 个在线 Term')
    await wrapper.get('[data-action="edit-name"]').trigger('click')
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
})
