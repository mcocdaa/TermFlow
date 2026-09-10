import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import NativeAuthorizeView from './NativeAuthorizeView.vue'

describe('NativeAuthorizeView', () => {
  it('shows device identity and requires fresh administrator/TOTP approval', async () => {
    const navigate = vi.fn()
    const decideAuthorization = vi.fn().mockResolvedValue({
      status: 'approved', callback_uri: 'termflow://auth/callback?state=state-1&transaction_id=11111111-1111-4111-8111-111111111111',
    })
    const runtime = createFakeRuntime({
      authorizationCompletion: { navigate },
      api: { oauth: {
        authorizationPreview: vi.fn().mockResolvedValue({
          transaction_id: '11111111-1111-4111-8111-111111111111', issuer: 'https://b.example', client_name: 'TermFlow Desktop',
          platform: 'Linux', client_version: '0.1.0', key_fingerprint: 'fingerprint', scopes: ['terminal.read'],
          redirect_uri: 'termflow://auth/callback', totp_required: true, expires_at: '2026-08-02T12:00:00Z',
        }),
        decideAuthorization,
      } } as unknown as ClientRuntime['api'],
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/authorize', component: NativeAuthorizeView }] })
    await router.push('/authorize?transaction_id=11111111-1111-4111-8111-111111111111'); await router.isReady()
    const wrapper = mount(NativeAuthorizeView, { global: { plugins: [router, createClientUi(runtime)] } })
    await flushPromises()

    expect(wrapper.text()).toContain('TermFlow Desktop')
    expect(wrapper.text()).toContain('fingerprint')
    expect(wrapper.text()).toContain('terminal.read')
    await wrapper.get('#authorize-admin-token').setValue('bootstrap-secret')
    await wrapper.get('#authorize-totp').setValue('123456')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(decideAuthorization).toHaveBeenCalledWith(expect.objectContaining({ adminToken: 'bootstrap-secret', totpCode: '123456', decision: 'allow' }))
    expect(navigate).toHaveBeenCalledOnce()
    expect(navigate.mock.calls[0]?.[0]).not.toMatch(/[?&](code|access_token|refresh_token)=/)
    expect(wrapper.html()).not.toContain('bootstrap-secret')
  })
})
