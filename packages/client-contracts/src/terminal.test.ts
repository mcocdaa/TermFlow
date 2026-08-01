import { describe, expect, it } from 'vitest'
import { parseTerminalControl } from './terminal'

describe('parseTerminalControl', () => {
  it.each([
    { type: 'terminal.ready', terminal_id: 't1', stream_id: 's1', rows: 24, cols: 80 },
    { type: 'terminal.size', terminal_id: 't1', rows: 30, cols: 100 },
    {
      type: 'terminal.binding_snapshot',
      terminal_id: 't1',
      prefix: 'C-b',
      prefix2: null,
      bindings: [{ action: 'copy_mode', key: 'C-b [', tooltip: '复制' }],
    },
    { type: 'terminal.error', terminal_id: 't1', code: 'failed', message: 'Failed safely.' },
    { type: 'terminal.closed', terminal_id: 't1', reason: 'stream_gap' },
    {
      type: 'terminal.action_result',
      terminal_id: 't1',
      action_id: 'a1',
      ok: false,
      error_code: 'target_not_found',
    },
  ])('accepts the server control $type', (control) => {
    expect(parseTerminalControl(JSON.stringify(control))).toEqual(control)
  })

  it.each([
    '{not-json',
    JSON.stringify(null),
    JSON.stringify({ type: 'terminal.unknown', terminal_id: 't1' }),
    JSON.stringify({ type: 'terminal.ready', terminal_id: 't1', stream_id: 's1', rows: 0, cols: 80 }),
    JSON.stringify({ type: 'terminal.size', terminal_id: '', rows: 24, cols: 80 }),
    JSON.stringify({ type: 'terminal.binding_snapshot', terminal_id: 't1', prefix: 'C-b', prefix2: null, bindings: [{ action: 'unknown', key: null, tooltip: 'bad' }] }),
    JSON.stringify({ type: 'terminal.error', terminal_id: 't1', code: 'bad' }),
    JSON.stringify({ type: 'terminal.closed', terminal_id: 't1', reason: 'made_up' }),
    JSON.stringify({ type: 'terminal.action_result', terminal_id: 't1', action_id: 'a1', ok: true }),
  ])('rejects malformed or unknown control %s', (text) => {
    expect(parseTerminalControl(text)).toBeNull()
  })
})
