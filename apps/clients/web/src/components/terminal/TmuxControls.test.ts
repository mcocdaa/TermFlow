import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ClosePaneDialog from './ClosePaneDialog.vue'
import PaneFocusMenu from './PaneFocusMenu.vue'
import TmuxActionMenu from './TmuxActionMenu.vue'

const bindings = {
  prefix: 'C-a',
  bindings: [
    { action: 'split_left_right' as const, key: 'C-a %', tooltip: '左右切分' },
    { action: 'split_top_bottom' as const, key: null, tooltip: '上下切分' },
    { action: 'new_window' as const, key: 'C-a c', tooltip: '新建窗口' },
    { action: 'select_left' as const, key: 'C-a h', tooltip: '向左' },
    { action: 'toggle_zoom' as const, key: 'C-a z', tooltip: '缩放' },
    { action: 'copy_mode' as const, key: 'C-a [', tooltip: '复制模式' },
  ],
}

describe('tmux controls', () => {
  it('opens desktop action and Pane focus menus only after a click', async () => {
    const wrapper = mount(TmuxActionMenu, { props: { bindings, activePaneId: '%3' } })
    const trigger = wrapper.get('[data-action="toggle-tmux-menu"]')
    await trigger.trigger('mouseenter')
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    expect(trigger.attributes('aria-expanded')).toBe('false')
    await trigger.trigger('click')
    expect(wrapper.get('[role="menu"]')).toBeTruthy()
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(trigger.find('svg').exists()).toBe(true)

    const panes = [{ pane_id: '%3', window_id: '@1', index: 0, title: 'shell', current_command: 'zsh', active: true, dead: false, left: 0, top: 0, width: 80, height: 24 }]
    const focus = mount(PaneFocusMenu, { props: { panes } })
    const focusTrigger = focus.get('[data-action="toggle-pane-focus-menu"]')
    await focusTrigger.trigger('mouseenter')
    expect(focus.find('[role="menu"]').exists()).toBe(false)
    await focusTrigger.trigger('click')
    expect(focus.get('[role="menu"]')).toBeTruthy()
    expect(focusTrigger.attributes('aria-expanded')).toBe('true')
    expect(focusTrigger.find('svg').exists()).toBe(true)
  })

  it('shows server-reported bindings, sends semantic actions, and keeps the mobile drawer hidden initially', async () => {
    const wrapper = mount(TmuxActionMenu, { props: { bindings, activePaneId: '%3' } })
    expect(wrapper.find('[data-mobile-drawer]').exists()).toBe(false)
    await wrapper.get('[data-action="toggle-tmux-menu"]').trigger('click')
    const split = wrapper.get('[data-action-id="split_left_right"]')
    expect(split.attributes('title')).toContain('C-a %')
    expect(wrapper.get('[data-action-id="split_top_bottom"]').attributes('title')).toContain('未绑定')
    await split.trigger('click')
    expect(wrapper.emitted('action')).toEqual([['split_left_right', '%3']])
    expect(wrapper.text()).not.toContain('Ctrl+B')
    await wrapper.get('[data-action="toggle-mobile-drawer"]').trigger('click')
    expect(wrapper.get('[data-mobile-drawer]')).toBeTruthy()
  })

  it('routes close Pane through confirmation instead of an immediate action', async () => {
    const wrapper = mount(TmuxActionMenu, { props: { bindings, activePaneId: '%3' } })
    await wrapper.get('[data-action="toggle-tmux-menu"]').trigger('click')
    await wrapper.get('[data-action-id="close_pane"]').trigger('click')
    expect(wrapper.emitted('request-close')?.[0]?.[0]).toBe('%3')
    expect(wrapper.emitted('request-close')?.[0]?.[1]).toBeInstanceOf(HTMLElement)
    expect(wrapper.emitted('action')).toBeUndefined()
  })

  it('disables server actions while the terminal stream is not ready', async () => {
    const wrapper = mount(TmuxActionMenu, { props: { bindings, activePaneId: '%3', disabled: true } })
    expect(wrapper.get('[data-action="toggle-tmux-menu"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-action="toggle-mobile-drawer"]').attributes('disabled')).toBeDefined()
  })

  it('closes the mobile action drawer with a downward swipe and restores its trigger focus', async () => {
    const wrapper = mount(TmuxActionMenu, { attachTo: document.body, props: { bindings, activePaneId: '%3' } })
    const trigger = wrapper.get('[data-action="toggle-mobile-drawer"]')
    await trigger.trigger('click')
    const drawer = wrapper.get('[data-mobile-drawer]')
    await drawer.trigger('pointerdown', { clientY: 100 })
    await drawer.trigger('pointerup', { clientY: 180 })
    expect(wrapper.find('[data-mobile-drawer]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })

  it('names the Pane and emits confirmed=true only after modal confirmation', async () => {
    const wrapper = mount(ClosePaneDialog, { attachTo: document.body, props: { paneId: '%3', paneName: '编辑器' } })
    expect(wrapper.text()).toContain('编辑器')
    await wrapper.get('[data-action="confirm-close-pane"]').trigger('click')
    expect(wrapper.emitted('confirm')).toEqual([[{ paneId: '%3', confirmed: true }]])
    wrapper.unmount()
  })
})
