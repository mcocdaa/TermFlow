import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ClientRuntime } from '../runtime'
import { createClientUi } from '../runtime'
import { createFakeRuntime } from '../test/fakeRuntime'
import SettingsView from './SettingsView.vue'

const { toDataURL } = vi.hoisted(() => ({ toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,public-qr') }))
vi.mock('qrcode', () => ({ default: { toDataURL } }))

async function mountSettings(runtime: ClientRuntime) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: SettingsView }] })
  await router.push('/settings'); await router.isReady()
  const wrapper = mount(SettingsView, { global: { plugins: [router, createClientUi(runtime)] } })
  await flushPromises()
  return wrapper
}

beforeEach(() => toDataURL.mockClear())

describe('SettingsView', () => {
  it('shows the metadata issuer read-only and a credential-free local QR', async () => {
    const runtime = createFakeRuntime({
      canonicalServerUrl: 'https://browser-origin.invalid',
      api: {
        oauth: { metadata: vi.fn().mockResolvedValue({ issuer: 'https://configured-b.example' }) },
        security: { totpStatus: vi.fn().mockResolvedValue({ enabled: false, available: false }) },
        clients: { list: vi.fn().mockResolvedValue({ clients: [] }) },
      } as unknown as ClientRuntime['api'],
    })
    const wrapper = await mountSettings(runtime)

    expect(wrapper.get('[data-server-issuer]').text()).toBe('https://configured-b.example')
    expect(wrapper.find('input[value="https://configured-b.example"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('TOTP 加密主密钥')
    const qrValue = String(toDataURL.mock.calls.at(-1)?.[0])
    expect(qrValue).toContain('https://configured-b.example')
    expect(qrValue).not.toMatch(/token|secret|access_token|refresh_token/i)
  })

  it('hides Web-only security management for native capabilities', async () => {
    const runtime = createFakeRuntime({
      capabilities: { manageSecurity: false, manageAuthorizedClients: false },
      api: { oauth: { metadata: vi.fn().mockResolvedValue({ issuer: 'https://b.example' }) } } as unknown as ClientRuntime['api'],
    })
    const wrapper = await mountSettings(runtime)

    expect(wrapper.text()).toContain('安全设置只能从已认证 Web C 管理')
    expect(wrapper.find('#totp-admin-token').exists()).toBe(false)
  })
})
