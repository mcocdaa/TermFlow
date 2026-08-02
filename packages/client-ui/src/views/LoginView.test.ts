import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@termflow/client-core'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import LoginView from './LoginView.vue'

async function mountLogin(runtime: ClientRuntime, path = '/login') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', component: LoginView },
      { path: '/computers', component: { template: '<div />' } },
      { path: '/', component: { template: '<div />' } },
    ],
  })
  await router.push(path)
  await router.isReady()
  return { router, wrapper: mount(LoginView, { global: { plugins: [router, createClientUi(runtime)] } }) }
}

describe('LoginView', () => {
  it('submits the token once through runtime, clears it, and navigates only to a safe redirect', async () => {
    const secret = 'tf_admin_super_secret_93'
    const login = vi.fn().mockResolvedValue({ authenticated: true, expires_at: '2026-08-01T01:00:00Z' })
    const runtime = createFakeRuntime({ api: { sessions: { login } } as unknown as ClientRuntime['api'] })
    const { router, wrapper } = await mountLogin(runtime, '/login?redirect=/computers')

    expect(wrapper.get('h1').text()).toBe('登录')
    expect(wrapper.get('label[for="admin-token"]').text()).toBe('管理员令牌')
    expect(wrapper.get('button[type="submit"]').text()).toBe('登录')
    expect(wrapper.text()).not.toContain('安全会话')
    expect(wrapper.text()).not.toContain('浏览器不会保存')
    expect(wrapper.text()).not.toContain('创建会话')

    await wrapper.get('input[type="password"]').setValue(secret)
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(login).toHaveBeenCalledOnce()
    expect(login).toHaveBeenCalledWith(secret)
    expect(router.currentRoute.value.fullPath).toBe('/computers')
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('')
    expect(wrapper.html()).not.toContain(secret)
    expect(wrapper.emitted()).not.toContain(secret)
  })

  it('shows a safe authentication error and clears the submitted token', async () => {
    const login = vi.fn().mockRejectedValue(new ApiError('authentication'))
    const runtime = createFakeRuntime({ api: { sessions: { login } } as unknown as ClientRuntime['api'] })
    const { wrapper } = await mountLogin(runtime)

    await wrapper.get('input[type="password"]').setValue('do-not-render-me')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('重新登录')
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('')
    expect(wrapper.html()).not.toContain('do-not-render-me')
  })
})
