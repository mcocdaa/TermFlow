import { flushPromises, mount } from '@vue/test-utils'
import { ApiError } from '@termflow/client-core'
import { createClientUi, type ClientRuntime } from '@termflow/client-ui'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import NativeConnectView from './NativeConnectView.vue'

const mocks = vi.hoisted(() => ({
  authorizeNativeClient: vi.fn(),
  replaceServer: vi.fn(),
  serverConfig: { current: 'https://relay.example.com' },
}))

vi.mock('@xterm/xterm', () => ({ Terminal: class {} }))
vi.mock('../nativeAuth', () => ({ authorizeNativeClient: mocks.authorizeNativeClient }))
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
      { path: '/requested', component: { template: '<div>requested</div>' } },
    ],
  })
  await router.push('/connect?redirect=/requested')
  await router.isReady()
  const wrapper = mount(NativeConnectView, {
    global: { plugins: [router, createClientUi(runtimeWith(metadata))] },
  })
  return { wrapper, router }
}

beforeEach(() => {
  mocks.authorizeNativeClient.mockReset()
  mocks.authorizeNativeClient.mockResolvedValue({})
  mocks.replaceServer.mockReset()
  mocks.replaceServer.mockResolvedValue(undefined)
  mocks.serverConfig.current = 'https://relay.example.com'
})

describe('NativeConnectView', () => {
  it('presents the remote-control registration action in product language', async () => {
    const { wrapper } = await render()

    expect(wrapper.get('.eyebrow').text()).toBe('Connect to Server')
    expect(wrapper.get('h1').text()).toBe('连接到服务器')
    expect(wrapper.get('label[for="server-url"]').text()).toBe('服务器地址')
    expect(wrapper.get('button[type="submit"]').text()).toBe('申请注册远程控制')
    expect(wrapper.find('.auth-card > p:not(.eyebrow)').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/\bB\b|Web C/)
  })

  it('disables the action while the existing system-browser OAuth flow is pending', async () => {
    let finishAuthorization!: () => void
    mocks.authorizeNativeClient.mockReturnValue(new Promise<void>((resolve) => { finishAuthorization = resolve }))
    const { wrapper, router } = await render()

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const button = wrapper.get('button[type="submit"]')
    expect(button.attributes()).toHaveProperty('disabled')
    expect(button.text()).toBe('等待服务器管理员审批')
    expect(mocks.authorizeNativeClient).toHaveBeenCalledWith(
      'https://relay.example.com',
      'https://relay.example.com/api/v1/oauth/authorize',
      ['terminal:read'],
    )

    finishAuthorization()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/requested')
  })

  it.each([
    {
      name: 'offline or missing HTTP capability',
      failure: () => new ApiError('offline'),
      expected: '无法连接服务器。请检查服务器地址、网络连接和本机服务是否正在运行。',
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
  ])('shows an actionable safe message for $name', async ({ failure, expected }) => {
    mocks.authorizeNativeClient.mockRejectedValue(failure())
    const { wrapper } = await render()

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toBe(expected)
    expect(wrapper.text()).not.toMatch(/token|secret|credential|authorization_callback_invalid/i)
    expect(wrapper.get('button[type="submit"]').text()).toBe('申请注册远程控制')
  })
})
