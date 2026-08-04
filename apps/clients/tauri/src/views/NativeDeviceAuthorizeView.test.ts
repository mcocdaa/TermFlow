import { flushPromises, mount } from '@vue/test-utils'
import { createClientUi, type ClientRuntime } from '@termflow/client-ui'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import NativeDeviceAuthorizeView from './NativeDeviceAuthorizeView.vue'

const mocks = vi.hoisted(() => ({
  begin: vi.fn(), openUrl: vi.fn(), metadata: vi.fn(), replaceServer: vi.fn(),
}))

vi.mock('../nativeAuth', () => ({ beginNativeDeviceAuthorization: mocks.begin }))
vi.mock('@tauri-apps/plugin-opener', () => ({ openUrl: mocks.openUrl }))
vi.mock('../serverConfig', () => ({
  canonicalIssuer: (value: string) => new URL(value).origin,
  serverConfig: { current: 'https://relay.example.com', replace: mocks.replaceServer },
}))
vi.mock('@tauri-apps/plugin-os', () => ({ platform: () => 'linux', arch: () => 'x64' }))

function runtime(): ClientRuntime {
  return {
    api: { oauth: { metadata: mocks.metadata } },
    clipboard: { writeText: vi.fn() },
  } as unknown as ClientRuntime
}

async function render() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/connect/device', component: NativeDeviceAuthorizeView }, { path: '/', component: { template: '<div />' } }] })
  await router.push('/connect/device'); await router.isReady()
  const wrapper = mount(NativeDeviceAuthorizeView, { global: { plugins: [router, createClientUi(runtime())] } })
  return { wrapper, router }
}

beforeEach(() => {
  mocks.begin.mockReset(); mocks.openUrl.mockReset(); mocks.replaceServer.mockReset();
  mocks.metadata.mockReset().mockResolvedValue({ issuer: 'https://relay.example.com', scopes_supported: ['terminal:read'] })
  mocks.replaceServer.mockResolvedValue(undefined)
  mocks.begin.mockResolvedValue({
    response: { device_code: 'secret-device-code', user_code: 'ABCD-EFGH', verification_uri: 'https://relay.example.com/device', verification_uri_complete: 'https://relay.example.com/device?code=ABCD-EFGH', expires_in: 600, interval: 1 },
    session: { authorize: vi.fn().mockResolvedValue({}), cancel: vi.fn() },
  })
})

describe('NativeDeviceAuthorizeView', () => {
  it('starts device authorization and never opens a browser', async () => {
    const { wrapper } = await render()
    await wrapper.get('button.primary-button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('ABCD-EFGH')
    expect(wrapper.text()).toContain('https://relay.example.com/device')
    expect(mocks.openUrl).not.toHaveBeenCalled()
  })
})
