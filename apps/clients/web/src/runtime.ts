import { createApiClient, TerminalSession, type TerminalScheduler } from '@termflow/client-core'
import type { ClientRuntime } from '@termflow/client-ui'
import { createBrowserClipboard } from './adapters/browserClipboard'
import { createBrowserClock } from './adapters/browserClock'
import { browserCanonicalServerUrl } from './adapters/browserCanonicalServerUrl'
import { createBrowserHttpTransport } from './adapters/browserHttpTransport'
import { browserPlatform } from './adapters/browserPlatform'
import { createBrowserTerminalTransport } from './adapters/browserTerminalTransport'
import { createBrowserVisibility } from './adapters/browserVisibility'

function browserDependencies(): ClientRuntime {
  const clock = createBrowserClock()
  const terminalTransport = createBrowserTerminalTransport()
  const scheduler: TerminalScheduler = {
    set: (callback, delayMs) => clock.setTimeout(callback, delayMs),
    clear: (handle) => clock.clearTimeout(handle),
  }
  return {
    api: createApiClient(createBrowserHttpTransport()),
    createTerminal: (termId, callbacks) => new TerminalSession(termId, callbacks, {
      transport: terminalTransport,
      scheduler,
      createId: () => globalThis.crypto.randomUUID(),
    }),
    clipboard: createBrowserClipboard(),
    clock,
    visibility: createBrowserVisibility(),
    capabilities: { manageSecurity: true, manageAuthorizedClients: true },
    authorizationCompletion: {
      navigate: (callbackUri) => {
        try {
          const parsed = new URL(callbackUri)
          if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
            globalThis.location.assign(callbackUri)
            return
          }
        } catch {
          // fall through to the home page
        }
        globalThis.location.assign(new URL('/', globalThis.location.origin).toString())
      },
    },
    canonicalServerUrl: browserCanonicalServerUrl(),
    platform: browserPlatform(),
  }
}

export function createBrowserRuntime(dependencies: ClientRuntime = browserDependencies()): ClientRuntime {
  return dependencies
}

export const browserRuntime = createBrowserRuntime()
