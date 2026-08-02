import { clientRoutes, type ClientRuntime } from '@termflow/client-ui'
import { createMemoryHistory, createRouter, createWebHashHistory } from 'vue-router'
import NativeConnectView from './views/NativeConnectView.vue'

export function createTauriRouter(runtime: ClientRuntime) {
  const routes = [
    { path: '/connect', component: NativeConnectView, meta: { bare: true } },
    { path: '/login', redirect: '/connect', meta: { bare: true } },
    ...clientRoutes.filter((route) => route.path !== '/login' && route.path !== '/authorize'),
  ]
  const router = createRouter({ history: import.meta.env.VITEST ? createMemoryHistory() : createWebHashHistory(), routes })
  router.beforeEach(async (to) => {
    if (!to.meta.requiresAuth) return true
    try { await runtime.api.dashboard.get(); return true }
    catch { return { path: '/connect', query: { redirect: to.fullPath } } }
  })
  return router
}
