import { flushPromises, mount } from '@vue/test-utils'
import { ApiError } from '@termflow/client-core'
import { createClientUi, type ClientRuntime } from '@termflow/client-ui'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import NativeConnectView from './NativeConnectView.vue'

const mocks = vi.hoisted(() => ({
  authorizeNativeClient: vi.fn(),
  prepareNativeServer: vi.fn(),
  verifyNativeConnection: vi.fn(),
  replaceServer: vi.fn(),
  serverConfig: { current: 'https://relay.example.com' },
}))

vi.mock('@xterm/xterm', () => ({ Terminal: class {} }))
vi.mock('../nativeAuth', () => ({
  authorizeNativeClient: mocks.authorizeNativeClient,
  verifyNativeConnection: mocks.verifyNativeConnection,
}))
vi.mock('../serverPreparation', () => ({
  prepareNativeServer: mocks.prepareNativeServer,
}))
vi.mock('../serverConfig', () => ({
  canonicalIssuer: (value: string) => new URL(value).origin,
  canonicalAuthorizeEndpoint: (_issuer: string, value: string) => value,
  serverConfig: {
    get current() { return mocks.serverConfig.current },
    replace: mocks.replaceServer,
  },
}))

function runtimeWith(metadata: ClientRuntime['api']['oauth']['metadata']): ClientRuntime {
  return {
    api: { oauth: { metadata } },
    clock: { now: () => 0, setTimeout: () => 1, clearTimeout: () => undefined, setInterval: () => 1, clearInterval: () => undefined },
  } as unknown as ClientRuntime
}

async function render(metadata = vi.fn().mockResolvedValue({
  issuer: 'https://relay.example.com',
  authorization_endpoint: 'https://relay.example.com/api/v1/oauth/authorize',
  scopes_supported: ['terminal:read'],
})) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/connect', component: NativeConnectView },
      { path: '/connect/device', component: { template: '<div>device</div>' } },
      { path: '/requested', component: { template: '<div>requested</div>' } },
    ],
  })
  await router.push('/connect?redirect=/requested')
  await router.isReady()
  const clientUi = createClientUi(runtimeWith(metadata))
  const wrapper = mount(NativeConnectView, {
    global: { plugins: [router, clientUi] },
  })
  return { wrapper, router, clientUi }
}

beforeEach(() => {
  mocks.authorizeNativeClient.mockReset()
  mocks.authorizeNativeClient.mockResolvedValue({})
  mocks.prepareNativeServer.mockReset()
  mocks.prepareNativeServer.mockImplementation(async (input: string) => {
    const canonical = new URL(input).origin
    return {
      issuer: canonical,
      metadata: {
        issuer: canonical,
        authorization_endpoint: `${canonical}/api/v1/oauth/authorize`,
        scopes_supported: ['terminal:read'],
      },
    }
  })
  mocks.replaceServer.mockReset()
  mocks.replaceServer.mockResolvedValue(undefined)
  mocks.serverConfig.current = 'https://relay.example.com'
  mocks.verifyNativeConnection.mockReset()
  mocks.verifyNativeConnection.mockResolvedValue(undefined)
})

