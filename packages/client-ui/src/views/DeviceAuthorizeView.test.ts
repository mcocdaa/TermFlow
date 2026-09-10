import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@termflow/client-core'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import DeviceAuthorizeView from './DeviceAuthorizeView.vue'

const preview = {
  transaction_id: '11111111-1111-4111-8111-111111111111', issuer: 'https://b.example',
  client_name: 'TermFlow Phone', platform: 'Android', client_version: '1.2.3',
  key_fingerprint: 'fingerprint', scopes: ['terminal.read'], redirect_uri: 'termflow://auth/callback',
  totp_required: true, expires_at: '2026-08-04T12:00:00Z',
}

async function mountDevice(runtime: ClientRuntime, path = '/device?code=ABCD-EFGH') {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/device', component: DeviceAuthorizeView }, { path: '/login', component: { template: '<div>login</div>' } }] })
  await router.push(path); await router.isReady()
  const wrapper = mount(DeviceAuthorizeView, { global: { plugins: [router, createClientUi(runtime)] } })
  await flushPromises()
  return { wrapper, router }
}

describe('DeviceAuthorizeView', () => {
  it('has a single return-to-login entry before device lookup', async () => {
    const runtime = createFakeRuntime()
    const { router, wrapper } = await mountDevice(runtime, '/device')

    const back = wrapper.get('[data-action="back-to-login"]')
    expect(back.element.tagName).toBe('A')
    expect(back.attributes('href')).toBe('/login')
    expect(wrapper.findAll('[data-action="back-to-login"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-action="cancel-device"]')).toHaveLength(0)

    await back.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('loads a user code, displays device metadata, and approves with the browser session/TOTP', async () => {
    const decideAuthorization = vi.fn().mockResolvedValue({ status: 'approved', callback_uri: 'termflow://auth/callback?state=s&transaction_id=11111111-1111-4111-8111-111111111111' })
    const runtime = createFakeRuntime({ api: { oauth: {
      deviceAuthorizationPreview: vi.fn().mockResolvedValue(preview), decideAuthorization,
    } } as unknown as ClientRuntime['api'] })
    const { wrapper } = await mountDevice(runtime)

    expect(wrapper.text()).toContain('TermFlow Phone')
    expect(wrapper.text()).toContain('Android')
    expect(wrapper.text()).toContain('terminal.read')
    expect(wrapper.text()).toContain('ABCD-EFGH')
    expect(wrapper.find('.device-authorize-layout').exists()).toBe(true)
    expect(wrapper.find('.device-authorize-qr').exists()).toBe(true)
    expect(wrapper.find('.device-authorize-status').exists()).toBe(true)
    await wrapper.get('#device-authorize-totp').setValue('123456')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(decideAuthorization).toHaveBeenCalledWith({ transactionId: preview.transaction_id, decision: 'allow', totpCode: '123456' })
    expect(wrapper.html()).not.toContain('123456')
    expect(wrapper.text()).toContain('授权成功')
  })

  it('redirects an unauthenticated browser to login while preserving the code', async () => {
    const runtime = createFakeRuntime({ api: { oauth: {
      deviceAuthorizationPreview: vi.fn().mockRejectedValue(new ApiError('authentication', { status: 401 })),
      decideAuthorization: vi.fn(),
    } } as unknown as ClientRuntime['api'] })
    const { router, wrapper } = await mountDevice(runtime)
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query.redirect).toBe('/device?code=ABCD-EFGH')
  })

  it('accepts a manually entered code when no query code is provided', async () => {
    const deviceAuthorizationPreview = vi.fn().mockResolvedValue(preview)
    const runtime = createFakeRuntime({ api: { oauth: {
      deviceAuthorizationPreview, decideAuthorization: vi.fn(),
    } } as unknown as ClientRuntime['api'] })
    const { wrapper } = await mountDevice(runtime, '/device')
    expect(wrapper.find('#device-user-code').element).toBeTruthy()
    await wrapper.get('#device-user-code').setValue('abcd-efgh')
    await wrapper.get('[data-action="lookup-device"]').trigger('submit')
    await flushPromises()
    expect(deviceAuthorizationPreview).toHaveBeenCalledWith('ABCD-EFGH')
  })
})
