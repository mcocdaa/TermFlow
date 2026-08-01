import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { describe, expect, it } from 'vitest'
import { createClientUi, useClientRuntime } from './runtime'
import type { ClientRuntime } from './runtime'

const fakeRuntime = (): ClientRuntime => ({
  api: {} as ClientRuntime['api'],
  createTerminal: () => ({ connect() {}, sendInput() {}, sendAction() {}, dispose() {} }),
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
})
