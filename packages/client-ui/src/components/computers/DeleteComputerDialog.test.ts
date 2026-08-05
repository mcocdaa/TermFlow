import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import type { ComputerSummary } from '../../types'
import DeleteComputerDialog from './DeleteComputerDialog.vue'

const offlineComputer: ComputerSummary = {
  installation_id: 'computer-1',
  display_name: '维护工作站',
  hostname: 'devbox',
  platform: 'Linux x86_64',
  client_version: '1.4.2',
  online: false,
  registered_at: '2026-07-20T00:00:00Z',
  last_seen_at: '2026-08-01T01:00:00Z',
  terms: [],
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('DeleteComputerDialog', () => {
  it('renders an alert dialog, confirms by installation id, and restores focus', async () => {
    const invoker = document.createElement('button')
    document.body.append(invoker)
    invoker.focus()
    const wrapper = mount(DeleteComputerDialog, {
      attachTo: document.body,
      props: { computer: offlineComputer, pending: false, error: '' },
    })
    await wrapper.vm.$nextTick()

    const dialog = wrapper.get('[role="alertdialog"]')
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(dialog.attributes('aria-labelledby')).toBe('delete-computer-title')
    expect(dialog.attributes('aria-describedby')).toBe('delete-computer-description')
    expect(dialog.text()).toContain('永久删除远端注册和控制凭据')
    expect(dialog.text()).toContain(offlineComputer.display_name)
    expect(document.activeElement).toBe(wrapper.get('[data-action="cancel-delete-computer"]').element)

    await wrapper.get('[data-action="confirm-delete-computer"]').trigger('click')
    expect(wrapper.emitted('confirm')).toEqual([[offlineComputer.installation_id]])
    await wrapper.get('[data-action="cancel-delete-computer"]').trigger('click')
    expect(wrapper.emitted('cancel')).toEqual([[]])

    wrapper.unmount()
    expect(document.activeElement).toBe(invoker)
  })

  it('cancels with Escape and traps Tab focus between the actions', async () => {
    const wrapper = mount(DeleteComputerDialog, {
      attachTo: document.body,
      props: { computer: offlineComputer, pending: false, error: '' },
    })
    await wrapper.vm.$nextTick()

    const dialog = wrapper.get('[role="alertdialog"]')
    const cancel = wrapper.get('[data-action="cancel-delete-computer"]')
    const confirm = wrapper.get('[data-action="confirm-delete-computer"]')
    const confirmElement = confirm.element as HTMLButtonElement
    confirmElement.focus()
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    dialog.element.dispatchEvent(tab)
    expect(tab.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(cancel.element)

    await dialog.trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('cancel')).toEqual([[]])
  })

  it('disables both actions and cancellation while deletion is pending', async () => {
    const wrapper = mount(DeleteComputerDialog, {
      attachTo: document.body,
      props: { computer: offlineComputer, pending: true, error: '电脑仍在线，无法删除。' },
    })
    await wrapper.vm.$nextTick()

    const dialog = wrapper.get('[role="alertdialog"]')
    expect(dialog.get('[data-action="cancel-delete-computer"]').attributes('disabled')).toBeDefined()
    expect(dialog.get('[data-action="confirm-delete-computer"]').attributes('disabled')).toBeDefined()
    expect(dialog.get('[data-action="confirm-delete-computer"]').text()).toBe('正在删除…')
    expect(dialog.get('[role="alert"]').text()).toBe('电脑仍在线，无法删除。')

    await dialog.trigger('keydown', { key: 'Escape' })
    await wrapper.get('.dialog-backdrop').trigger('click')
    expect(wrapper.emitted('cancel')).toBeUndefined()
  })
})
