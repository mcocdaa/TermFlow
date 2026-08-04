import { invoke } from '@tauri-apps/api/core'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { logNativeEvent } from './diagnostics'

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))

describe('native diagnostics', () => {
  beforeEach(() => vi.mocked(invoke).mockReset())

  it('sends only the structured safe event fields', async () => {
    await logNativeEvent({ event: 'metadata_success', issuer: 'https://relay.example', requestId: 'req-1' })
    expect(invoke).toHaveBeenCalledWith('native_log', {
      event: 'metadata_success',
      level: undefined,
      issuer: 'https://relay.example',
      requestId: 'req-1',
      errorCode: undefined,
      errorDetail: undefined,
    })
  })

  it('does not throw when native logging is unavailable', async () => {
    vi.mocked(invoke).mockRejectedValueOnce(new Error('no native runtime'))
    await expect(logNativeEvent({ event: 'connect_started' })).resolves.toBeUndefined()
  })

  it('forwards a bounded diagnostic detail without changing the event shape', async () => {
    await logNativeEvent({ event: 'browser_open_failed', errorCode: 'browser_open_failed', errorDetail: 'Error: shell execute failed' })
    expect(invoke).toHaveBeenCalledWith('native_log', expect.objectContaining({
      errorCode: 'browser_open_failed',
      errorDetail: 'Error: shell execute failed',
    }))
  })
})
