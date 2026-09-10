import { mount } from '@vue/test-utils'
import { defineComponent, h, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useTerminalPageLock } from './useTerminalPageLock'

afterEach(() => {
  document.documentElement.className = ''
  document.body.className = ''
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('terminal page lock', () => {
  it('locks html body and app only while active, then restores the original scroll', async () => {
    const root = document.createElement('div')
    root.id = 'app'
    document.body.append(root)
    Object.defineProperties(window, {
      scrollX: { value: 12, configurable: true },
      scrollY: { value: 34, configurable: true },
    })
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    const active = ref(false)
    const wrapper = mount(defineComponent({
      setup() {
        useTerminalPageLock(active)
        return () => h('div')
      },
    }), { attachTo: root })

    active.value = true
    await nextTick()
    for (const element of [document.documentElement, document.body, root]) {
      expect(element.classList.contains('termflow-terminal-route')).toBe(true)
    }

    active.value = false
    await nextTick()
    for (const element of [document.documentElement, document.body, root]) {
      expect(element.classList.contains('termflow-terminal-route')).toBe(false)
    }
    expect(scrollTo).toHaveBeenCalledWith(12, 34)
    wrapper.unmount()
  })

  it('removes root classes and restores scroll when its owner unmounts while active', async () => {
    const root = document.createElement('div')
    root.id = 'app'
    document.body.append(root)
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    const wrapper = mount(defineComponent({
      setup() {
        useTerminalPageLock(ref(true))
        return () => h('div')
      },
    }), { attachTo: root })
    await nextTick()

    wrapper.unmount()
    expect([document.documentElement, document.body, root].every((element) => !element.classList.contains('termflow-terminal-route'))).toBe(true)
    expect(scrollTo).toHaveBeenCalledOnce()
  })
})
