import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import type { BindingSnapshotDto } from '../api/types'
import type { TerminalActionId } from '../api/types'
import type { TerminalAdapter, TerminalAdapterFactory } from '../terminal/terminalAdapter'
import { createXtermAdapter } from '../terminal/terminalAdapter'
import type { TerminalActionResultControl } from '../terminal/protocol'
import type { TerminalConnectionStatus, TerminalSocketCallbacks, TerminalSocketLike } from '../terminal/socket'
import { createTerminalSocket } from '../terminal/socket'

export type TerminalSocketFactory = (termId: string, callbacks: TerminalSocketCallbacks) => TerminalSocketLike

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
    onError: (error) => { terminalError.value = userFacingTerminalError(error.code) },
    onClosed: (reason) => { terminalError.value = reason === 'replaced' ? '此终端已被另一个已认证连接接管。' : '终端连接已关闭。'; resetKey.value += 1 },
    onReset: () => { adapter?.reset(); resetKey.value += 1 },
    onActionResult: (result) => { lastActionResult.value = result; if (!result.ok) terminalError.value = userFacingTerminalError(result.error_code) },
  }
  const socket = socketFactory(termId, callbacks)
  onMounted(() => socket.connect())
  onBeforeUnmount(() => { adapter?.dispose(); adapter = null; pendingOutput.length = 0; socket.dispose() })

  return {
    status, dimensions, bindings, terminalError, lastActionResult, resetKey,
    sendAction: (actionId: TerminalActionId, options?: { targetPaneId?: string; confirmed?: boolean }) => socket.sendAction(actionId, options),
    sendInput: (value: string | Uint8Array) => socket.sendInput(value),
    focus: () => adapter?.focus(),
    refreshTheme: () => adapter?.refreshTheme(),
    canClientPan: () => adapter?.canClientPan() ?? false,
  }
}
