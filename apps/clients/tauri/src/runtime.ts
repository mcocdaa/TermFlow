import { writeText } from '@tauri-apps/plugin-clipboard-manager'
import { arch, platform } from '@tauri-apps/plugin-os'
import { createApiClient, TerminalSession, type TerminalScheduler } from '@termflow/client-core'
import type { ClientRuntime } from '@termflow/client-ui'
import { createTauriHttpTransport } from './adapters/tauriHttpTransport'
import { createTauriTerminalTransport } from './adapters/tauriTerminalTransport'
import { clearNativeCredentials } from './adapters/tauriCredentialVault'
import { createTauriAudioUpload } from './adapters/tauriAudioUpload'
import { createSessionDraftStore } from './adapters/sessionDraftStore'
import { createTauriVoiceRecorder, isMobileVoicePlatform } from './adapters/tauriVoiceRecorder'
import { serverConfig } from './serverConfig'

export async function createTauriRuntime(): Promise<ClientRuntime> {
  await serverConfig.load()
  const scheduler: TerminalScheduler = { set: (callback, delay) => globalThis.setTimeout(callback, delay), clear: (handle) => globalThis.clearTimeout(handle as number) }
  const terminalTransport = createTauriTerminalTransport()
  const api = createApiClient(createTauriHttpTransport())
  const currentPlatform = platform()
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
    // Agent ports (M6b spec §4.6): the Rust-owned Tauri transport lands
    // with plan 1017; until then the stream factory fails fast and the
    // cursor store degrades to an in-memory slot (persistence is an
    // optimization, never a correctness dependency — §4.4).
    createAgentStream: () => {
      throw new Error('Agent stream transport is not implemented in the Tauri client yet (plan 1017)')
    },
    agentCursorStore: (() => {
      const entries = new Map<string, { cursor: string, seq: number }>()
      return {
        load: (conversationId) => entries.get(conversationId) ?? null,
        save: (conversationId, cursor, seq) => { entries.set(conversationId, { cursor, seq }) },
        clear: (conversationId) => { entries.delete(conversationId) },
      }
    })(),
    clipboard: { writeText },
    clock: { now: Date.now, setTimeout: (callback, delay) => globalThis.setTimeout(callback, delay), clearTimeout: (handle) => globalThis.clearTimeout(handle as number), setInterval: (callback, delay) => globalThis.setInterval(callback, delay), clearInterval: (handle) => globalThis.clearInterval(handle as number) },
    visibility: { isHidden: () => document.hidden, subscribe: (listener) => { document.addEventListener('visibilitychange', listener); return () => document.removeEventListener('visibilitychange', listener) } },
    capabilities: { manageSecurity: false, manageAuthorizedClients: false },
    authorizationCompletion: { navigate: () => undefined },
    get canonicalServerUrl() { return serverConfig.current },
    platform: `${currentPlatform} ${arch()}`,
    // Mobile-only voice capability (M7b spec §4.2): web/desktop get no
    // runtime injection, here `enabled()` gates the injected adapters on
    // platform ∈ {android, ios} ∧ a capture pipeline (getUserMedia) exists.
    voice: {
      uploadAudio: createTauriAudioUpload(),
      createRecorder: () => createTauriVoiceRecorder({ platform: currentPlatform }),
      enabled: () => isMobileVoicePlatform(currentPlatform) && typeof navigator.mediaDevices?.getUserMedia === 'function',
      draftStore: createSessionDraftStore(),
    },
  }
}
