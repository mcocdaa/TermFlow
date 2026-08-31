import { writeText } from '@tauri-apps/plugin-clipboard-manager'
import { arch, platform } from '@tauri-apps/plugin-os'
import { createApiClient, TerminalSession, type TerminalScheduler } from '@termflow/client-core'
import type { ClientRuntime } from '@termflow/client-ui'
import { createTauriHttpTransport } from './adapters/tauriHttpTransport'
import { createTauriTerminalTransport } from './adapters/tauriTerminalTransport'
import { clearNativeCredentials } from './adapters/tauriCredentialVault'
import { serverConfig } from './serverConfig'

export async function createTauriRuntime(): Promise<ClientRuntime> {
  await serverConfig.load()
  const scheduler: TerminalScheduler = { set: (callback, delay) => globalThis.setTimeout(callback, delay), clear: (handle) => globalThis.clearTimeout(handle as number) }
  const terminalTransport = createTauriTerminalTransport()
  const api = createApiClient(createTauriHttpTransport())
  return {
    api: {
      ...api,
      sessions: {
        ...api.sessions,
        async status(signal?: AbortSignal) {
          await api.dashboard.get(signal)
          return { authenticated: true, expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString() }
        },
        async logout() {
          await clearNativeCredentials(serverConfig.current)
          return { ok: true }
        },
      },
    },
    createTerminal: (termId, callbacks) => new TerminalSession(termId, callbacks, { transport: terminalTransport, scheduler, createId: () => globalThis.crypto.randomUUID() }),
    clipboard: { writeText },
    clock: { now: Date.now, setTimeout: (callback, delay) => globalThis.setTimeout(callback, delay), clearTimeout: (handle) => globalThis.clearTimeout(handle as number), setInterval: (callback, delay) => globalThis.setInterval(callback, delay), clearInterval: (handle) => globalThis.clearInterval(handle as number) },
    visibility: { isHidden: () => document.hidden, subscribe: (listener) => { document.addEventListener('visibilitychange', listener); return () => document.removeEventListener('visibilitychange', listener) } },
    capabilities: { manageSecurity: false, manageAuthorizedClients: false },
    authorizationCompletion: { navigate: () => undefined },
    get canonicalServerUrl() { return serverConfig.current },
    platform: `${platform()} ${arch()}`,
  }
}
