import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { createAppRouter } from '../router'
import type { DashboardDto } from '../api/types'

const dashboard: DashboardDto = {
  metrics: { online_terms: 2, active_panes: 5, interactions_24h: 37, computers: 2 },
  computers: [
    {
      computer_id: 'computer-1', display_name: '设计工作站', hostname: 'studio.local', platform: 'macOS', client_version: '1.2.0', online: true,
      online_term_count: 1, registered_at: '2026-07-30T10:00:00Z', last_seen_at: '2026-08-01T05:00:00Z',
      terms: [
        { term_id: 'term-1', computer_id: 'computer-1', name: '产品开发', online: true, window_count: 2, pane_count: 4, pane_current_command: 'python3', last_seen_at: '2026-08-01T05:00:00Z' },
        { term_id: 'term-2', computer_id: 'computer-1', name: '离线维护', online: false, window_count: 1, pane_count: 1, pane_current_command: 'zsh', last_seen_at: '2026-07-31T02:00:00Z' },
      ],
    },
    {
      computer_id: 'computer-2', display_name: '实验机', hostname: 'lab-box', platform: 'Linux', client_version: '1.2.0', online: true,
      online_term_count: 1, registered_at: '2026-07-29T10:00:00Z', last_seen_at: '2026-08-01T05:10:00Z',
      terms: [{ term_id: 'term-3', computer_id: 'computer-2', name: '数据任务', online: true, window_count: 1, pane_count: 1, pane_current_command: 'make', last_seen_at: '2026-08-01T05:10:00Z' }],
    },
  ],
}

describe('DashboardView', () => {
  it('renders server metrics and Computers with complete Term rows', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(dashboard), { status: 200, headers: { 'content-type': 'application/json' } })))
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }), history: createMemoryHistory() })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router] } })
    await flushPromises()

    expect(wrapper.text()).toContain('在线 Terms2')
    expect(wrapper.text()).toContain('活动 Panes5')
    expect(wrapper.text()).toContain('24 小时交互37')
    expect(wrapper.text()).toContain('Computers2')
    expect(wrapper.text()).toContain('设计工作站')
    expect(wrapper.text()).toContain('产品开发')
    expect(wrapper.text()).toContain('python3')
    expect(wrapper.text()).toContain('2 Windows')
    expect(wrapper.text()).toContain('4 Panes')
    expect(wrapper.get('a[href="/terms/term-1"]')).toBeTruthy()
    expect(wrapper.find('a[href="/terms/term-2"]').exists()).toBe(false)
    expect(wrapper.get('[data-term-id="term-2"] button').attributes('disabled')).toBeDefined()
  })

  it('cancels stale polling when the document becomes hidden', async () => {
    const fetchMock = vi.fn().mockImplementation((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    }))
    vi.stubGlobal('fetch', fetchMock)
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }), history: createMemoryHistory() })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router] } })
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true)
    wrapper.unmount()
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
  })
})
