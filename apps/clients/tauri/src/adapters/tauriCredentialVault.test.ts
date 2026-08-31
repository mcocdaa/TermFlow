import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ invoke: vi.fn() }))

vi.mock('@tauri-apps/api/core', () => ({ invoke: mocks.invoke }))

import { createTauriCredentialVault } from './tauriCredentialVault'

describe('createTauriCredentialVault', () => {
  it('clears only the native credential for its issuer and never exposes keyring data', async () => {
    const vault = createTauriCredentialVault()
    await vault.replace('https://relay.example', {
      accessToken: 'short-lived', expiresAt: '2026-08-05T12:00:00Z', tokenType: 'DPoP',
    })

    await vault.clear('https://relay.example')

    await expect(vault.load('https://relay.example')).resolves.toBeNull()
    expect(mocks.invoke).toHaveBeenCalledWith('native_clear_credentials', { issuer: 'https://relay.example' })
  })
})
