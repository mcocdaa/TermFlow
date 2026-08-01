import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ClosePaneDialog from './ClosePaneDialog.vue'
import TmuxActionMenu from './TmuxActionMenu.vue'

const bindings = {
  prefix: 'C-a',
  actions: { split_left_right: 'C-a %', split_top_bottom: null, new_window: 'C-a c', select_left: 'C-a h', toggle_zoom: 'C-a z', enter_copy_mode: 'C-a [' },
}

describe('tmux controls', () => {
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
    expect(wrapper.emitted('request-close')).toEqual([['%3']])
    expect(wrapper.emitted('action')).toBeUndefined()
  })

  it('names the Pane and emits confirmed=true only after modal confirmation', async () => {
    const wrapper = mount(ClosePaneDialog, { attachTo: document.body, props: { paneId: '%3', paneName: '编辑器' } })
    expect(wrapper.text()).toContain('编辑器')
    await wrapper.get('[data-action="confirm-close-pane"]').trigger('click')
    expect(wrapper.emitted('confirm')).toEqual([[{ paneId: '%3', confirmed: true }]])
    wrapper.unmount()
  })
})
