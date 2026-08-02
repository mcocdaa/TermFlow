import type { ClientRuntime } from '@termflow/client-ui'
import { describe, expect, it } from 'vitest'
import { createBrowserRuntime } from './runtime'

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
})
