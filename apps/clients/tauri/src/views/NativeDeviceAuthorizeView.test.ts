import { flushPromises, mount } from '@vue/test-utils'
import { createClientUi, type ClientRuntime } from '@termflow/client-ui'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import NativeDeviceAuthorizeView from './NativeDeviceAuthorizeView.vue'

const mocks = vi.hoisted(() => ({
  begin: vi.fn(), openUrl: vi.fn(), metadata: vi.fn(), replaceServer: vi.fn(),
}))

vi.mock('@xterm/xterm', () => ({ Terminal: class {} }))
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

async function render(client = runtime()) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/connect/device', component: NativeDeviceAuthorizeView }, { path: '/connect', component: { template: '<div />' } }, { path: '/', component: { template: '<div />' } }] })
  await router.push('/connect/device'); await router.isReady()
  const clientUi = createClientUi(client)
  const wrapper = mount(NativeDeviceAuthorizeView, { global: { plugins: [router, clientUi] } })
  return { wrapper, router, client, clientUi }
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
  it('generates a device code immediately and offers one return-to-connect action', async () => {
    const { wrapper } = await render()
    await flushPromises()

    expect(mocks.begin).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.device-start-form').exists()).toBe(false)
    expect(wrapper.find('.native-device-layout').exists()).toBe(true)
    expect(wrapper.get('[data-action="back-to-connect"]').text()).toBe('返回')
    expect(wrapper.findAll('[data-action="back-to-connect"]')).toHaveLength(1)
  })

  it('starts device authorization with a themed two-column QR layout and never opens a browser', async () => {
    const { wrapper } = await render()
    await flushPromises()
    expect(wrapper.text()).toContain('ABCD-EFGH')
    expect(wrapper.text()).toContain('https://relay.example.com')
    expect(wrapper.text()).not.toContain('验证地址')
    expect(wrapper.find('.native-device-layout').exists()).toBe(true)
    expect(wrapper.find('.native-device-qr').exists()).toBe(true)
    expect(wrapper.find('.native-device-details').exists()).toBe(true)
    expect(wrapper.find('.native-device-qr .themed-qr-code').exists()).toBe(true)
    expect(wrapper.find('.native-device-qr .device-code').exists()).toBe(true)
    expect(wrapper.find('.native-device-details .device-code').exists()).toBe(false)
    expect(wrapper.find('.native-device-details .native-device-server').text()).toContain('https://relay.example.com')
    expect(wrapper.find('.device-verification-url').exists()).toBe(false)
    expect(wrapper.get('[data-action="copy-device-code"]').attributes('aria-label')).toBe('复制设备码')
    expect(wrapper.find('[data-action="copy-device-code"] svg').exists()).toBe(true)
    expect(wrapper.findAll('.native-device-actions button')).toHaveLength(2)
    expect(wrapper.findAll('.native-device-actions [data-action="copy-device-code"]')).toHaveLength(0)
    expect(wrapper.get('[data-action="back-to-connect"]').text()).toBe('返回')
    expect(wrapper.get('[data-action="regenerate"]').text()).toBe('重新生成')
    expect(wrapper.text()).not.toContain('取消')
    expect(mocks.openUrl).not.toHaveBeenCalled()
  })

  it('copies the device code from the adjacent SVG action and reports success', async () => {
    const client = runtime()
    const { wrapper } = await render(client)
    await flushPromises()

    await wrapper.get('[data-action="copy-device-code"]').trigger('click')
    expect(client.clipboard.writeText).toHaveBeenCalledWith('ABCD-EFGH')
    expect(wrapper.find('[data-action="copy-device-code"]').attributes('title')).toBe('复制设备码')
  })

  it('cancels the active session and returns to connect', async () => {
    const { wrapper, router } = await render()
    await flushPromises()

    await wrapper.get('[data-action="back-to-connect"]').trigger('click')
    await flushPromises()
    expect(mocks.begin.mock.results[0]?.value).toBeDefined()
    expect(router.currentRoute.value.path).toBe('/connect')
    expect(wrapper.find('.native-device-layout').exists()).toBe(false)
  })

  it('regenerates the code while keeping the server address', async () => {
    mocks.begin
      .mockResolvedValueOnce({
        response: { device_code: 'first', user_code: 'FIRST-CODE', verification_uri: 'https://relay.example.com/device', verification_uri_complete: 'https://relay.example.com/device?code=FIRST-CODE', expires_in: 600, interval: 1 },
        session: { authorize: vi.fn().mockReturnValue(new Promise(() => undefined)), cancel: vi.fn() },
      })
      .mockResolvedValueOnce({
        response: { device_code: 'second', user_code: 'SECOND-CODE', verification_uri: 'https://relay.example.com/device', verification_uri_complete: 'https://relay.example.com/device?code=SECOND-CODE', expires_in: 600, interval: 1 },
        session: { authorize: vi.fn().mockReturnValue(new Promise(() => undefined)), cancel: vi.fn() },
      })
    const { wrapper } = await render()
    await flushPromises()
    await wrapper.get('[data-action="regenerate"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('SECOND-CODE')
    expect(mocks.replaceServer).toHaveBeenLastCalledWith('https://relay.example.com')
    expect(mocks.begin).toHaveBeenCalledTimes(2)
  })

  it('shows an explicit authorization phase when the server slows polling', async () => {
    const session = { authorize: vi.fn().mockReturnValue(new Promise(() => undefined)), cancel: vi.fn() }
    mocks.begin.mockResolvedValue({
      response: { device_code: 'secret-device-code', user_code: 'ABCD-EFGH', verification_uri: 'https://relay.example.com/device', verification_uri_complete: 'https://relay.example.com/device?code=ABCD-EFGH', expires_in: 600, interval: 1 },
      session,
    })
    const { wrapper } = await render()
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('等待浏览器确认')
  })

  it('shows the shared connection toast before navigating after approval', async () => {
    const session = { authorize: vi.fn().mockResolvedValue({}), cancel: vi.fn() }
    mocks.begin.mockResolvedValue({
      response: { device_code: 'secret-device-code', user_code: 'ABCD-EFGH', verification_uri: 'https://relay.example.com/device', verification_uri_complete: 'https://relay.example.com/device?code=ABCD-EFGH', expires_in: 600, interval: 1 },
      session,
    })
    const { clientUi } = await render()
    await flushPromises()

    expect(clientUi.toast.current.value).toEqual({ text: '已连接', tone: 'success' })
  })
})
