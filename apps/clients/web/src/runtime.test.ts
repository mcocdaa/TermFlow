import type { ClientRuntime } from '@termflow/client-ui'
import { describe, expect, it, vi } from 'vitest'
import { browserRuntime, createBrowserRuntime } from './runtime'

describe('browser runtime composition', () => {
  it('assembles the injected browser ports as one ClientRuntime', () => {
    const dependencies: ClientRuntime = {
      api: {} as ClientRuntime['api'],
      createTerminal: () => ({ async connect() {}, async sendInput() {}, async sendAction() {}, async dispose() {} }),
      clipboard: { writeText: async () => undefined },
      clock: { now: () => 1, setTimeout: () => 2, clearTimeout: () => undefined, setInterval: () => 3, clearInterval: () => undefined },
      visibility: { isHidden: () => false, subscribe: () => () => undefined },
      capabilities: { manageSecurity: true, manageAuthorizedClients: true },
      authorizationCompletion: { navigate: () => undefined },
      canonicalServerUrl: 'https://b.termflow.test',
      platform: 'MacIntel',
    }

    const runtime = createBrowserRuntime(dependencies)

    expect(runtime).toEqual(dependencies)
  })

  it('assembles the default HTTP, terminal, and browser ports once', () => {
    expect(browserRuntime.api.sessions.status).toBeTypeOf('function')
    expect(browserRuntime.api.dashboard.get).toBeTypeOf('function')
    expect(browserRuntime.createTerminal).toBeTypeOf('function')
    expect(browserRuntime.clipboard.writeText).toBeTypeOf('function')
    expect(browserRuntime.clock.now).toBeTypeOf('function')
    expect(browserRuntime.visibility.subscribe).toBeTypeOf('function')
    expect(browserRuntime.canonicalServerUrl).toBe('http://localhost:3000')
    expect(browserRuntime.platform).toBeTypeOf('string')
  })

  it('sends HTTP(S) authorization callbacks to the callback URL and custom schemes home', () => {
    const assign = vi.fn()
    const original = globalThis.location
    Object.defineProperty(globalThis, 'location', {
      value: { origin: 'http://localhost:3000', assign },
      configurable: true,
    })
    try {
      browserRuntime.authorizationCompletion.navigate(
        'https://example.com/auth/callback?state=abc'
      )
      expect(assign).toHaveBeenCalledWith('https://example.com/auth/callback?state=abc')

      browserRuntime.authorizationCompletion.navigate('termflow://auth/callback?state=abc')
      expect(assign).toHaveBeenLastCalledWith(
        new URL('/', 'http://localhost:3000').toString()
      )
    } finally {
      Object.defineProperty(globalThis, 'location', {
        value: original,
        configurable: true,
      })
    }
  })
})
