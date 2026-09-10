import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import SetupKeyPopover from './SetupKeyPopover.vue'

afterEach(() => {
  document.body.innerHTML = ''
})

describe('SetupKeyPopover', () => {
  it('opens a non-modal setup-key dialog and emits copy', async () => {
    const wrapper = mount(SetupKeyPopover, {
      attachTo: document.body,
      props: { setupKey: 'SETUPKEY', copied: false },
    })
    const trigger = wrapper.get('[data-action="toggle-setup-key"]')

    expect(trigger.attributes('aria-haspopup')).toBe('dialog')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)

    await trigger.trigger('click')
    const dialog = wrapper.get('[role="dialog"]')
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(dialog.attributes('aria-modal')).toBeUndefined()
    expect(dialog.get('[data-setup-key]').text()).toBe('SETUPKEY')

    await dialog.get('[data-action="copy-setup-key"]').trigger('click')
    expect(wrapper.emitted('copy')).toHaveLength(1)
    await wrapper.setProps({ copied: true })
    expect(dialog.text()).toContain('已复制')
    wrapper.unmount()
  })

  it('closes by trigger, Escape, and outside pointer and restores trigger focus', async () => {
    const wrapper = mount(SetupKeyPopover, {
      attachTo: document.body,
      props: { setupKey: 'SETUPKEY', copied: false },
    })
    const trigger = wrapper.get<HTMLButtonElement>('[data-action="toggle-setup-key"]')

    await trigger.trigger('click')
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)

    await trigger.trigger('click')
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)

    await trigger.trigger('click')
    await trigger.trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })
})
