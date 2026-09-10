import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { App, createClientUi, useTerminalSession } from '@termflow/client-ui'
import { createFakeRuntime } from './test/fakeRuntime'
import { createAppRouter } from './router'

describe('browser router composition', () => {
  it('preserves the terminal session instance across agent query mutation', async () => {
    const scroll = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    const fake = createFakeRuntime()
    const createTerminal = vi.fn(fake.createTerminal)
    const runtime = { ...fake, createTerminal }
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }) })
    await router.push('/terms/term-7')
    const Canvas = defineComponent({ props: ['termId'], setup(props) { useTerminalSession(props.termId, ref(null)); return { resetViewport() {}, restoreViewport() {}, captureViewport() {} } }, template: '<div />' })
    const w = mount(App, { global: { plugins: [router, createClientUi(runtime)], stubs: { TerminalCanvas: Canvas } } })
    await flushPromises()
    await router.replace('/terms/term-7?agent=11111111-1111-4111-8111-111111111111')
    await flushPromises()
    await router.replace('/terms/term-7?agent=22222222-2222-4222-8222-222222222222')
    await flushPromises()
    expect(createTerminal).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.params.termId).toBe('term-7')
    w.unmount()
    scroll.mockRestore()
  })
  it('discards non-UUID agent query values before rendering a terminal', async () => {
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }) })
    await router.push('/terms/term-7?agent=PRIVATE_MESSAGE')
    expect(router.currentRoute.value.query.agent).toBeUndefined()
  })
  it('allows only a canonical agent UUID on terminal routes', async () => {
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: true }) })
    await router.push('/terms/term-7?keep=yes&agent=11111111-1111-4111-8111-111111111111&secret=drop-me')
    expect(router.currentRoute.value.query).toEqual({ agent: '11111111-1111-4111-8111-111111111111' })
  })
  it('redirects protected pages to login and retains the intended route', async () => {
    const router = createAppRouter({ sessionStatus: async () => ({ authenticated: false }) })

    await router.push('/terms/term-7')
    await router.isReady()

    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=/terms/term-7')
  })
})
