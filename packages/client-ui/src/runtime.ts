import type { ApiClient, TerminalSessionCallbacks, TerminalSessionLike } from '@termflow/client-core'
import { inject, type Plugin } from 'vue'
import { clientRuntimeKey } from './runtimeKey'

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

export interface ClientRuntime {
  readonly api: ApiClient
  readonly createTerminal: (termId: string, callbacks: TerminalSessionCallbacks) => TerminalSessionLike
  readonly clipboard: ClipboardPort
  readonly clock: ClockPort
  readonly visibility: VisibilityPort
  readonly canonicalServerUrl: string
}

export function createClientUi(runtime: ClientRuntime): Plugin {
  Object.freeze(runtime)
  return {
    install(app) { app.provide(clientRuntimeKey, runtime) },
  }
}

export function useClientRuntime(): ClientRuntime {
  const runtime = inject(clientRuntimeKey, undefined)
  if (runtime === undefined) throw new Error('TermFlow client runtime is not installed.')
  return runtime
}
