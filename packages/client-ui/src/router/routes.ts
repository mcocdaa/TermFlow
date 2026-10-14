import type { RouteRecordRaw } from 'vue-router'

export const clientRoutes: RouteRecordRaw[] = [
  { path: '/agent-preview', component: () => import('../views/AgentPreviewView.vue'), meta: { bare: true } },
  { path: '/login', component: () => import('../views/LoginView.vue'), meta: { bare: true } },
  { path: '/authorize', component: () => import('../views/NativeAuthorizeView.vue'), meta: { bare: true } },
  { path: '/device', component: () => import('../views/DeviceAuthorizeView.vue'), meta: { bare: true, requiresAuth: true } },
  { path: '/', component: () => import('../views/DashboardView.vue'), meta: { requiresAuth: true } },
  { path: '/computers', component: () => import('../views/ComputersView.vue'), meta: { requiresAuth: true } },
  { path: '/settings', component: () => import('../views/SettingsView.vue'), meta: { requiresAuth: true } },
  { path: '/settings/two-factor-auth', component: () => import('../views/TotpActivationView.vue'), meta: { requiresAuth: true, webOnly: true } },
  { path: '/agent', component: () => import('../views/AgentView.vue'), meta: { requiresAuth: true } },
  { path: '/agent/:conversationId', component: () => import('../views/AgentChatView.vue'), meta: { requiresAuth: true } },
  { path: '/terms/:termId', component: () => import('../views/TerminalView.vue'), meta: { requiresAuth: true, terminal: true } },
  { path: '/:pathMatch(.*)*', component: () => import('../views/NotFoundView.vue') },
]
