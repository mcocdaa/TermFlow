import type { ApiClient, TerminalSessionCallbacks, TerminalSessionLike } from '@termflow/client-core'
import { inject, type App } from 'vue'
import { createSessionActions, type SessionActions } from './composables/useSession'
import { createBottomToast, type BottomToastController } from './composables/useBottomToast'
import { bottomToastKey, clientRuntimeKey, sessionActionsKey, themeStateKey } from './runtimeKey'
import { createThemeState, type ThemeState } from './theme/theme'

export interface ClipboardPort {
  writeText(text: string): Promise<void>
}

export interface ClockPort {
  now(): number
  setTimeout(callback: () => void, delayMs: number): unknown
  clearTimeout(handle: unknown): void
  setInterval(callback: () => void, delayMs: number): unknown
  clearInterval(handle: unknown): void
}

export interface VisibilityPort {
  isHidden(): boolean
  subscribe(listener: () => void): () => void
}

export interface ClientCapabilities {
  manageSecurity: boolean
  manageAuthorizedClients: boolean
}

export interface AuthorizationCompletionPort {
  navigate(callbackUri: string): void
}

export interface ClientRuntime {
  readonly api: ApiClient
  readonly createTerminal: (termId: string, callbacks: TerminalSessionCallbacks) => TerminalSessionLike
  readonly clipboard: ClipboardPort
  readonly clock: ClockPort
  readonly visibility: VisibilityPort
  readonly capabilities: ClientCapabilities
  readonly authorizationCompletion: AuthorizationCompletionPort
  readonly canonicalServerUrl: string
  readonly platform: string
}

export interface ClientUiOptions {
  readonly theme?: ThemeState
}

export interface ClientUiPlugin {
  readonly session: SessionActions
  readonly theme: ThemeState
  readonly toast: BottomToastController
  install(app: App): void
}

function defaultThemeState(): ThemeState {
  return createThemeState(
    { load: () => null, save: () => undefined },
    { apply: () => undefined },
  )
}

export function createClientUi(runtime: ClientRuntime, options: ClientUiOptions = {}): ClientUiPlugin {
  Object.freeze(runtime)
  const session = createSessionActions(runtime.api)
  const theme = options.theme ?? defaultThemeState()
  const toast = createBottomToast(runtime.clock)
  return {
    session,
    theme,
    toast,
    install(app) {
      app.provide(clientRuntimeKey, runtime)
      app.provide(sessionActionsKey, session)
      app.provide(themeStateKey, theme)
      app.provide(bottomToastKey, toast)
    },
  }
}

export function useClientRuntime(): ClientRuntime {
  const runtime = inject(clientRuntimeKey, undefined)
  if (runtime === undefined) throw new Error('TermFlow client runtime is not installed.')
  return runtime
}
