import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import type { TermSummary } from '../../types'
import DeleteTermDialog from './DeleteTermDialog.vue'

const offlineTerm: TermSummary = {
  instance_id: 'term /2',
  name: '离线维护',
  online: false,
  window_count: 1,
  pane_count: 1,
  active_pane_count: 0,
  current_command: 'zsh',
  last_seen_at: '2026-07-31T02:00:00Z',
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('DeleteTermDialog', () => {
  it('explains the remote/local boundary, confirms by UUID, and restores focus', async () => {
    const invoker = document.createElement('button')
    document.body.append(invoker)
    invoker.focus()
    const wrapper = mount(DeleteTermDialog, {
      attachTo: document.body,
      props: { term: offlineTerm, pending: false, error: '' },
    })
    await wrapper.vm.$nextTick()

    const dialog = wrapper.get('[role="alertdialog"]')
    expect(dialog.attributes('aria-modal')).toBe('true')
    expect(dialog.text()).toContain('永久删除远端注册和控制凭据')
    expect(dialog.text()).toContain('不会删除本地 tmux Session')
    expect(dialog.text()).toContain(`termflow activate ${offlineTerm.instance_id}`)
    expect(document.activeElement).toBe(wrapper.get('[data-action="cancel-delete-term"]').element)

    await wrapper.get('[data-action="confirm-delete-term"]').trigger('click')
    expect(wrapper.emitted('confirm')).toEqual([[offlineTerm.instance_id]])
    wrapper.unmount()
    expect(document.activeElement).toBe(invoker)
  })

  it('traps focus and blocks cancellation while pending', async () => {
    const wrapper = mount(DeleteTermDialog, {
      attachTo: document.body,
      props: { term: offlineTerm, pending: true, error: 'Term 已重新上线，无法删除。' },
    })
    await wrapper.vm.$nextTick()

    const cancel = wrapper.get('[data-action="cancel-delete-term"]')
    const confirm = wrapper.get('[data-action="confirm-delete-term"]')
    expect(cancel.attributes('disabled')).toBeDefined()
    expect(confirm.attributes('disabled')).toBeDefined()
    expect(confirm.text()).toBe('正在删除…')
    expect(wrapper.get('[role="alert"]').text()).toBe('Term 已重新上线，无法删除。')

    await wrapper.get('[role="alertdialog"]').trigger('keydown', { key: 'Escape' })
    await wrapper.get('.dialog-backdrop').trigger('click')
    expect(wrapper.emitted('cancel')).toBeUndefined()
  })
})
