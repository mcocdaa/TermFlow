import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ClosePaneDialog from '../components/terminal/ClosePaneDialog.vue'

describe('accessibility contracts', () => {
  it('traps modal focus and restores it to the invoking control', async () => {
    const invoker = document.createElement('button')
    document.body.append(invoker)
    invoker.focus()
    const wrapper = mount(ClosePaneDialog, { attachTo: document.body, props: { paneId: '%1', paneName: 'Shell' } })
    await wrapper.vm.$nextTick()
    expect(document.activeElement?.textContent).toContain('取消')
    await wrapper.get('[data-action="confirm-close-pane"]').trigger('keydown', { key: 'Tab' })
    expect(document.activeElement?.textContent).toContain('取消')
    wrapper.unmount()
    expect(document.activeElement).toBe(invoker)
    invoker.remove()
  })
})
