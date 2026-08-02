import { createMemoryHistory, createRouter, createWebHistory, type RouterHistory } from 'vue-router'
import { clientRoutes } from '@termflow/client-ui'

export interface RouterDependencies {
  sessionStatus: () => Promise<{ authenticated: boolean }>
  history?: RouterHistory
}

export function createAppRouter(dependencies: RouterDependencies) {
  const router = createRouter({
    history: dependencies.history ?? (import.meta.env.VITEST ? createMemoryHistory() : createWebHistory()),
    routes: clientRoutes,
  })
  router.beforeEach(async (to) => {
    if (!to.meta.requiresAuth) return true
    const session = await dependencies.sessionStatus()
    return session.authenticated ? true : { path: '/login', query: { redirect: to.fullPath } }
  })
  return router
}
