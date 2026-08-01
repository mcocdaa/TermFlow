import { describe, expect, it, vi } from 'vitest'
import { ApiError, apiRequest } from './http'

describe('apiRequest', () => {
  it('uses only relative public API URLs with same-origin credentials and abort support', async () => {
    const controller = new AbortController()
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await apiRequest<{ ok: boolean }>('/dashboard', { signal: controller.signal })

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/dashboard', expect.objectContaining({
      credentials: 'same-origin',
      signal: controller.signal,
    }))
    expect(fetchMock.mock.calls[0][0]).not.toMatch(/^https?:/)
  })

  it.each([
    [401, 'authentication', '会话已过期，请重新登录。'],
    [422, 'validation', '提交的内容不符合要求。'],
    [429, 'rate_limit', '操作过于频繁，请稍后重试。'],
    [503, 'server', '服务暂时不可用，请稍后重试。'],
  ] as const)('maps status %i to a safe structured error', async (status, kind, message) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: 'internal_secret', message: 'raw server stack trace' },
    }), { status, headers: { 'content-type': 'application/json' } })))

    const error = await apiRequest('/dashboard').catch((caught) => caught) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.kind).toBe(kind)
    expect(error.message).toBe(message)
    expect(error.message).not.toContain('stack trace')
  })

  it('reports network failures without logging request data', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network failed')))
    await expect(apiRequest('/dashboard')).rejects.toMatchObject({ kind: 'offline' })
    expect(consoleSpy).not.toHaveBeenCalled()
  })
})
