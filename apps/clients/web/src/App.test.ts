import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App, createClientUi } from '@termflow/client-ui'
import { createFakeRuntime } from './test/fakeRuntime'
import { createAppRouter } from './router'

afterEach(() => {
  document.body.innerHTML = ''
})

it('gives the mobile navigation icon-only presentation with accessible labels', async () => {
  const runtime = createFakeRuntime()
  runtime.api.agents.capabilities = vi.fn(async () => ({
    agent_broker_enabled: true,
    state: 'ready',
    reason_code: null,
    delegated_write_grants_enabled: false,
  })) as never
  const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }) })
  await router.push('/settings')
  await router.isReady()
  const wrapper = mount(App, {
    attachTo: document.body,
    global: {
      plugins: [router, createClientUi(runtime)],
      stubs: { RouterView: { template: '<div data-router-view />' } },
    },
  })
  await flushPromises()

  const links = wrapper.findAll('.mobile-nav a')
  expect(links).toHaveLength(4)
  expect(links.map((link) => link.attributes('aria-label'))).toEqual([
    '控制中心',
    '电脑管理',
    '设置',
    'Agent 控制台',
  ])
  expect(links.every((link) => link.find('.nav-label').exists())).toBe(true)
  wrapper.unmount()
})
