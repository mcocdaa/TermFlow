import type { ClientRuntime } from '@termflow/client-ui'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { browserApiClient } from './api/http'
import { browserRuntime, createBrowserRuntime } from './runtime'
import { createTerminalSocket } from './terminal/socket'

describe('browser runtime composition', () => {
  it('assembles the injected browser ports as one ClientRuntime', () => {
    const dependencies: ClientRuntime = {
      api: {} as ClientRuntime['api'],
      createTerminal: () => ({ connect() {}, sendInput() {}, sendAction() {}, dispose() {} }),
      clipboard: { writeText: async () => undefined },
      clock: { now: () => 1, setTimeout: () => 2, clearTimeout: () => undefined, setInterval: () => 3, clearInterval: () => undefined },
      visibility: { isHidden: () => false, subscribe: () => () => undefined },
      canonicalServerUrl: 'https://b.termflow.test',
    }

    const runtime = createBrowserRuntime(dependencies)

    expect(runtime).toEqual(dependencies)
  })

  it('assembles the default browser ports once and routes the session facade through that runtime', () => {
    expect(browserRuntime.api).toBe(browserApiClient)
    expect(browserRuntime.createTerminal).toBe(createTerminalSocket)
    expect(browserRuntime.clipboard.writeText).toBeTypeOf('function')
    expect(browserRuntime.clock.now).toBeTypeOf('function')
    expect(browserRuntime.visibility.subscribe).toBeTypeOf('function')
    expect(browserRuntime.canonicalServerUrl).toBe('http://localhost:3000')

    const sessionFacade = readFileSync(resolve(process.cwd(), 'src/stores/session.ts'), 'utf8')
    expect(sessionFacade).toContain('createSessionActions(browserRuntime.api)')
    expect(sessionFacade).not.toContain('browserApiClient')
  })
})
