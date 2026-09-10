import { describe, expect, it } from 'vitest'
import { createApiClient } from '../http/apiClient'

describe('cleanup deletion results', () => {
  for (const target of ['term', 'computer', 'conversation'] as const) {
    for (const status of [202, 204]) {
      it(`${target} preserves ${status} semantics`, async () => {
        const api = createApiClient({ request: async () => ({ status, headers: { get: () => null },
          body: status === 202 ? { state: 'deletion_pending', cleanup_job_id: 'job-1', status_url: '/jobs/job-1' } : undefined }) })
        const remove = target === 'term' ? api.terms.remove : target === 'computer' ? api.computers.remove : api.agents.deleteConversation
        expect(await remove('target')).toEqual(status === 204 ? { state: 'completed' } : {
          state: 'deletion_pending', cleanupJobId: 'job-1', statusUrl: '/jobs/job-1',
        })
      })
    }
  }
})
