import { describe, expect, it, vi } from 'vitest'
import { NativeTokenSession } from './tokenSession'
import type { CredentialVaultPort } from './ports'

describe('NativeTokenSession', () => {
  it('refreshes early and atomically replaces the rotated refresh token', async () => {
    const vault: CredentialVaultPort = {
      load: vi.fn().mockResolvedValue({ accessToken: 'old', expiresAt: '2026-08-02T12:00:30Z', tokenType: 'DPoP' }),
      replace: vi.fn().mockResolvedValue(undefined),
      clear: vi.fn().mockResolvedValue(undefined),
    }
    const refresh = vi.fn().mockResolvedValue({ accessToken: 'new', expiresAt: '2026-08-02T12:10:00Z', tokenType: 'DPoP' })
    const session = new NativeTokenSession({ issuer: 'https://b.example', vault, refresh, now: () => Date.parse('2026-08-02T12:00:00Z') })

    await expect(session.accessToken()).resolves.toBe('new')
    expect(refresh).toHaveBeenCalledWith('https://b.example')
    expect(vault.replace).toHaveBeenCalledWith('https://b.example', expect.objectContaining({ accessToken: 'new' }))
  })

  it('clears credentials after invalid_grant', async () => {
    const vault: CredentialVaultPort = {
      load: vi.fn().mockResolvedValue({ accessToken: 'old', expiresAt: '2026-08-02T12:00:00Z', tokenType: 'DPoP' }),
      replace: vi.fn().mockResolvedValue(undefined),
      clear: vi.fn().mockResolvedValue(undefined),
    }
    const session = new NativeTokenSession({
      issuer: 'https://b.example', vault, now: () => Date.parse('2026-08-02T12:00:00Z'),
      refresh: vi.fn().mockRejectedValue(Object.assign(new Error('safe'), { code: 'invalid_grant' })),
    })

    await expect(session.accessToken()).rejects.toThrow('native_authorization_required')
    expect(vault.clear).toHaveBeenCalledWith('https://b.example')
  })
})
