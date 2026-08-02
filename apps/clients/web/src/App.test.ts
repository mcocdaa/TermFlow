import { mount } from '@vue/test-utils'
import { createClientUi } from '@termflow/client-ui'
import { describe, expect, it } from 'vitest'
import App from './App.vue'
import { createAppRouter } from './router'
import { createFakeRuntime } from './test/fakeRuntime'

async function renderAt(path: string) {
  const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }) })
  await router.push(path)
  await router.isReady()
  return mount(App, { global: { plugins: [router, createClientUi(createFakeRuntime())] } })
}

describe('application routes', () => {
  it.each([
    ['/login', '登录'],
    ['/', '控制中心'],
    ['/computers', '电脑管理'],
    ['/terms/term-7', '远程终端'],
    ['/missing', '页面不存在'],
  ])('renders %s in the application shell', async (path, heading) => {
    const wrapper = await renderAt(path)
    expect(wrapper.get('h1').text()).toContain(heading)
    expect(wrapper.get('[href="#main-content"]').text()).toBe('跳到主要内容')
  })

  it('redirects protected pages to login and retains the intended route', async () => {
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: false }) })
    await router.push('/terms/term-7')
    await router.isReady()
    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=/terms/term-7')
  })

  it('uses a bare shell for login and Lucide icons for application navigation', async () => {
    const login = await renderAt('/login')
    expect(login.find('.app-header').exists()).toBe(false)
    expect(login.find('.side-nav').exists()).toBe(false)
    expect(login.find('.mobile-nav').exists()).toBe(false)

    const dashboard = await renderAt('/')
    const dashboardLink = dashboard.get('.side-nav a[href="/"]')
    const computersLink = dashboard.get('.side-nav a[href="/computers"]')
    expect(dashboardLink.text()).toBe('控制中心')
    expect(computersLink.text()).toBe('电脑管理')
    expect(dashboardLink.find('svg').exists()).toBe(true)
    expect(computersLink.find('svg').exists()).toBe(true)
    expect(dashboard.get('.mobile-nav a[href="/"]').find('svg').exists()).toBe(true)
    expect(dashboard.get('.mobile-nav a[href="/computers"]').find('svg').exists()).toBe(true)
  })
})
