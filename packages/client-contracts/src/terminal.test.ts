import { describe, expect, it } from 'vitest'
import { parseTerminalControl } from './terminal'

const TERMINAL_ID = '11111111-1111-4111-8111-111111111111'
const STREAM_ID = '22222222-2222-4222-8222-222222222222'
const ACTION_ID = '33333333-3333-4333-8333-333333333333'

describe('parseTerminalControl', () => {
  it.each([
    { type: 'terminal.ready', terminal_id: TERMINAL_ID, stream_id: STREAM_ID, rows: 24, cols: 80 },
    { type: 'terminal.size', terminal_id: TERMINAL_ID, rows: 30, cols: 100 },
    {
      type: 'terminal.binding_snapshot',
      terminal_id: TERMINAL_ID,
      prefix: 'C-b',
      prefix2: null,
      bindings: [{ action: 'copy_mode', key: 'C-b [', tooltip: '复制' }],
    },
    { type: 'terminal.error', terminal_id: TERMINAL_ID, code: 'failed', message: 'Failed safely.' },
    { type: 'terminal.closed', terminal_id: TERMINAL_ID, reason: 'stream_gap' },
    {
      type: 'terminal.action_result',
      terminal_id: TERMINAL_ID,
      action_id: ACTION_ID,
      ok: false,
      error_code: 'target_not_found',
    },
  ])('accepts the server control $type', (control) => {
    expect(parseTerminalControl(JSON.stringify(control))).toEqual(control)
  })

  it.each([
    '{not-json',
    JSON.stringify(null),
    JSON.stringify({ type: 'terminal.unknown', terminal_id: TERMINAL_ID }),
    JSON.stringify({ type: 'terminal.ready', terminal_id: TERMINAL_ID, stream_id: STREAM_ID, rows: 0, cols: 80 }),
    JSON.stringify({ type: 'terminal.ready', terminal_id: 'not-a-uuid', stream_id: STREAM_ID, rows: 24, cols: 80 }),
    JSON.stringify({ type: 'terminal.ready', terminal_id: TERMINAL_ID, stream_id: 'not-a-uuid', rows: 24, cols: 80 }),
    JSON.stringify({ type: 'terminal.size', terminal_id: '', rows: 24, cols: 80 }),
    JSON.stringify({ type: 'terminal.binding_snapshot', terminal_id: TERMINAL_ID, prefix: 'C-b', prefix2: null, bindings: [{ action: 'unknown', key: null, tooltip: 'bad' }] }),
    JSON.stringify({ type: 'terminal.error', terminal_id: TERMINAL_ID, code: 'bad' }),
    JSON.stringify({ type: 'terminal.closed', terminal_id: TERMINAL_ID, reason: 'made_up' }),
    JSON.stringify({ type: 'terminal.action_result', terminal_id: TERMINAL_ID, action_id: ACTION_ID, ok: true }),
    JSON.stringify({ type: 'terminal.action_result', terminal_id: TERMINAL_ID, action_id: 'not-a-uuid', ok: true, error_code: null }),
  ])('rejects malformed or unknown control %s', (text) => {
    expect(parseTerminalControl(text)).toBeNull()
  })
})
