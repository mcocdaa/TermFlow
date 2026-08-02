import { createMemoryHistory, createRouter, createWebHistory, type RouterHistory } from 'vue-router'
import { ComputersView, DashboardView, LoginView } from '@termflow/client-ui'
import NotFoundView from './views/NotFoundView.vue'
import TerminalView from './views/TerminalView.vue'

export interface RouterDependencies {
  sessionStatus: () => Promise<{ authenticated: boolean }>
  history?: RouterHistory
}

export function createAppRouter(dependencies: RouterDependencies) {
  const router = createRouter({
    history: dependencies.history ?? (import.meta.env.VITEST ? createMemoryHistory() : createWebHistory()),
    routes: [
      { path: '/login', component: LoginView, meta: { bare: true } },
      { path: '/', component: DashboardView, meta: { requiresAuth: true } },
      { path: '/computers', component: ComputersView, meta: { requiresAuth: true } },
      { path: '/terms/:termId', component: TerminalView, meta: { requiresAuth: true, terminal: true } },
      { path: '/:pathMatch(.*)*', component: NotFoundView },
    ],
  })
  router.beforeEach(async (to) => {
    if (!to.meta.requiresAuth) return true
    const session = await dependencies.sessionStatus()
    return session.authenticated ? true : { path: '/login', query: { redirect: to.fullPath } }
  })
  return router
}
