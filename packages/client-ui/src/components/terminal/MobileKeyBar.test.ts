import { mount } from '@vue/test-utils'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import { describe, expect, it, vi } from 'vitest'
import { MobileModifierController } from '@termflow/client-core'
import MobileKeyBar from './MobileKeyBar.vue'

const mountKeyBar = (props: InstanceType<typeof MobileKeyBar>['$props']) => mount(MobileKeyBar, {
  props,
  global: { plugins: [createClientUi(createFakeRuntime())] },
})

describe('MobileKeyBar', () => {
  it('separates viewport coverage from horizontal key scrolling', () => {
    const wrapper = mountKeyBar({ prefix: 'C-a', controller: new MobileModifierController() })
    const shell = wrapper.get('.mobile-keybar-shell')
    const scroller = shell.get('.mobile-keybar')
    expect(shell.attributes('aria-hidden')).toBeUndefined()
    expect(scroller.attributes('aria-label')).toBe('移动端修饰键')
    expect(scroller.findAll('button')).toHaveLength(18)
  })

  it('keeps the platform-neutral modifier state reactive in Vue', async () => {
    const wrapper = mountKeyBar({ prefix: 'C-a', controller: new MobileModifierController() })
    const ctrl = wrapper.findAll('button')[0]
    expect(ctrl!.attributes('aria-pressed')).toBe('false')
    await ctrl!.trigger('click')
    expect(ctrl!.attributes('aria-pressed')).toBe('true')
    await ctrl!.trigger('click')
    expect(ctrl!.find('.locked-indicator').exists()).toBe(true)
  })

  it('disables every terminal-input control while the stream is not ready', async () => {
    const wrapper = mountKeyBar({ prefix: 'C-a', controller: new MobileModifierController(), disabled: true })
    expect(wrapper.findAll('button')).toHaveLength(18)
    expect(wrapper.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    await wrapper.findAll('button')[3]!.trigger('click')
    expect(wrapper.emitted('input')).toBeUndefined()
  })

  it('emits proper ANSI escape codes for arrows, interrupt (^C), and enter', async () => {
    const wrapper = mountKeyBar({ prefix: 'C-a', controller: new MobileModifierController() })
    const buttons = wrapper.findAll('button')

    // Up arrow button (index 6)
    const upBtn = buttons.find((b) => b.text() === '↑')
    expect(upBtn).toBeDefined()
    await upBtn!.trigger('click')
    expect(wrapper.emitted('input')?.[0]?.[0]).toEqual(new TextEncoder().encode('\u001b[A'))

    // Interrupt ^C button
    const intBtn = buttons.find((b) => b.text() === '^C')
    expect(intBtn).toBeDefined()
    await intBtn!.trigger('click')
    expect(wrapper.emitted('input')?.[1]?.[0]).toEqual(Uint8Array.of(3))

    // Enter button
    const enterBtn = buttons.find((b) => b.text() === '↵')
    expect(enterBtn).toBeDefined()
    await enterBtn!.trigger('click')
    expect(wrapper.emitted('input')?.[2]?.[0]).toEqual(new TextEncoder().encode('\r'))
  })

  it('contains a vertical pointer drag without forwarding terminal input or a document gesture', async () => {
    const documentMove = vi.fn()
    document.addEventListener('pointermove', documentMove)
    const wrapper = mount(MobileKeyBar, {
      attachTo: document.body,
      props: { prefix: 'C-a', controller: new MobileModifierController() },
      global: { plugins: [createClientUi(createFakeRuntime())] },
    })

    await wrapper.get('.mobile-keybar').trigger('pointermove', { pointerType: 'touch', clientX: 20, clientY: 80 })
    expect(documentMove).not.toHaveBeenCalled()
    expect(wrapper.emitted('input')).toBeUndefined()

    document.removeEventListener('pointermove', documentMove)
    wrapper.unmount()
  })
})
