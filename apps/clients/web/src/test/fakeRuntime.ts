import type { ClientRuntime } from '@termflow/client-ui'

export function createFakeRuntime(): ClientRuntime {
  return {
    api: {
      dashboard: { get: async () => ({ metrics: { online_terms: 0, total_terms: 0, active_panes: 0, interactions_24h: 0, computers: 0 }, computers: [] }) },
      computers: { list: async () => ({ computers: [] }) },
    } as unknown as ClientRuntime['api'],
    createTerminal: () => ({ connect() {}, sendInput() {}, sendAction() {}, dispose() {} }),
    clipboard: { writeText: async () => undefined },
    clock: { now: () => 0, setTimeout: () => 1, clearTimeout: () => undefined, setInterval: () => 1, clearInterval: () => undefined },
    visibility: { isHidden: () => false, subscribe: () => () => undefined },
    canonicalServerUrl: 'https://control.test',
  }
}
