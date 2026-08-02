import type {
  TerminalConnectionStatus,
  TerminalSessionCallbacks,
  TerminalSessionLike,
} from '@termflow/client-core'
import type { TerminalActionResultFrame } from '@termflow/client-contracts'
import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import { useClientRuntime } from '../runtime'
import type { BindingSnapshot, TerminalActionId } from '../types'
import { createXtermAdapter, type TerminalAdapter, type TerminalAdapterFactory } from '../terminal/xtermAdapter'

export type TerminalFactory = (termId: string, callbacks: TerminalSessionCallbacks) => TerminalSessionLike

const terminalErrorMessages: Record<string, string> = {
  instance_offline: 'Term 当前离线，无法打开终端。',
  unauthorized: '会话已过期，请重新登录。',
  backpressure: '终端繁忙，请稍后重试。',
  target_not_found: '目标 Pane 已不存在，请刷新状态。',
  rate_limited: '操作过于频繁，请稍后重试。',
  stream_gap: '连接已重建，正在等待终端重绘。',
}

function userFacingTerminalError(code?: string | null) {
  return code ? terminalErrorMessages[code] ?? '终端发生错误，请稍后重试。' : '终端发生错误，请稍后重试。'
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
  const bindings = ref<BindingSnapshot>({ prefix: '未报告', bindings: [] })
  const terminalError = ref('')
  const lastActionResult = ref<TerminalActionResultFrame | null>(null)
  const resetKey = ref(0)
  const authenticationRequired = ref(0)
  const pendingOutput: Uint8Array[] = []
  let adapter: TerminalAdapter | null = null

  const callbacks: TerminalSessionCallbacks = {
    onStatus: (value) => { status.value = value; adapter?.setInputEnabled(value === 'connected') },
    onReady: (control) => {
      dimensions.value = { rows: control.rows, cols: control.cols }
      if (!adapter && host.value) {
        adapter = adapterFactory(host.value, dimensions.value, (data) => terminal.sendInput(transformInput ? transformInput(data) : data))
        adapter.setInputEnabled(true)
        for (const bytes of pendingOutput.splice(0)) adapter.write(bytes)
        adapter.focus()
      } else adapter?.resize(control.cols, control.rows)
    },
    onOutput: (bytes) => { if (adapter) adapter.write(bytes); else pendingOutput.push(bytes) },
    onSize: (size) => { dimensions.value = size; adapter?.resize(size.cols, size.rows) },
    onBindings: (control) => { bindings.value = { prefix: control.prefix, prefix2: control.prefix2, bindings: control.bindings } },
    onError: (error) => { terminalError.value = userFacingTerminalError(error.code) },
    onClosed: (reason) => { terminalError.value = reason === 'replaced' ? '此终端已被另一个已认证连接接管。' : '终端连接已关闭。'; resetKey.value += 1 },
    onReset: () => { adapter?.reset(); resetKey.value += 1 },
    onActionResult: (result) => { lastActionResult.value = result; if (!result.ok) terminalError.value = userFacingTerminalError(result.error_code) },
    onAuthenticationRequired: () => { authenticationRequired.value += 1 },
  }
  const terminal = (terminalFactory ?? runtime.createTerminal)(termId, callbacks)
  onMounted(() => terminal.connect())
  onBeforeUnmount(() => { adapter?.dispose(); adapter = null; pendingOutput.length = 0; terminal.dispose() })

  return {
    status, dimensions, bindings, terminalError, lastActionResult, resetKey, authenticationRequired,
    sendAction: (actionId: TerminalActionId, options?: { targetPaneId?: string; confirmed?: boolean }) => terminal.sendAction(actionId, options),
    sendInput: (value: string | Uint8Array) => terminal.sendInput(value),
    focus: () => adapter?.focus(),
    refreshTheme: () => adapter?.refreshTheme(),
    measureCell: () => adapter?.measureCell() ?? null,
    canClientPan: () => adapter?.canClientPan() ?? false,
  }
}
