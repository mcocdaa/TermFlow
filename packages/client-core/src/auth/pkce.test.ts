import { describe, expect, it } from 'vitest'
import { createPkce, type AuthCryptoPort } from './pkce'

const cryptoPort: AuthCryptoPort = {
  randomBytes: (length) => Uint8Array.from({ length }, (_, index) => index),
  sha256: async (input) => new Uint8Array(await crypto.subtle.digest('SHA-256', input.slice().buffer)),
}

describe('createPkce', () => {
  it('creates a 43-character verifier and its S256 challenge', async () => {
    const result = await createPkce(cryptoPort)

    expect(result.verifier).toHaveLength(43)
    expect(result.challenge).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(result.method).toBe('S256')
  })
})
