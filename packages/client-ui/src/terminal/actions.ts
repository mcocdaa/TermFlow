import type { TerminalAction as TerminalActionId } from '@termflow/client-contracts'
export interface TmuxActionDefinition { id: TerminalActionId; label: string; destructive?: boolean }

export const tmuxActions: readonly TmuxActionDefinition[] = [
  { id: 'split_left_right', label: '左右切分 Pane' },
  { id: 'split_top_bottom', label: '上下切分 Pane' },
  { id: 'new_window', label: '新建 Window' },
  { id: 'select_left', label: '选择左侧 Pane' },
  { id: 'select_right', label: '选择右侧 Pane' },
  { id: 'select_up', label: '选择上方 Pane' },
  { id: 'select_down', label: '选择下方 Pane' },
  { id: 'toggle_zoom', label: '切换 tmux Zoom' },
  { id: 'copy_mode', label: '进入 Copy Mode' },
  { id: 'close_pane', label: '关闭 Pane', destructive: true },
]
