import { createApiClient } from '@termflow/client-core'
import type { ClientRuntime } from '@termflow/client-ui'

export function createFakeRuntime(overrides: Partial<ClientRuntime> = {}): ClientRuntime {
  const api = createApiClient({ request: async (path) => {
    let body: unknown = {}
    if (path.includes('capabilities')) body = { agent_broker_enabled: false, state: 'disabled', reason_code: null, delegated_write_grants_enabled: false }
    else if (path.includes('dashboard')) body = { computers: [], metrics: {} }
    else if (path.includes('topology')) body = { topology: { session_name: 'Term', revision: 1, windows: [] } }
    else if (path.includes('session')) body = { authenticated: true, expires_at: '2030-01-01' }
    return { status: 200, headers: new Headers(), body }
  } })
  return {
    api,
    sensitiveAuthorization: { mode: 'browser-session' },
    createTerminal: () => ({ async connect() {}, async sendInput() {}, async sendAction() {}, async dispose() {} }),
    createAgentStream: () => ({ async connect(_request, emit) { emit({ type: 'open' }); return { async close() {} } } }),
    agentCursorStore: { load: () => null, save: () => undefined, clear: () => undefined },
    clipboard: { writeText: async () => undefined },
    clock: { now: () => 0, setTimeout: () => 1, clearTimeout: () => undefined, setInterval: () => 1, clearInterval: () => undefined },
    visibility: { isHidden: () => false, subscribe: () => () => undefined },
    capabilities: { manageSecurity: true, manageAuthorizedClients: true },
    authorizationCompletion: { navigate: () => undefined },
    canonicalServerUrl: 'https://control.example', platform: 'Linux',
    ...overrides,
  }
}
