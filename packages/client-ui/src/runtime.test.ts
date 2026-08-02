import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { describe, expect, it } from 'vitest'
import { createClientUi, useClientRuntime } from './runtime'
import type { ClientRuntime } from './runtime'
import { createThemeState } from './theme/theme'

const fakeRuntime = (): ClientRuntime => ({
  api: {} as ClientRuntime['api'],
  createTerminal: () => ({ async connect() {}, async sendInput() {}, async sendAction() {}, async dispose() {} }),
  clipboard: { writeText: async () => undefined },
  clock: {
    now: () => 0,
    setTimeout: () => 1,
    clearTimeout: () => undefined,
    setInterval: () => 1,
    clearInterval: () => undefined,
  },
  visibility: { isHidden: () => false, subscribe: () => () => undefined },
  canonicalServerUrl: 'https://control.example',
  platform: 'Linux x86_64',
})

describe('client UI runtime', () => {
  it('provides one frozen runtime to shared Vue code', () => {
    const runtime = fakeRuntime()
    const component = defineComponent({
      setup: () => ({ injected: useClientRuntime() }),
      template: '<div />',
    })
    const wrapper = mount(component, { global: { plugins: [createClientUi(runtime)] } })
    expect(wrapper.vm.injected).toBe(runtime)
    expect(Object.isFrozen(runtime)).toBe(true)
  })

  it('fails with a fixed startup error when the composition root omitted the runtime', () => {
    const component = defineComponent({ setup: () => useClientRuntime(), template: '<div />' })
    expect(() => mount(component)).toThrow('TermFlow client runtime is not installed.')
  })

  it('creates isolated session and theme state for each Vue application', async () => {
    const firstTheme = createThemeState({ load: () => 'cloud-cobalt', save: () => undefined }, { apply: () => undefined })
    const secondTheme = createThemeState({ load: () => 'midnight-indigo', save: () => undefined }, { apply: () => undefined })
    const firstRuntime: ClientRuntime = {
      ...fakeRuntime(),
      api: { sessions: { login: async () => ({ authenticated: true, expires_at: '2026-08-02T00:00:00Z' }) } } as unknown as ClientRuntime['api'],
    }
    const first = createClientUi(firstRuntime, { theme: firstTheme })
    const second = createClientUi(fakeRuntime(), { theme: secondTheme })

    expect(first.session).toBeDefined()
    expect(first.session).not.toBe(second.session)
    expect(first.theme).toBe(firstTheme)
    expect(second.theme).toBe(secondTheme)
    await first.session.loginWithToken('one-app-only')
    expect(first.session.sessionState.authenticated).toBe(true)
    expect(second.session.sessionState.authenticated).toBe(false)
  })
})
