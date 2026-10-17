import type {
  TerminalConnectionStatus,
  TerminalSessionCallbacks,
  TerminalSessionLike,
} from '@termflow/client-core'
import type { TerminalActionResultFrame } from '@termflow/client-contracts'
import { computed, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import { locale, t } from '../i18n'
import { useClientRuntime } from '../runtime'
import type { BindingSnapshot, TerminalActionId } from '../types'
import { createXtermAdapter, type TerminalAdapter, type TerminalAdapterFactory, type TerminalMouseDispatch } from '../terminal/xtermAdapter'

export type TerminalFactory = (termId: string, callbacks: TerminalSessionCallbacks) => TerminalSessionLike

const terminalErrorKeys: Record<string, string> = {
  instance_offline: 'terminal.error.instance_offline',
  unauthorized: 'terminal.error.unauthorized',
  backpressure: 'terminal.error.backpressure',
  target_not_found: 'terminal.error.target_not_found',
  rate_limited: 'terminal.error.rate_limited',
  stream_gap: 'terminal.error.stream_gap',
}

function terminalErrorKeyFor(code?: string | null): string {
  const key = code ? terminalErrorKeys[code] : undefined
  return key ?? 'terminal.error.default'
}

export function useTerminalSession(
  termId: string,
  host: Ref<HTMLElement | null>,
  terminalFactory: TerminalFactory | undefined = undefined,
  adapterFactory: TerminalAdapterFactory = createXtermAdapter,
  transformInput?: (value: string | Uint8Array) => string | Uint8Array,
) {
  const runtime = useClientRuntime()
  const status = ref<TerminalConnectionStatus>('connecting')
  const dimensions = ref<{ rows: number; cols: number } | null>(null)
  const bindings = ref<BindingSnapshot>({ prefix: t('terminal.unreportedPrefix'), bindings: [] })
  watch(locale, () => {
    if (bindings.value.prefix === '未报告' || bindings.value.prefix === 'Unreported') {
      bindings.value = { ...bindings.value, prefix: t('terminal.unreportedPrefix') }
    }
  })
  const terminalErrorKey = ref('')
  const terminalError = computed(() => {
    void locale.value
    return terminalErrorKey.value ? t(terminalErrorKey.value) : ''
  })
  const lastActionResult = ref<TerminalActionResultFrame | null>(null)
  const resetKey = ref(0)
  const authenticationRequired = ref(0)
  const pendingOutput: Uint8Array[] = []
  let adapter: TerminalAdapter | null = null

  const callbacks: TerminalSessionCallbacks = {
    onStatus: (value) => {
      status.value = value
      if (value === 'connected') terminalErrorKey.value = ''
      adapter?.setInputEnabled(value === 'connected')
    },
    onReady: (control) => {
      dimensions.value = { rows: control.rows, cols: control.cols }
      if (!adapter && host.value) {
        adapter = adapterFactory(host.value, dimensions.value, (data) => { void terminal.sendInput(transformInput ? transformInput(data) : data) }, runtime.platform)
        adapter.setInputEnabled(true)
        for (const bytes of pendingOutput.splice(0)) adapter.write(bytes)
        adapter.focus()
      } else adapter?.resize(control.cols, control.rows)
    },
    onOutput: (bytes) => { if (adapter) adapter.write(bytes); else pendingOutput.push(bytes) },
    onSize: (size) => { dimensions.value = size; adapter?.resize(size.cols, size.rows) },
    onBindings: (control) => { bindings.value = { prefix: control.prefix, prefix2: control.prefix2, bindings: control.bindings } },
    onError: (error) => { terminalErrorKey.value = terminalErrorKeyFor(error.code) },
    onClosed: (reason) => { terminalErrorKey.value = reason === 'replaced' ? 'terminal.closed.replaced' : 'terminal.closed.default'; resetKey.value += 1 },
    onReset: () => { adapter?.reset(); resetKey.value += 1 },
    onActionResult: (result) => { lastActionResult.value = result; if (!result.ok) terminalErrorKey.value = terminalErrorKeyFor(result.error_code) },
    onAuthenticationRequired: () => { authenticationRequired.value += 1 },
  }
  const terminal = (terminalFactory ?? runtime.createTerminal)(termId, callbacks)
  onMounted(() => { void terminal.connect() })
  onBeforeUnmount(() => { adapter?.dispose(); adapter = null; pendingOutput.length = 0; void terminal.dispose() })

  return {
    status, dimensions, bindings, terminalError, lastActionResult, resetKey, authenticationRequired,
    sendAction: (actionId: TerminalActionId, options?: { targetPaneId?: string; confirmed?: boolean }) => terminal.sendAction(actionId, options),
    sendInput: (value: string | Uint8Array) => terminal.sendInput(value),
    focus: () => adapter?.focus(),
    refreshTheme: () => adapter?.refreshTheme(),
    measureCell: () => adapter?.measureCell() ?? null,
    setVisualScale: (scale: number) => adapter?.setVisualScale(scale) ?? null,
    canClientPan: () => adapter?.canClientPan() ?? false,
    canNativeWheel: () => adapter?.canNativeWheel() ?? false,
    dispatchMouse: (event: TerminalMouseDispatch) => adapter?.dispatchMouse(event),
  }
}
