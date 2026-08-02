import { ApiError } from '../http/apiError'

function nonceChallenge(error: unknown): string | null {
  return error instanceof ApiError && error.code === 'use_dpop_nonce' && error.dpopNonce !== undefined
    ? error.dpopNonce
    : null
}

export async function requestWithDpopNonce<T>(
  createProof: (nonce?: string) => Promise<string>,
  request: (proof: string) => Promise<T>,
): Promise<T> {
  try {
    return await request(await createProof())
  } catch (error) {
    const nonce = nonceChallenge(error)
    if (nonce === null) throw error
    return request(await createProof(nonce))
  }
}
