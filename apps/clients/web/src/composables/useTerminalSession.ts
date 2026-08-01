import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import type { BindingSnapshotDto } from '../api/types'
import type { TerminalActionId } from '../api/types'
import type { TerminalAdapter, TerminalAdapterFactory } from '../terminal/terminalAdapter'
import { createXtermAdapter } from '../terminal/terminalAdapter'
import type { TerminalActionResultControl } from '../terminal/protocol'
import type { TerminalConnectionStatus, TerminalSocketCallbacks, TerminalSocketLike } from '../terminal/socket'
import { createTerminalSocket } from '../terminal/socket'

export type TerminalSocketFactory = (termId: string, callbacks: TerminalSocketCallbacks) => TerminalSocketLike

export function useTerminalSession(termId: string, host: Ref<HTMLElement | null>, socketFactory: TerminalSocketFactory = createTerminalSocket, adapterFactory: TerminalAdapterFactory = createXtermAdapter, transformInput?: (value: string | Uint8Array) => string | Uint8Array) {
  const status = ref<TerminalConnectionStatus>('connecting')
  const dimensions = ref<{ rows: number; cols: number } | null>(null)
  const bindings = ref<BindingSnapshotDto>({ prefix: '未报告', bindings: [] })
  const terminalError = ref('')
  const lastActionResult = ref<TerminalActionResultControl | null>(null)
  const resetKey = ref(0)
  const pendingOutput: Uint8Array[] = []
  let adapter: TerminalAdapter | null = null

  const callbacks: TerminalSocketCallbacks = {
    onStatus: (value) => { status.value = value },
    onReady: (control) => {
      dimensions.value = { rows: control.rows, cols: control.cols }
      if (!adapter && host.value) {
        adapter = adapterFactory(host.value, dimensions.value, (data) => socket.sendInput(transformInput ? transformInput(data) : data))
        for (const bytes of pendingOutput.splice(0)) adapter.write(bytes)
        adapter.focus()
      } else adapter?.resize(control.cols, control.rows)
    },
    onOutput: (bytes) => { if (adapter) adapter.write(bytes); else pendingOutput.push(bytes) },
    onSize: (size) => { dimensions.value = size; adapter?.resize(size.cols, size.rows) },
    onBindings: (control) => { bindings.value = { prefix: control.prefix, prefix2: control.prefix2, bindings: control.bindings } },
    onError: (error) => { terminalError.value = error.message || `终端错误：${error.code}` },
    onClosed: (reason) => { terminalError.value = reason === 'replaced' ? '此终端已被另一个已认证连接接管。' : '终端连接已关闭。'; resetKey.value += 1 },
    onReset: () => { adapter?.reset(); resetKey.value += 1 },
    onActionResult: (result) => { lastActionResult.value = result; if (!result.ok) terminalError.value = result.error_code ? `操作未完成：${result.error_code}` : '操作未完成。' },
  }
  const socket = socketFactory(termId, callbacks)
  onMounted(() => socket.connect())
  onBeforeUnmount(() => { adapter?.dispose(); adapter = null; pendingOutput.length = 0; socket.dispose() })

  return {
    status, dimensions, bindings, terminalError, lastActionResult, resetKey,
    sendAction: (actionId: TerminalActionId, options?: { targetPaneId?: string; confirmed?: boolean }) => socket.sendAction(actionId, options),
    sendInput: (value: string | Uint8Array) => socket.sendInput(value),
    focus: () => adapter?.focus(),
    canClientPan: () => adapter?.canClientPan() ?? false,
  }
}
