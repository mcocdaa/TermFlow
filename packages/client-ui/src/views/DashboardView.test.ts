import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import DashboardView from './DashboardView.vue'

type DashboardSnapshot = Awaited<ReturnType<ClientRuntime['api']['dashboard']['get']>>

const dashboard: DashboardSnapshot = {
  metrics: { online_terms: 2, total_terms: 3, active_panes: 5, interactions_24h: 37, computers: 2 },
  computers: [
    {
      installation_id: 'computer-1', display_name: '设计工作站', hostname: 'studio.local', platform: 'macOS', client_version: '1.2.0', online: true,
      registered_at: '2026-07-30T10:00:00Z', last_seen_at: '2026-08-01T05:00:00Z',
      terms: [
        { instance_id: 'term-1', name: '产品开发', online: true, window_count: 2, pane_count: 4, active_pane_count: 1, current_command: 'python3', last_seen_at: '2026-08-01T05:00:00Z' },
        { instance_id: 'term-2', name: '离线维护', online: false, window_count: 1, pane_count: 1, active_pane_count: 0, current_command: 'zsh', last_seen_at: '2026-07-31T02:00:00Z' },
      ],
    },
    {
      installation_id: 'computer-2', display_name: '实验机', hostname: 'lab-box', platform: 'Linux', client_version: '1.2.0', online: true,
      registered_at: '2026-07-29T10:00:00Z', last_seen_at: '2026-08-01T05:10:00Z',
      terms: [{ instance_id: 'term-3', name: '数据任务', online: true, window_count: 1, pane_count: 1, active_pane_count: 1, current_command: 'make', last_seen_at: '2026-08-01T05:10:00Z' }],
    },
  ],
}

async function mountDashboard(runtime: ClientRuntime) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: DashboardView },
      { path: '/terms/:termId', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()
  return mount(DashboardView, { global: { plugins: [router, createClientUi(runtime)] } })
}

describe('DashboardView', () => {
  it('renders runtime metrics and Computers with complete Term rows', async () => {
    const getDashboard = vi.fn().mockResolvedValue(dashboard)
    const runtime = createFakeRuntime({ api: { dashboard: { get: getDashboard } } as unknown as ClientRuntime['api'] })
    const wrapper = await mountDashboard(runtime)
    await flushPromises()

    expect(getDashboard).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('在线 Terms2')
    expect(wrapper.text()).toContain('共 3 Terms')
    expect(wrapper.text()).toContain('活动 Panes5')
    expect(wrapper.text()).toContain('24 小时交互37')
    expect(wrapper.text()).toContain('Computers2')
    const metricCards = wrapper.findAll('.metric-card')
    expect(metricCards).toHaveLength(4)
    expect(metricCards.every((card) => Boolean(card.attributes('aria-describedby')))).toBe(true)
    expect(metricCards.find((card) => card.text().includes('在线 Terms'))?.get('[role="tooltip"]').text()).toContain('共 3 个 Term')
    expect(metricCards.find((card) => card.text().includes('活动 Panes'))?.get('[role="tooltip"]').text()).toContain('当前处于活动状态')
    expect(metricCards.find((card) => card.text().includes('24 小时交互'))?.get('[role="tooltip"]').text()).toContain('过去 24 小时')
    expect(metricCards.find((card) => card.text().includes('Computers'))?.get('[role="tooltip"]').text()).toContain('2 台在线')
    expect(wrapper.text()).toContain('设计工作站')
    expect(wrapper.text()).toContain('产品开发')
    expect(wrapper.text()).toContain('python3')
    expect(wrapper.text()).toContain('2 Windows')
    expect(wrapper.text()).toContain('4 Panes')
    const onlineTerm = wrapper.get('[data-term-id="term-1"]')
    expect(onlineTerm.element.tagName).toBe('A')
    expect(onlineTerm.attributes('href')).toBe('/terms/term-1')
    expect(onlineTerm.attributes('aria-label')).toContain('产品开发')
    expect(onlineTerm.text()).not.toContain('打开终端')
    expect(onlineTerm.get('.term-last-seen').element.tagName).toBe('TIME')
    expect(wrapper.find('a[href="/terms/term-2"]').exists()).toBe(false)
    const offlineTerm = wrapper.get('[data-term-id="term-2"]')
    expect(offlineTerm.element.tagName).toBe('ARTICLE')
    expect(offlineTerm.find('button').exists()).toBe(false)
    expect(offlineTerm.find('a').exists()).toBe(false)
  })

  it('does not render an isolated metadata separator for an unnamed host', async () => {
    const sparseDashboard: DashboardSnapshot = {
      metrics: { online_terms: 0, total_terms: 0, active_panes: 0, interactions_24h: 0, computers: 1 },
      computers: [{
        installation_id: 'computer-sparse', display_name: 'Computer', hostname: null, platform: null, client_version: null, online: false,
        registered_at: '2026-08-01T00:00:00Z', last_seen_at: null, terms: [],
      }],
    }
    const runtime = createFakeRuntime({ api: { dashboard: { get: vi.fn().mockResolvedValue(sparseDashboard) } } as unknown as ClientRuntime['api'] })
    const wrapper = await mountDashboard(runtime)
    await flushPromises()

    const card = wrapper.get('[data-computer-id="computer-sparse"]')
    expect(card.text()).not.toContain('·')
    expect(card.text()).not.toContain('null')
  })

  it('aborts stale polling through runtime visibility and clock ports', async () => {
    let visibilityListener: (() => void) | undefined
    let hidden = false
    let signal: AbortSignal | undefined
    const getDashboard = vi.fn((nextSignal?: AbortSignal) => {
      signal = nextSignal
      return new Promise<DashboardSnapshot>((_resolve, reject) => {
        nextSignal?.addEventListener('abort', () => reject(new Error('aborted')))
      })
    })
    const clearTimeout = vi.fn()
    const runtime = createFakeRuntime({
      api: { dashboard: { get: getDashboard } } as unknown as ClientRuntime['api'],
      clock: { now: () => 0, setTimeout: () => 7, clearTimeout, setInterval: () => 1, clearInterval: () => undefined },
      visibility: {
        isHidden: () => hidden,
        subscribe: (listener) => { visibilityListener = listener; return vi.fn() },
      },
    })
    const wrapper = await mountDashboard(runtime)
    await flushPromises()
    hidden = true
    visibilityListener?.()
    await flushPromises()

    expect(getDashboard).toHaveBeenCalledTimes(1)
    expect(signal?.aborted).toBe(true)
    wrapper.unmount()
  })
})
