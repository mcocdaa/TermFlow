import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it } from 'vitest'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import QrCodeDialog from './QrCodeDialog.vue'

describe('QrCodeDialog', () => {
  it('is modal, closes by Escape/backdrop, and restores trigger focus', async () => {
    const trigger = document.createElement('button')
    document.body.append(trigger)
    trigger.focus()
    const wrapper = mount(QrCodeDialog, {
      attachTo: document.body,
      props: {
        open: true,
        title: '服务网址二维码',
        value: 'termflow://relay',
        description: '公开连接信息',
        returnFocus: trigger,
      },
      global: { plugins: [createClientUi(createFakeRuntime())] },
    })
    await nextTick()

    const dialog = wrapper.get('[role="dialog"]')
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(document.activeElement).toBe(wrapper.get('[data-action="close-qr"]').element)
    await dialog.trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(1)
    await nextTick()
    expect(document.activeElement).toBe(trigger)

    await wrapper.setProps({ open: false })
    await wrapper.setProps({ open: true })
    await wrapper.get('.qr-dialog-backdrop').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(2)
    wrapper.unmount()
    trigger.remove()
  })
})
