import type { ClientRuntime } from '../runtime'

export function createFakeRuntime(overrides: Partial<ClientRuntime> = {}): ClientRuntime {
  return {
    api: {} as ClientRuntime['api'],
    createTerminal: () => ({ connect() {}, sendInput() {}, sendAction() {}, dispose() {} }),
    clipboard: { writeText: async () => undefined },
    clock: {
      now: () => 0,
      setTimeout: () => 1,
      clearTimeout: () => undefined,
      setInterval: () => 1,
      clearInterval: () => undefined,
    },
    visibility: { isHidden: () => false, subscribe: () => () => undefined },
    canonicalServerUrl: 'https://control.example',
    ...overrides,
  }
}
