import { flushPromises, mount } from '@vue/test-utils'
import type { TerminalSessionLike } from '@termflow/client-core'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import { createClientUi } from './runtime'
import { clientRoutes } from './router/routes'
import { createFakeRuntime } from './test/fakeRuntime'

async function renderAt(path: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
  await router.push(path)
  await router.isReady()
  const clientUi = createClientUi(createFakeRuntime())
  return { wrapper: mount(App, { global: { plugins: [router, clientUi] } }), clientUi }
}

beforeEach(() => { vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined) })
afterEach(() => { vi.restoreAllMocks() })

describe('shared application routes', () => {
  it('adds the page lock roots only for the terminal route', async () => {
    const root = document.createElement('div')
    root.id = 'app'
    document.body.append(root)
    const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, {
      attachTo: root,
      global: { plugins: [router, createClientUi(createFakeRuntime())] },
    })
    const roots = [document.documentElement, document.body, root]
    expect(roots.every((element) => !element.classList.contains('termflow-terminal-route'))).toBe(true)

    await router.push('/terms/term-7')
    await flushPromises()
    expect(roots.every((element) => element.classList.contains('termflow-terminal-route'))).toBe(true)

    await router.push('/')
    await flushPromises()
    expect(roots.every((element) => !element.classList.contains('termflow-terminal-route'))).toBe(true)
    expect(window.scrollTo).toHaveBeenCalledOnce()
    wrapper.unmount()
    root.remove()
  })

  it('replaces the terminal session when navigating between Term route parameters', async () => {
    const first: TerminalSessionLike = { connect: vi.fn(), sendInput: vi.fn(), sendAction: vi.fn(), dispose: vi.fn() }
    const second: TerminalSessionLike = { connect: vi.fn(), sendInput: vi.fn(), sendAction: vi.fn(), dispose: vi.fn() }
    const terminals = [first, second]
    const createdTermIds: string[] = []
    const createTerminal = vi.fn((termId: string) => {
      createdTermIds.push(termId)
      return terminals[createdTermIds.length - 1]!
    })
    const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
    await router.push('/terms/term-a')
    await router.isReady()
    const wrapper = mount(App, {
      global: { plugins: [router, createClientUi(createFakeRuntime({ createTerminal }))] },
    })
    await flushPromises()

    await router.push('/terms/term-b')
    await flushPromises()

    expect(createdTermIds).toEqual(['term-a', 'term-b'])
    expect(first.dispose).toHaveBeenCalledOnce()
    expect(second.connect).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it.each([
    ['/login', '登录'],
    ['/', '控制中心'],
    ['/computers', '电脑管理'],
    ['/terms/term-7', '远程终端'],
    ['/missing', '页面不存在'],
  ])('renders %s in the application shell', async (path, heading) => {
    const { wrapper } = await renderAt(path)
    expect(wrapper.get('h1').text()).toContain(heading)
    expect(wrapper.get('[href="#main-content"]').text()).toBe('跳到主要内容')
    wrapper.unmount()
  })

  it('uses a bare shell for login and Lucide icons for application navigation', async () => {
    const { wrapper: login } = await renderAt('/login')
    expect(login.find('.app-header').exists()).toBe(false)
    expect(login.find('.side-nav').exists()).toBe(false)
    expect(login.find('.mobile-nav').exists()).toBe(false)
    login.unmount()

    const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
    await router.push('/')
    await router.isReady()
    const clientUi = createClientUi(createFakeRuntime())
    await clientUi.session.loginWithToken('test-only-token')
    const dashboard = mount(App, { global: { plugins: [router, clientUi] } })
    const dashboardLink = dashboard.get('.side-nav a[href="/"]')
    const computersLink = dashboard.get('.side-nav a[href="/computers"]')
    expect(dashboardLink.text()).toBe('控制中心')
    expect(computersLink.text()).toBe('电脑管理')
    expect(dashboardLink.find('svg').exists()).toBe(true)
    expect(computersLink.find('svg').exists()).toBe(true)
    expect(dashboard.get('.mobile-nav a[href="/"]').find('svg').exists()).toBe(true)
    expect(dashboard.get('.mobile-nav a[href="/computers"]').find('svg').exists()).toBe(true)
    const logout = dashboard.get('[data-action="logout"]')
    expect(logout.attributes('aria-label')).toBe('退出登录')
    expect(logout.attributes('title')).toBe('退出登录')
    expect(logout.find('svg').exists()).toBe(true)
    expect(logout.get('.logout-label').text()).toBe('退出')
    dashboard.unmount()
  })

  it('restores an existing runtime session when the application starts', async () => {
    const status = vi.fn().mockResolvedValue({ authenticated: true, expires_at: '2026-08-05T12:00:00Z' })
    const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, {
      global: { plugins: [router, createClientUi(createFakeRuntime({
        api: { sessions: { status } } as unknown as ReturnType<typeof createFakeRuntime>['api'],
      }))] },
    })

    await flushPromises()

    expect(status).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-action="logout"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('does not probe a browser session on a bare login route', async () => {
    const status = vi.fn().mockResolvedValue({ authenticated: false, expires_at: null })
    const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
    await router.push('/login')
    await router.isReady()
    const wrapper = mount(App, {
      global: { plugins: [router, createClientUi(createFakeRuntime({
        api: { sessions: { status } } as unknown as ReturnType<typeof createFakeRuntime>['api'],
      }))] },
    })

    await flushPromises()

    expect(status).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('restores a native session after leaving a bare connection route', async () => {
    const status = vi.fn().mockResolvedValue({ authenticated: true, expires_at: '2026-08-05T12:00:00Z' })
    const router = createRouter({ history: createMemoryHistory(), routes: clientRoutes })
    await router.push('/login')
    await router.isReady()
    const wrapper = mount(App, {
      global: { plugins: [router, createClientUi(createFakeRuntime({
        api: { sessions: { status } } as unknown as ReturnType<typeof createFakeRuntime>['api'],
      }))] },
    })
    await flushPromises()

    await router.push('/')
    await flushPromises()

    expect(status).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-action="logout"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
