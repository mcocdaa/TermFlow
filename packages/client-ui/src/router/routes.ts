import type { RouteRecordRaw } from 'vue-router'
import ComputersView from '../views/ComputersView.vue'
import DashboardView from '../views/DashboardView.vue'
import LoginView from '../views/LoginView.vue'
import NativeAuthorizeView from '../views/NativeAuthorizeView.vue'
import NotFoundView from '../views/NotFoundView.vue'
import TerminalView from '../views/TerminalView.vue'
import SettingsView from '../views/SettingsView.vue'
import TotpActivationView from '../views/TotpActivationView.vue'

export const clientRoutes: RouteRecordRaw[] = [
  { path: '/login', component: LoginView, meta: { bare: true } },
  { path: '/authorize', component: NativeAuthorizeView, meta: { bare: true } },
  { path: '/', component: DashboardView, meta: { requiresAuth: true } },
  { path: '/computers', component: ComputersView, meta: { requiresAuth: true } },
  { path: '/settings', component: SettingsView, meta: { requiresAuth: true } },
  { path: '/settings/two-factor-auth', component: TotpActivationView, meta: { requiresAuth: true, webOnly: true } },
  { path: '/terms/:termId', component: TerminalView, meta: { requiresAuth: true, terminal: true } },
  { path: '/:pathMatch(.*)*', component: NotFoundView },
]
