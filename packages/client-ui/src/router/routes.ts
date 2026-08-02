import type { RouteRecordRaw } from 'vue-router'
import ComputersView from '../views/ComputersView.vue'
import DashboardView from '../views/DashboardView.vue'
import LoginView from '../views/LoginView.vue'
import NotFoundView from '../views/NotFoundView.vue'
import TerminalView from '../views/TerminalView.vue'

export const clientRoutes: RouteRecordRaw[] = [
  { path: '/login', component: LoginView, meta: { bare: true } },
  { path: '/', component: DashboardView, meta: { requiresAuth: true } },
  { path: '/computers', component: ComputersView, meta: { requiresAuth: true } },
  { path: '/terms/:termId', component: TerminalView, meta: { requiresAuth: true, terminal: true } },
  { path: '/:pathMatch(.*)*', component: NotFoundView },
]
