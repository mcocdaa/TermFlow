const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'

export interface AuthCryptoPort {
  randomBytes(length: number): Uint8Array
  sha256(input: Uint8Array): Promise<Uint8Array>
}

export interface PkcePair {
  verifier: string
  challenge: string
  method: 'S256'
}

export function base64Url(bytes: Uint8Array): string {
  let result = ''
  for (let index = 0; index < bytes.length; index += 3) {
    const first = bytes[index] ?? 0
    const second = bytes[index + 1]
    const third = bytes[index + 2]
    const value = (first << 16) | ((second ?? 0) << 8) | (third ?? 0)
    result += ALPHABET[(value >>> 18) & 63]
    result += ALPHABET[(value >>> 12) & 63]
    if (second !== undefined) result += ALPHABET[(value >>> 6) & 63]
    if (third !== undefined) result += ALPHABET[value & 63]
  }
  return result
}

export async function createPkce(cryptoPort: AuthCryptoPort): Promise<PkcePair> {
  const verifier = base64Url(cryptoPort.randomBytes(32))
  const challenge = base64Url(await cryptoPort.sha256(new TextEncoder().encode(verifier)))
  return { verifier, challenge, method: 'S256' }
}
