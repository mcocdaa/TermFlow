import { describe, expect, it } from 'vitest'
import AgentChatView from '../views/AgentChatView.vue'
import AgentView from '../views/AgentView.vue'
import { clientRoutes } from './routes'

describe('client route table', () => {
  it('mounts the Agent overview and chat views behind the auth guard', () => {
    const overview = clientRoutes.find((route) => route.path === '/agent')
    const chat = clientRoutes.find((route) => route.path === '/agent/:conversationId')

    expect(overview?.meta?.requiresAuth).toBe(true)
    expect(overview?.component).toBe(AgentView)
    expect(chat?.meta?.requiresAuth).toBe(true)
    expect(chat?.component).toBe(AgentChatView)
  })

  it('keeps the catch-all route last so /agent resolves first', () => {
    const index = clientRoutes.findIndex((route) => route.path === '/agent')
    const catchAll = clientRoutes.findIndex((route) => route.path === '/:pathMatch(.*)*')
    expect(index).toBeGreaterThan(-1)
    expect(index).toBeLessThan(catchAll)
  })
})
