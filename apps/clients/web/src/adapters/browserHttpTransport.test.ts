import { describe, expect, it, vi } from 'vitest'
import { HttpTransportError } from '@termflow/client-core'
import { createBrowserHttpTransport } from './browserHttpTransport'

describe('createBrowserHttpTransport', () => {
  it('uses a relative URL, same-origin cookies, JSON, and abort signals', async () => {
    const controller = new AbortController()
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
    const transport = createBrowserHttpTransport(fetchMock)

    const result = await transport.request('/api/v1/dashboard', {
      method: 'POST',
      body: { view: 'all' },
      signal: controller.signal,
    })

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/dashboard', expect.objectContaining({
      body: JSON.stringify({ view: 'all' }),
      credentials: 'same-origin',
      method: 'POST',
      signal: controller.signal,
    }))
    expect(fetchMock.mock.calls[0][0]).not.toMatch(/^https?:/)
    expect(result.body).toEqual({ ok: true })
  })

  it('does not parse non-JSON bodies', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('secret proxy page', {
      status: 502,
      headers: { 'content-type': 'text/html' },
    }))

    await expect(createBrowserHttpTransport(fetchMock).request('/api/v1/dashboard', { method: 'GET' }))
      .resolves.toMatchObject({ status: 502, body: undefined })
  })

  it('rejects absolute and browser-normalized protocol-relative paths before fetch', async () => {
    const fetchMock = vi.fn()
    const transport = createBrowserHttpTransport(fetchMock)

    await expect(transport.request('https://evil.example/api' as `/${string}`, { method: 'GET' }))
      .rejects.toMatchObject({ kind: 'invalid_request' })
    await expect(transport.request('//evil.example/api', { method: 'GET' }))
      .rejects.toMatchObject({ kind: 'invalid_request' })
    await expect(transport.request('/\\evil.example/api', { method: 'GET' }))
      .rejects.toMatchObject({ kind: 'invalid_request' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('maps browser abort and network failures to transport errors', async () => {
    const aborted = createBrowserHttpTransport(vi.fn().mockRejectedValue(new DOMException('cancelled', 'AbortError')))
    const offline = createBrowserHttpTransport(vi.fn().mockRejectedValue(new TypeError('network failed')))

    await expect(aborted.request('/api/v1/dashboard', { method: 'GET' }))
      .rejects.toEqual(new HttpTransportError('aborted'))
    await expect(offline.request('/api/v1/dashboard', { method: 'GET' }))
      .rejects.toEqual(new HttpTransportError('offline'))
  })
})
