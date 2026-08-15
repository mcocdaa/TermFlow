import type { AgentCursorStore, AgentStreamTransport, AguiEvent, RecordedAudio, TranscriptionUploadResponse, VoiceStorage } from '@termflow/client-core'
import type { ClientRuntime, ClientVoiceRuntime } from '../runtime'

/** B side upload 201 body (M7b spec §3.1) — tests tweak fields per scenario. */
export function createFakeUploadResponse(overrides: Partial<TranscriptionUploadResponse> = {}): TranscriptionUploadResponse {
  return {
    draft_id: 'draft-fake-1',
    state: 'pending_confirm',
    transcript: '测试转写文本',
    provider: 'fake-speeches',
    region: 'cn-beijing',
    language: 'zh',
    duration_seconds: 2.5,
    expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    ...overrides,
  }
}

/**
 * In-memory draft slot for tests (M7b spec §4.7.4) — mirrors the client-ui
 * in-memory fallback so tests never touch platform storage.
 */
export function createFakeVoiceStorage(): VoiceStorage {
  const slots = new Map<string, string>()
  return {
    getItem: (key) => slots.get(key) ?? null,
    setItem: (key, value) => {
      slots.set(key, value)
    },
    removeItem: (key) => {
      slots.delete(key)
    },
  }
}

/**
 * Voice capability stub (M7b spec §4.2). Tests override `enabled`, the
 * recorder, or the uploader per scenario; the defaults complete a happy-path
 * press → upload → draft flow.
 */
export function createFakeVoice(overrides: Partial<ClientVoiceRuntime> = {}): ClientVoiceRuntime {
  return {
    uploadAudio: async (_audio: RecordedAudio): Promise<TranscriptionUploadResponse> =>
      createFakeUploadResponse(),
    createRecorder: () => ({
      start: async () => undefined,
      stop: async () => ({
        blob: new Blob(['fake-audio'], { type: 'audio/webm' }),
        mimeType: 'audio/webm',
        durationSeconds: 1.2,
      }),
      abort: () => undefined,
    }),
    enabled: () => true,
    draftStore: createFakeVoiceStorage(),
    ...overrides,
  }
}

/**
 * In-memory Agent cursor store for tests (M6b spec §4.4) — mirrors the web
 * localStorage adapter so composable tests never touch platform storage.
 */
export function createFakeAgentCursorStore(): AgentCursorStore {
  const entries = new Map<string, { cursor: string, seq: number }>()
  return {
    load: (conversationId) => entries.get(conversationId) ?? null,
    save: (conversationId, cursor, seq) => {
      entries.set(conversationId, { cursor, seq })
    },
    clear: (conversationId) => {
      entries.delete(conversationId)
    },
  }
}

/**
 * Quiet no-op Agent stream transport stub (M6b spec §4.6). Composable tests
 * replace it with a scripted fake to drive connect requests and frame
 * emissions; the default keeps runtime construction cheap for unrelated
 * suites.
 */
export function createFakeAgentStreamTransport(): AgentStreamTransport<AguiEvent> {
  return {
    connect: async (_request, emit) => {
      const closed = { value: false }
      emit({ type: 'open' })
      return {
        close: async (_code, _reason) => {
          closed.value = true
        },
      }
    },
  }
}

export type FakeRuntimeOverrides = Partial<Omit<ClientRuntime, 'voice'>> & {
  /** Tests pass `voice: undefined` to simulate a runtime without the capability. */
  voice?: ClientVoiceRuntime | undefined
}

export function createFakeRuntime(overrides: FakeRuntimeOverrides = {}): ClientRuntime {
  const { voice, ...rest } = overrides
  const base = {
    api: {
      sessions: {
        status: async () => ({ authenticated: true, expires_at: null }),
        login: async () => ({ authenticated: true, expires_at: null }),
        logout: async () => ({ authenticated: false }),
      },
      dashboard: { get: async () => ({ metrics: { online_terms: 0, total_terms: 0, active_panes: 0, interactions_24h: 0, computers: 0 }, computers: [] }) },
      computers: { list: async () => ({ computers: [] }), remove: async () => undefined, rename: async (_id: string, name: string) => ({ installation_id: _id, display_name: name, hostname: null, platform: null, client_version: null, online: false, registered_at: '', last_seen_at: null, terms: [] }) },
      terms: {
        topology: async (id: string) => ({ instance_id: id, topology: { session_id: '$0', session_name: `Term · ${id}`, revision: 0, windows: [] } }),
        rename: async (id: string, name: string) => ({ instance_id: id, name, online: true, window_count: 0, pane_count: 0, active_pane_count: 0, current_command: null, last_seen_at: null }),
        remove: async () => undefined,
      },
      // Draft lifecycle endpoints (confirm/cancel) ride this request helper
      // when `useVoiceDraft` builds its default draft API.
      request: async () => undefined,
      // App.vue gates the Agent navigation on this capability; disabled by
      // default so unrelated suites keep the fail-closed shell.
      agents: {
        capabilities: async () => ({ agent_broker_enabled: false, delegated_write_grants_enabled: false, speech_to_text_enabled: false }),
      },
    } as unknown as ClientRuntime['api'],
    createTerminal: () => ({ async connect() {}, async sendInput() {}, async sendAction() {}, async dispose() {} }),
    createAgentStream: createFakeAgentStreamTransport,
    agentCursorStore: createFakeAgentCursorStore(),
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
    ...rest,
  }
  // Re-attach the voice capability only when explicitly provided; the
  // undefined form must yield a runtime without the property (exactOptional).
  return voice === undefined ? base : { ...base, voice }
}
