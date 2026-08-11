import { clientRoutes, type ClientRuntime } from '@termflow/client-ui'
import { createMemoryHistory, createRouter, createWebHashHistory } from 'vue-router'
import NativeConnectView from './views/NativeConnectView.vue'
import NativeDeviceAuthorizeView from './views/NativeDeviceAuthorizeView.vue'

const AUTH_PROBE_TIMEOUT_MS = 8_000

function withTimeout<T>(promise: Promise<T>, milliseconds: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(() => reject(new Error('authorization_probe_timeout')), milliseconds)
    promise.then(
      (value) => { globalThis.clearTimeout(timer); resolve(value) },
      (error) => { globalThis.clearTimeout(timer); reject(error) },
    )
  })
}

export function createTauriRouter(runtime: ClientRuntime) {
  const routes = [
    { path: '/connect', component: NativeConnectView, meta: { bare: true } },
    { path: '/connect/device', component: NativeDeviceAuthorizeView, meta: { bare: true } },
    { path: '/login', redirect: '/connect', meta: { bare: true } },
    ...clientRoutes.filter((route) => route.path !== '/login' && route.path !== '/authorize' && route.meta?.webOnly !== true),
  ]
  const router = createRouter({ history: import.meta.env.VITEST ? createMemoryHistory() : createWebHashHistory(), routes })
  router.beforeEach(async (to) => {
    if (!to.meta.requiresAuth) return true
    try {
      // A hung native command must never leave the app on a blank page: if the
      // probe does not settle in time, treat the session as absent and hand the
      // user the connect screen instead of waiting forever.
      await withTimeout(runtime.api.dashboard.get(), AUTH_PROBE_TIMEOUT_MS)
      return true
    } catch { return { path: '/connect', query: { redirect: to.fullPath } } }
  })
  return router
}
