import type { RouteRecordRaw } from 'vue-router'
import AgentChatView from '../views/AgentChatView.vue'
import AgentView from '../views/AgentView.vue'
import ComputersView from '../views/ComputersView.vue'
import DashboardView from '../views/DashboardView.vue'
import LoginView from '../views/LoginView.vue'
import NativeAuthorizeView from '../views/NativeAuthorizeView.vue'
import DeviceAuthorizeView from '../views/DeviceAuthorizeView.vue'
import NotFoundView from '../views/NotFoundView.vue'
import TerminalView from '../views/TerminalView.vue'
import SettingsView from '../views/SettingsView.vue'
import TotpActivationView from '../views/TotpActivationView.vue'

export const clientRoutes: RouteRecordRaw[] = [
  { path: '/login', component: LoginView, meta: { bare: true } },
  { path: '/authorize', component: NativeAuthorizeView, meta: { bare: true } },
  { path: '/device', component: DeviceAuthorizeView, meta: { bare: true, requiresAuth: true } },
  { path: '/', component: DashboardView, meta: { requiresAuth: true } },
  { path: '/computers', component: ComputersView, meta: { requiresAuth: true } },
  { path: '/settings', component: SettingsView, meta: { requiresAuth: true } },
  { path: '/settings/two-factor-auth', component: TotpActivationView, meta: { requiresAuth: true, webOnly: true } },
  { path: '/agent', component: AgentView, meta: { requiresAuth: true } },
  { path: '/agent/:conversationId', component: AgentChatView, meta: { requiresAuth: true } },
  { path: '/terms/:termId', component: TerminalView, meta: { requiresAuth: true, terminal: true } },
  { path: '/:pathMatch(.*)*', component: NotFoundView },
]
