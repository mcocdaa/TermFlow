import type { ClientRuntime } from '../runtime'

export function createFakeRuntime(overrides: Partial<ClientRuntime> = {}): ClientRuntime {
  return {
    api: {
      sessions: {
        status: async () => ({ authenticated: true, expires_at: null }),
        login: async () => ({ authenticated: true, expires_at: null }),
        logout: async () => ({ authenticated: false }),
      },
      dashboard: { get: async () => ({ metrics: { online_terms: 0, total_terms: 0, active_panes: 0, interactions_24h: 0, computers: 0 }, computers: [] }) },
      computers: { list: async () => ({ computers: [] }), rename: async (_id: string, name: string) => ({ installation_id: _id, display_name: name, hostname: null, platform: null, client_version: null, online: false, registered_at: '', last_seen_at: null, terms: [] }) },
      terms: {
        topology: async (id: string) => ({ instance_id: id, topology: { session_id: '$0', session_name: `Term · ${id}`, revision: 0, windows: [] } }),
        rename: async (id: string, name: string) => ({ instance_id: id, name, online: true, window_count: 0, pane_count: 0, active_pane_count: 0, current_command: null, last_seen_at: null }),
      },
    } as unknown as ClientRuntime['api'],
    createTerminal: () => ({ async connect() {}, async sendInput() {}, async sendAction() {}, async dispose() {} }),
    clipboard: { writeText: async () => undefined },
    clock: {
      now: () => 0,
      setTimeout: () => 1,
      clearTimeout: () => undefined,
      setInterval: () => 1,
      clearInterval: () => undefined,
    },
    visibility: { isHidden: () => false, subscribe: () => () => undefined },
    capabilities: { manageSecurity: true, manageAuthorizedClients: true },
    authorizationCompletion: { navigate: () => undefined },
    canonicalServerUrl: 'https://control.example',
    platform: 'Linux x86_64',
    ...overrides,
  }
}
