import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ClosePaneDialog from './ClosePaneDialog.vue'
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
  it('opens the tmux action menu only after a click', async () => {
    const wrapper = mount(TmuxActionMenu, { props: { bindings, activePaneId: '%3', open: false } })
    const trigger = wrapper.get('[data-action="toggle-tmux-menu"]')
    await trigger.trigger('mouseenter')
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    expect(trigger.attributes('aria-expanded')).toBe('false')
    await trigger.trigger('click')
    expect(wrapper.emitted('update:open')).toEqual([[true]])
    await wrapper.setProps({ open: true })
    expect(wrapper.get('[role="menu"]')).toBeTruthy()
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(trigger.find('svg').exists()).toBe(true)
  })

  it('shows server-reported bindings and sends semantic actions from the only tmux menu', async () => {
    const wrapper = mount(TmuxActionMenu, { props: { bindings, activePaneId: '%3', open: false } })
    expect(wrapper.find('[data-action="toggle-mobile-drawer"]').exists()).toBe(false)
    expect(wrapper.find('[data-mobile-drawer]').exists()).toBe(false)
    const trigger = wrapper.get('[data-action="toggle-tmux-menu"]')
    expect(trigger.attributes('aria-label')).toBe('tmux 操作')
    expect(trigger.get('.control-label').text()).toBe('tmux 操作')
    await trigger.trigger('click')
    await wrapper.setProps({ open: true })
    const split = wrapper.get('[data-action-id="split_left_right"]')
    expect(split.get('.action-label').text()).toBe('左右切分 Pane')
    expect(split.find('small').exists()).toBe(false)
    const splitTooltip = wrapper.get(`#${split.attributes('aria-describedby')}`)
    expect(splitTooltip.attributes('role')).toBe('tooltip')
    expect(splitTooltip.get('code').text()).toContain('Ctrl + a')
    const unbound = wrapper.get('[data-action-id="split_top_bottom"]')
    expect(wrapper.get(`#${unbound.attributes('aria-describedby')}`).text()).toContain('未绑定')
    await split.trigger('click')
    expect(wrapper.emitted('action')).toEqual([['split_left_right', '%3']])
    expect(wrapper.emitted('update:open')?.at(-1)).toEqual([false])
    expect(wrapper.text()).not.toContain('Ctrl+B')
  })

  it('routes close Pane through confirmation instead of an immediate action', async () => {
    const wrapper = mount(TmuxActionMenu, { props: { bindings, activePaneId: '%3', open: false } })
    await wrapper.get('[data-action="toggle-tmux-menu"]').trigger('click')
    await wrapper.setProps({ open: true })
    await wrapper.get('[data-action-id="close_pane"]').trigger('click')
    expect(wrapper.emitted('request-close')?.[0]?.[0]).toBe('%3')
    expect(wrapper.emitted('request-close')?.[0]?.[1]).toBeInstanceOf(HTMLElement)
    expect(wrapper.emitted('action')).toBeUndefined()
  })

  it('disables server actions while the terminal stream is not ready', async () => {
    const wrapper = mount(TmuxActionMenu, { props: { bindings, activePaneId: '%3', disabled: true } })
    expect(wrapper.get('[data-action="toggle-tmux-menu"]').attributes('disabled')).toBeDefined()
  })

  it('names the Pane and emits confirmed=true only after modal confirmation', async () => {
    const wrapper = mount(ClosePaneDialog, { attachTo: document.body, props: { paneId: '%3', paneName: '编辑器' } })
    expect(wrapper.text()).toContain('编辑器')
    await wrapper.get('[data-action="confirm-close-pane"]').trigger('click')
    expect(wrapper.emitted('confirm')).toEqual([[{ paneId: '%3', confirmed: true }]])
    wrapper.unmount()
  })
})
