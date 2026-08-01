import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import App from './App.vue'
import { createAppRouter } from './router'

async function renderAt(path: string) {
  const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }) })
  await router.push(path)
  await router.isReady()
  return mount(App, { global: { plugins: [router] } })
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
})
