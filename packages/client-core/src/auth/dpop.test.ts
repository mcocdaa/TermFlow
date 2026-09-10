import { describe, expect, it } from 'vitest'
import { createDpopProof, type NativeKeyPort } from './dpop'

function decode(segment: string): Record<string, unknown> {
  return JSON.parse(Buffer.from(segment, 'base64url').toString('utf8')) as Record<string, unknown>
}

const key: NativeKeyPort = {
  publicJwk: async () => ({ kty: 'EC', crv: 'P-256', alg: 'ES256', x: 'x', y: 'y' }),
  thumbprint: async () => 'thumbprint',
  signJwt: async () => new Uint8Array([1, 2, 3]),
}

describe('createDpopProof', () => {
  it('binds method, canonical URL, nonce, jti, and access token hash', async () => {
    const proof = await createDpopProof({
      key,
      method: 'POST',
      url: 'https://b.example/api/v1/oauth/token?ignored=1#fragment',
      nonce: 'nonce-1',
      accessToken: 'access-secret',
      now: () => 1_754_000_000_999,
      createId: () => 'jti-1',
      sha256: async input => new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', input.slice().buffer)),
    })
    const [encodedHeader, encodedClaims] = proof.split('.')
    const header = decode(encodedHeader!)
    const claims = decode(encodedClaims!)

    expect(header).toMatchObject({ typ: 'dpop+jwt', alg: 'ES256' })
    expect(claims).toMatchObject({
      htm: 'POST',
      htu: 'https://b.example/api/v1/oauth/token',
      nonce: 'nonce-1',
      jti: 'jti-1',
      iat: 1_754_000_000,
    })
    expect(claims.ath).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(proof.split('.')).toHaveLength(3)
  })
})