describe('NativeConnectView', () => {
  it('presents the remote-control registration action in product language', async () => {
    const { wrapper } = await render()

    expect(wrapper.get('.eyebrow').text()).toBe('Connect to Server')
    expect(wrapper.get('h1').text()).toBe('连接到服务器')
    expect(wrapper.get('label[for="server-url"]').text()).toBe('服务器地址')
    expect(wrapper.get('[data-action="browser-login"]').text()).toContain('本机浏览器登录')
    expect(wrapper.get('[data-action="device-authorize"]').text()).toContain('其他设备授权')
    expect(wrapper.find('.native-auth-options').classes()).toContain('native-auth-options')
    expect(wrapper.get('[data-action="browser-login"]').attributes('title')).toContain('本机系统浏览器')
    expect(wrapper.get('[data-action="device-authorize"]').attributes('title')).toContain('不会打开本机浏览器')
    expect(wrapper.findAll('.native-auth-options > button')).toHaveLength(2)
    expect(wrapper.find('.auth-card > p:not(.eyebrow)').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/\bB\b|Web C/)
  })

  it('prepares the typed private issuer before entering device authorization', async () => {
    const { wrapper, router } = await render()
    await wrapper.get('#server-url').setValue('https://termflow.mcocdaa-newapi.xin/')
    await wrapper.get('[data-action="device-authorize"]').trigger('click')
    await flushPromises()

    expect(mocks.prepareNativeServer).toHaveBeenCalledWith(
      'https://termflow.mcocdaa-newapi.xin/',
      expect.any(Function),
    )
    expect(router.currentRoute.value.path).toBe('/connect/device')
    expect(mocks.authorizeNativeClient).not.toHaveBeenCalled()
  })

  it('stays on connect when device authorization cannot prepare the server', async () => {
    mocks.prepareNativeServer.mockRejectedValueOnce(new ApiError('offline'))
    const { wrapper, router } = await render()

    await wrapper.get('[data-action="device-authorize"]').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/connect')
    expect(wrapper.get('[role="alert"]').text()).toBe(
      '无法连接服务器。请检查服务器地址、网络连接和本机服务是否正在运行。',
    )
    expect(mocks.authorizeNativeClient).not.toHaveBeenCalled()
  })

  it('disables the action while the existing system-browser OAuth flow is pending', async () => {
    let finishAuthorization!: () => void
    mocks.authorizeNativeClient.mockReturnValue(new Promise<void>((resolve) => { finishAuthorization = resolve }))
    const { wrapper, router, clientUi } = await render()

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const button = wrapper.get('[data-action="browser-login"]')
    const deviceButton = wrapper.get('[data-action="device-authorize"]')
    expect(button.attributes()).toHaveProperty('disabled')
    expect(deviceButton.attributes()).toHaveProperty('disabled')
    expect(button.text()).toContain('等待浏览器审批')
    expect(mocks.authorizeNativeClient).toHaveBeenCalledWith(
      'https://relay.example.com',
      'https://relay.example.com/api/v1/oauth/authorize',
      ['terminal:read'],
    )

    finishAuthorization()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/requested')
    expect(clientUi.toast.current.value).toEqual({ text: '已连接', tone: 'success' })
  })

  it.each([
    {
      name: 'offline server',
      failure: () => new ApiError('offline'),
      expected: '无法连接服务器。请检查服务器地址、网络连接和本机服务是否正在运行。',
    },
    {
      name: 'invalid HTTP capability',
      failure: () => new ApiError('http_capability_denied'),
      expected: '客户端网络权限配置无效。请升级或重新安装 TermFlow。',
    },
    {
      name: 'user cancellation',
      failure: () => new Error('authorization_cancelled'),
      expected: '注册申请已取消。请重新申请，并在系统浏览器中完成审批。',
    },
    {
      name: 'invalid deep-link callback',
      failure: () => new Error('authorization_callback_invalid'),
      expected: '未收到有效的 TermFlow 回调。请确认系统允许 termflow:// 链接打开本应用，然后重新申请。',
    },
    {
      name: 'timed-out deep-link callback',
      failure: () => new Error('authorization_callback_timeout'),
      expected: '未收到有效的 TermFlow 回调。请确认系统允许 termflow:// 链接打开本应用，然后重新申请。',
    },
  ])('shows an actionable safe message for $name', async ({ failure, expected }) => {
    mocks.authorizeNativeClient.mockRejectedValue(failure())
    const { wrapper } = await render()

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toBe(expected)
    expect(wrapper.text()).not.toMatch(/token|secret|credential|authorization_callback_invalid/i)
    expect(wrapper.get('[data-action="browser-login"]').text()).toContain('本机浏览器登录')
  })

  it('verifies the stored credential before leaving the connection page', async () => {
    const { wrapper, router } = await render()

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mocks.verifyNativeConnection).toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/requested')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('stays on the connection page when the protected request is denied', async () => {
    mocks.verifyNativeConnection.mockRejectedValue(new ApiError('http_capability_denied'))
    const { wrapper, router } = await render()

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(wrapper.get('[role="alert"]').text()).toBe('客户端网络权限配置无效。请升级或重新安装 TermFlow。')
    expect(router.currentRoute.value.path).toBe('/connect')
    expect(wrapper.get('[data-action="browser-login"]').text()).toContain('本机浏览器登录')
  })
})
