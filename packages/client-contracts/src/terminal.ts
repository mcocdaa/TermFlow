import type {
  TerminalAction,
  TerminalActionResultFrame,
  TerminalBinding,
  TerminalBindingSnapshotFrame,
  TerminalClosedFrame,
  TerminalCloseReason,
  TerminalErrorFrame,
  TerminalReadyFrame,
  TerminalSizeFrame,
} from './generated'

export type TerminalControl =
  | TerminalReadyFrame
  | TerminalSizeFrame
  | TerminalBindingSnapshotFrame
  | TerminalErrorFrame
  | TerminalClosedFrame
  | TerminalActionResultFrame

const TERMINAL_ACTIONS = new Set<TerminalAction>([
  'split_left_right',
  'split_top_bottom',
  'new_window',
  'select_left',
  'select_right',
  'select_up',
  'select_down',
  'toggle_zoom',
  'copy_mode',
  'close_pane',
])

const TERMINAL_CLOSE_REASONS = new Set<TerminalCloseReason>([
  'client_closed',
  'replaced',
  'grace_expired',
  'stream_gap',
  'instance_offline',
  'internal_error',
])

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const nonEmptyString = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const uuid = (value: unknown): value is string =>
  typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
const nullableString = (value: unknown): value is string | null => value === null || typeof value === 'string'
const positiveInteger = (value: unknown): value is number => Number.isInteger(value) && Number(value) > 0
const hasOwn = (value: Record<string, unknown>, key: string) => Object.prototype.hasOwnProperty.call(value, key)
const terminalAction = (value: unknown): value is TerminalAction =>
  typeof value === 'string' && TERMINAL_ACTIONS.has(value as TerminalAction)
const terminalCloseReason = (value: unknown): value is TerminalCloseReason =>
  typeof value === 'string' && TERMINAL_CLOSE_REASONS.has(value as TerminalCloseReason)

function binding(value: unknown): TerminalBinding | null {
  if (!record(value) || !terminalAction(value.action) || !nullableString(value.key) || !nonEmptyString(value.tooltip)) return null
  return { action: value.action, key: value.key, tooltip: value.tooltip }
}

export function parseTerminalControl(text: string): TerminalControl | null {
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    return null
  }
  if (!record(value) || typeof value.type !== 'string' || !uuid(value.terminal_id)) return null

  switch (value.type) {
    case 'terminal.ready':
      return uuid(value.stream_id) && positiveInteger(value.rows) && positiveInteger(value.cols)
        ? { type: value.type, terminal_id: value.terminal_id, stream_id: value.stream_id, rows: value.rows, cols: value.cols }
        : null
    case 'terminal.size':
      return positiveInteger(value.rows) && positiveInteger(value.cols)
        ? { type: value.type, terminal_id: value.terminal_id, rows: value.rows, cols: value.cols }
        : null
    case 'terminal.binding_snapshot': {
      if (typeof value.prefix !== 'string' || !nullableString(value.prefix2) || !Array.isArray(value.bindings)) return null
      const bindings = value.bindings.map(binding)
      return bindings.every((item): item is TerminalBinding => item !== null)
        ? { type: value.type, terminal_id: value.terminal_id, prefix: value.prefix, prefix2: value.prefix2, bindings }
        : null
    }
    case 'terminal.error':
      return nonEmptyString(value.code) && typeof value.message === 'string'
        ? { type: value.type, terminal_id: value.terminal_id, code: value.code, message: value.message }
        : null
    case 'terminal.closed':
      return terminalCloseReason(value.reason)
        ? { type: value.type, terminal_id: value.terminal_id, reason: value.reason }
        : null
    case 'terminal.action_result':
      return uuid(value.action_id) && typeof value.ok === 'boolean' && hasOwn(value, 'error_code') && nullableString(value.error_code)
        ? { type: value.type, terminal_id: value.terminal_id, action_id: value.action_id, ok: value.ok, error_code: value.error_code }
        : null
    default:
      return null
  }
}
