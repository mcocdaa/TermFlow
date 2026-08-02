import type { ClientRuntime } from '@termflow/client-ui'
import { browserApiClient } from './api/http'
import { createBrowserClipboard } from './adapters/browserClipboard'
import { createBrowserClock } from './adapters/browserClock'
import { browserCanonicalServerUrl } from './adapters/browserCanonicalServerUrl'
import { createBrowserVisibility } from './adapters/browserVisibility'
import { createTerminalSocket } from './terminal/socket'

function browserDependencies(): ClientRuntime {
  return {
    api: browserApiClient,
    createTerminal: createTerminalSocket,
    clipboard: createBrowserClipboard(),
    clock: createBrowserClock(),
    visibility: createBrowserVisibility(),
    canonicalServerUrl: browserCanonicalServerUrl(),
  }
}

export function createBrowserRuntime(dependencies: ClientRuntime = browserDependencies()): ClientRuntime {
  return dependencies
}
