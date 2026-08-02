import { base64Url } from './pkce'
import type { NativeKeyPort } from './ports'

export type { NativeKeyPort } from './ports'

export interface DpopProofOptions {
  key: NativeKeyPort
  method: string
  url: string
  nonce?: string
  accessToken?: string
  now?: () => number
  createId: () => string
}

function encodeJson(value: unknown): string {
  return base64Url(new TextEncoder().encode(JSON.stringify(value)))
}

async function sha256(value: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))
}

export async function createDpopProof(options: DpopProofOptions): Promise<string> {
  const target = new URL(options.url)
  if (target.username !== '' || target.password !== '') throw new Error('dpop_url_invalid')
  target.search = ''
  target.hash = ''
  const header = encodeJson({ typ: 'dpop+jwt', alg: 'ES256', jwk: await options.key.publicJwk() })
  const claims: Record<string, string | number> = {
    jti: options.createId(),
    htm: options.method.toUpperCase(),
    htu: target.toString(),
    iat: Math.floor((options.now?.() ?? Date.now()) / 1_000),
  }
  if (options.nonce !== undefined) claims.nonce = options.nonce
  if (options.accessToken !== undefined) claims.ath = base64Url(await sha256(options.accessToken))
  const payload = encodeJson(claims)
  const signingInput = new TextEncoder().encode(`${header}.${payload}`)
  const signature = base64Url(await options.key.signJwt(signingInput))
  return `${header}.${payload}.${signature}`
}
