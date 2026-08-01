import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { createAppRouter } from '../router'

describe('browser login privacy', () => {
  it('posts the admin token once, clears it, and never exposes it to browser persistence or navigation', async () => {
    const secret = 'tf_admin_super_secret_93'
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ authenticated: true }), {
      status: 201,
      headers: { 'content-type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }), history: createMemoryHistory() })
    await router.push('/login?redirect=/computers')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router] } })

    await wrapper.get('input[type="password"]').setValue(secret)
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/session')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin', method: 'POST' })
    expect(router.currentRoute.value.fullPath).toBe('/computers')
    expect(wrapper.html()).not.toContain(secret)
    expect(localStorage.getItem(secret)).toBeNull()
    expect(sessionStorage.getItem(secret)).toBeNull()
    expect(window.location.href).not.toContain(secret)
    expect(log).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    expect(wrapper.emitted()).not.toContain(secret)
  })

  it('shows a safe authentication error and clears the submitted token', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })))
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: false }), history: createMemoryHistory() })
    await router.push('/login')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [router] } })

    await wrapper.get('input[type="password"]').setValue('do-not-render-me')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('重新登录')
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('')
    expect(wrapper.html()).not.toContain('do-not-render-me')
  })
})
