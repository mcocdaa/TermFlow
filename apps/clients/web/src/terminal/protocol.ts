import type { BindingSnapshotDto } from '../api/types'

export interface TerminalReadyControl {
  type: 'terminal.ready'
  terminal_id: string
  stream_id: string
  rows: number
  cols: number
  gap?: boolean
}
export interface TerminalSizeControl { type: 'terminal.size'; rows: number; cols: number }
export interface TerminalBindingControl extends BindingSnapshotDto { type: 'terminal.binding_snapshot' }
export interface TerminalErrorControl { type: 'terminal.error'; code: string; message?: string }
export interface TerminalClosedControl { type: 'terminal.closed'; reason: string }
export interface TerminalActionResultControl { type: 'terminal.action_result'; request_id?: string; action_id: string; ok: boolean; error?: string }
export type TerminalControl = TerminalReadyControl | TerminalSizeControl | TerminalBindingControl | TerminalErrorControl | TerminalClosedControl | TerminalActionResultControl

const positiveInteger = (value: unknown): value is number => Number.isInteger(value) && Number(value) > 0

export function parseTerminalControl(value: string): TerminalControl | null {
  let data: unknown
  try { data = JSON.parse(value) } catch { return null }
  if (!data || typeof data !== 'object' || !('type' in data) || typeof data.type !== 'string') return null
  const control = data as Record<string, unknown>
  switch (control.type) {
    case 'terminal.ready':
      return typeof control.terminal_id === 'string' && typeof control.stream_id === 'string' && positiveInteger(control.rows) && positiveInteger(control.cols)
        ? control as unknown as TerminalReadyControl : null
    case 'terminal.size':
      return positiveInteger(control.rows) && positiveInteger(control.cols) ? control as unknown as TerminalSizeControl : null
    case 'terminal.binding_snapshot':
      return typeof control.prefix === 'string' && !!control.actions && typeof control.actions === 'object' ? control as unknown as TerminalBindingControl : null
    case 'terminal.error':
      return typeof control.code === 'string' ? control as unknown as TerminalErrorControl : null
    case 'terminal.closed':
      return typeof control.reason === 'string' ? control as unknown as TerminalClosedControl : null
    case 'terminal.action_result':
      return typeof control.action_id === 'string' && typeof control.ok === 'boolean' ? control as unknown as TerminalActionResultControl : null
    default: return null
  }
}
