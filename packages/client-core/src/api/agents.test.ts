import { describe, expect, it, vi } from 'vitest'
import { createApiClient } from '../http/apiClient'

describe('Agent conversation title mutation', () => {
  it('sends a PATCH request with the title and returns the updated conversation', async () => {
    const request = vi.fn(async (_path: `/${string}`, _options?: unknown) => ({
      conversation_id: 'conversation-1',
      binding_id: 'binding-1',
      title: '部署检查',
      status: 'active',
      created_at: '2026-09-09T00:00:00Z',
      updated_at: '2026-09-09T00:01:00Z',
    }))
    const api = createApiClient({ request: async (path, options) => ({
      status: 200,
      headers: { get: () => null },
      body: await request(path, options),
    }) })

    const result = await api.agents.renameConversation('conversation-1', '部署检查')

    expect(result.title).toBe('部署检查')
    expect(request).toHaveBeenCalledWith(
      '/api/v1/agent/conversations/conversation-1',
      { method: 'PATCH', body: { title: '部署检查' } },
    )
  })
})
