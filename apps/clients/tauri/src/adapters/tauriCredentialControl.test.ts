import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ invoke: vi.fn() }))

vi.mock('@tauri-apps/api/core', () => ({ invoke: mocks.invoke }))

import { clearNativeCredentials } from './tauriCredentialControl'

describe('clearNativeCredentials', () => {
  it('asks Rust to delete only the refresh credential for its issuer', async () => {
    await clearNativeCredentials('https://relay.example')

    expect(mocks.invoke).toHaveBeenCalledWith('native_clear_credentials', { issuer: 'https://relay.example' })
  })
})
