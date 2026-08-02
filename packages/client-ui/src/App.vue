<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell" :class="{ 'is-terminal': terminalLayout, 'is-bare': bareLayout }">
    <header v-if="!terminalLayout && !bareLayout" class="app-header">
      <RouterLink class="brand" to="/" aria-label="TermFlow 控制中心">TermFlow</RouterLink>
      <span class="header-context">远程终端控制</span>
      <ThemePicker />
      <button
        v-if="sessionState.authenticated"
        data-action="logout"
        class="text-button logout-button"
        type="button"
        aria-label="退出登录"
        title="退出登录"
        @click="logout"
      >
        <LogOut :size="18" aria-hidden="true" />
        <span class="logout-label">退出</span>
      </button>
    </header>
    <aside v-if="!terminalLayout && !bareLayout" class="side-nav" aria-label="主导航">
      <RouterLink to="/"><LayoutDashboard :size="18" aria-hidden="true" />控制中心</RouterLink>
      <RouterLink to="/computers"><MonitorCog :size="18" aria-hidden="true" />电脑管理</RouterLink>
      <RouterLink to="/settings"><Settings :size="18" aria-hidden="true" />设置</RouterLink>
    </aside>
    <main id="main-content" tabindex="-1"><RouterView :key="routeViewKey" /></main>
    <nav v-if="!terminalLayout && !bareLayout" class="mobile-nav" aria-label="移动端导航">
      <RouterLink to="/"><LayoutDashboard :size="18" aria-hidden="true" />控制中心</RouterLink>
      <RouterLink to="/computers"><MonitorCog :size="18" aria-hidden="true" />电脑管理</RouterLink>
      <RouterLink to="/settings"><Settings :size="18" aria-hidden="true" />设置</RouterLink>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { LayoutDashboard, LogOut, MonitorCog, Settings } from '@lucide/vue'
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import ThemePicker from './components/settings/ThemePicker.vue'
import { useSession } from './composables/useSession'
import { useTerminalPageLock } from './composables/useTerminalPageLock'

const router = useRouter()
const route = useRoute()
const { logoutSession, sessionState } = useSession()
const terminalLayout = computed(() => route.meta.terminal === true)
const bareLayout = computed(() => route.meta.bare === true)
useTerminalPageLock(terminalLayout)
const routeViewKey = computed(() => terminalLayout.value ? `term:${String(route.params.termId)}` : 'shared-client-route')
async function logout() {
  await logoutSession()
  await router.replace('/login')
}
</script>
