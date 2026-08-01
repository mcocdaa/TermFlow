import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import DisplayMenu from './DisplayMenu.vue'

describe('DisplayMenu', () => {
  it('uses one title-bar button with four vertical client display choices', async () => {
    const wrapper = mount(DisplayMenu, { props: { modelValue: 'font-100', open: false } })
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    await wrapper.get('[data-action="toggle-display-menu"]').trigger('click')
    expect(wrapper.emitted('update:open')).toEqual([[true]])
    await wrapper.setProps({ open: true })
    expect(wrapper.get('[data-action="toggle-display-menu"]').attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('[data-action="toggle-display-menu"]').find('svg').exists()).toBe(true)
    const choices = wrapper.findAll('[role="menuitemradio"]')
    expect(choices.map((item) => item.text())).toEqual(['50%', '75%', '100% 实际字号', '适应窗口'])
    expect(choices.every((item) => item.find('svg').exists())).toBe(true)
    await choices[1].trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['scale-75']])
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    await wrapper.setProps({ open: false })
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
  })

  it('supports Escape and arrow-key focus without emitting terminal controls', async () => {
    const wrapper = mount(DisplayMenu, { attachTo: document.body, props: { modelValue: 'fit', open: false } })
    await wrapper.get('button').trigger('click')
    await wrapper.setProps({ open: true })
    await wrapper.vm.$nextTick()
    expect(document.activeElement?.getAttribute('role')).toBe('menuitemradio')
    expect(document.activeElement?.textContent).toContain('适应窗口')
    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement?.getAttribute('role')).toBe('menuitemradio')
    expect(document.activeElement?.textContent).toContain('50%')
    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    await wrapper.setProps({ open: false })
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    expect(document.activeElement).toBe(wrapper.get('[data-action="toggle-display-menu"]').element)
    expect(wrapper.emitted()).not.toHaveProperty('terminal-control')
    wrapper.unmount()
  })
})
