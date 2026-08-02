import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../http/apiError'
import { requestWithDpopNonce } from './dpopRequest'

describe('requestWithDpopNonce', () => {
  it('retries exactly once with the server nonce', async () => {
    const proof = vi.fn().mockResolvedValueOnce('proof-1').mockResolvedValueOnce('proof-2')
    const request = vi.fn()
      .mockRejectedValueOnce(new ApiError('authentication', { code: 'use_dpop_nonce', dpopNonce: 'nonce-1' }))
      .mockResolvedValueOnce({ ok: true })

    await expect(requestWithDpopNonce(proof, request)).resolves.toEqual({ ok: true })
    expect(proof.mock.calls).toEqual([[], ['nonce-1']])
    expect(request.mock.calls).toEqual([['proof-1'], ['proof-2']])
  })

  it('does not loop after a second nonce challenge', async () => {
    const challenge = new ApiError('authentication', { code: 'use_dpop_nonce', dpopNonce: 'nonce-1' })
    const request = vi.fn().mockRejectedValue(challenge)

    await expect(requestWithDpopNonce(vi.fn().mockResolvedValue('proof'), request)).rejects.toBe(challenge)
    expect(request).toHaveBeenCalledTimes(2)
  })
})
