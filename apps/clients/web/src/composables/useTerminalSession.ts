import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import type { BindingSnapshotDto } from '../api/types'
import type { TerminalAdapter, TerminalAdapterFactory } from '../terminal/terminalAdapter'
import { createXtermAdapter } from '../terminal/terminalAdapter'
import type { TerminalActionResultControl } from '../terminal/protocol'
import type { TerminalConnectionStatus, TerminalSocketCallbacks, TerminalSocketLike } from '../terminal/socket'
import { createTerminalSocket } from '../terminal/socket'

export type TerminalSocketFactory = (termId: string, callbacks: TerminalSocketCallbacks) => TerminalSocketLike

export function useTerminalSession(termId: string, host: Ref<HTMLElement | null>, socketFactory: TerminalSocketFactory = createTerminalSocket, adapterFactory: TerminalAdapterFactory = createXtermAdapter) {
  const status = ref<TerminalConnectionStatus>('connecting')
  const dimensions = ref<{ rows: number; cols: number } | null>(null)
  const bindings = ref<BindingSnapshotDto>({ prefix: '未报告', actions: {} })
  const terminalError = ref('')
  const lastActionResult = ref<TerminalActionResultControl | null>(null)
  const pendingOutput: Uint8Array[] = []
  let adapter: TerminalAdapter | null = null

  const callbacks: TerminalSocketCallbacks = {
    onStatus: (value) => { status.value = value },
    onReady: (control) => {
      dimensions.value = { rows: control.rows, cols: control.cols }
      if (!adapter && host.value) {
        adapter = adapterFactory(host.value, dimensions.value, (data) => socket.sendInput(data))
        for (const bytes of pendingOutput.splice(0)) adapter.write(bytes)
        adapter.focus()
      } else adapter?.resize(control.cols, control.rows)
    },
    onOutput: (bytes) => { if (adapter) adapter.write(bytes); else pendingOutput.push(bytes) },
    onSize: (size) => { dimensions.value = size; adapter?.resize(size.cols, size.rows) },
    onBindings: (control) => { bindings.value = { prefix: control.prefix, actions: control.actions } },
    onError: (error) => { terminalError.value = error.message || `终端错误：${error.code}` },
    onClosed: (reason) => { terminalError.value = reason === 'replaced' ? '此终端已被另一个已认证连接接管。' : '终端连接已关闭。' },
    onReset: () => adapter?.reset(),
    onActionResult: (result) => { lastActionResult.value = result; if (!result.ok) terminalError.value = result.error || '操作未完成。' },
  }
  const socket = socketFactory(termId, callbacks)
  onMounted(() => socket.connect())
  onBeforeUnmount(() => { adapter?.dispose(); adapter = null; pendingOutput.length = 0; socket.dispose() })

  return {
    status, dimensions, bindings, terminalError, lastActionResult,
    sendAction: (actionId: string, options?: { targetPaneId?: string; confirmed?: boolean }) => socket.sendAction(actionId, options),
    focus: () => adapter?.focus(),
  }
}
