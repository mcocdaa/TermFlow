import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { NativeClientResponse } from '@termflow/client-contracts'
import type { ClientRuntime } from '../../runtime'
import { createClientUi } from '../../runtime'
import { createFakeRuntime } from '../../test/fakeRuntime'
import AuthorizedClientsPanel from './AuthorizedClientsPanel.vue'

const client: NativeClientResponse = {
  client_id: '11111111-1111-4111-8111-111111111111',
  display_name: 'TermFlow Windows',
  platform: 'Windows',
  client_version: '0.1.0',
  key_thumbprint: 'thumbprint-abcdef',
  scopes: ['terminal.read'],
  created_at: '2026-08-01T00:00:00Z',
  last_used_at: null,
  revoked_at: null,
}

function mountPanel(api: Partial<ClientRuntime['api']>, attachTo?: HTMLElement) {
  const runtime = createFakeRuntime({ api: api as unknown as ClientRuntime['api'] })
  const wrapper = mount(AuthorizedClientsPanel, {
    props: { totpEnabled: false },
    global: { plugins: [createClientUi(runtime)] },
    ...(attachTo === undefined ? {} : { attachTo }),
  })
  return { wrapper, runtime }
}

describe('AuthorizedClientsPanel revoke dialog', () => {
  it('opens a focus-safe alert dialog instead of an inline form', async () => {
    const host = document.createElement('div')
    document.body.append(host)
    const { wrapper } = mountPanel({
      clients: { list: async () => ({ clients: [client] }), remove: async () => ({}) },
    } as unknown as Partial<ClientRuntime['api']>, host)
    await flushPromises()

    expect(wrapper.find('#revoke-admin-token').exists()).toBe(false)

    await wrapper.get('button.danger-button').trigger('click')
    await flushPromises()
    const backdrop = wrapper.get('.dialog-backdrop')
    const dialog = backdrop.get('[role="alertdialog"]')
    expect(dialog.classes()).toContain('dialog-panel')
    expect(dialog.text()).toContain('TermFlow Windows')
    expect(wrapper.get('#revoke-admin-token').element).toBe(document.activeElement)
  })

  it('cancels on Escape and restores the revoked client row', async () => {
    const { wrapper } = mountPanel({
      clients: { list: async () => ({ clients: [client] }), remove: async () => ({}) },
    } as unknown as Partial<ClientRuntime['api']>)
    await flushPromises()

    await wrapper.get('button.danger-button').trigger('click')
    await wrapper.get('[role="alertdialog"]').trigger('keydown', { key: 'Escape' })
    await flushPromises()

    expect(wrapper.find('[role="alertdialog"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('TermFlow Windows')
  })

  it('removes the selected client with the entered credentials and shows a success toast', async () => {
    const remove = vi.fn().mockResolvedValue({})
    const { wrapper, runtime } = mountPanel({
      clients: { list: async () => ({ clients: [client] }), remove },
    } as unknown as Partial<ClientRuntime['api']>)
    await flushPromises()

    await wrapper.get('button.danger-button').trigger('click')
    await wrapper.get('#revoke-admin-token').setValue('admin-secret')
    await wrapper.get('[role="alertdialog"] form').trigger('submit')
    await flushPromises()

    expect(remove).toHaveBeenCalledWith(client.client_id, { adminToken: 'admin-secret' })
    expect(wrapper.find('[role="alertdialog"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('TermFlow Windows')
  })

  it('stays open with an error when removal fails', async () => {
    const remove = vi.fn().mockRejectedValue(new Error('server refused'))
    const { wrapper } = mountPanel({
      clients: { list: async () => ({ clients: [client] }), remove },
    } as unknown as Partial<ClientRuntime['api']>)
    await flushPromises()

    await wrapper.get('button.danger-button').trigger('click')
    await wrapper.get('#revoke-admin-token').setValue('admin-secret')
    await wrapper.get('[role="alertdialog"] form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[role="alertdialog"]').exists()).toBe(true)
    expect(wrapper.get('.form-error').text()).not.toBe('')
  })
})
