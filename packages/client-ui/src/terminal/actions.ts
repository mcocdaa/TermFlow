import type { TerminalAction as TerminalActionId } from '@termflow/client-contracts'
import { t } from '../i18n'

export interface TmuxActionDefinition {
  id: TerminalActionId
  labelKey: string
  readonly label: string
  destructive?: boolean
}

function defineTmuxAction(id: TerminalActionId, destructive?: boolean): TmuxActionDefinition {
  const labelKey = `terminal.tmux.action.${id}`
  return {
    id,
    labelKey,
    ...(destructive ? { destructive: true } : {}),
    get label() {
      return t(labelKey)
    },
  }
}

export const tmuxActions: readonly TmuxActionDefinition[] = [
  defineTmuxAction('split_left_right'),
  defineTmuxAction('split_top_bottom'),
  defineTmuxAction('new_window'),
  defineTmuxAction('select_left'),
  defineTmuxAction('select_right'),
  defineTmuxAction('select_up'),
  defineTmuxAction('select_down'),
  defineTmuxAction('toggle_zoom'),
  defineTmuxAction('copy_mode'),
  defineTmuxAction('close_pane', true),
]

