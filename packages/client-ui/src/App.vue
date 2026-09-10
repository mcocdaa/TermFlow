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
      <RouterLink to="/" aria-label="控制中心"><LayoutDashboard :size="18" aria-hidden="true" /><span class="nav-label">控制中心</span></RouterLink>
      <RouterLink to="/computers" aria-label="电脑管理"><MonitorCog :size="18" aria-hidden="true" /><span class="nav-label">电脑管理</span></RouterLink>
      <RouterLink to="/settings" aria-label="设置"><Settings :size="18" aria-hidden="true" /><span class="nav-label">设置</span></RouterLink>
      <RouterLink v-if="agentBrokerEnabled" to="/agent" aria-label="Agent 控制台"><Bot :size="18" aria-hidden="true" /><span class="nav-label">Agent 控制台</span></RouterLink>
    </aside>
    <main id="main-content" tabindex="-1"><RouterView :key="routeViewKey" /></main>
    <nav v-if="!terminalLayout && !bareLayout" class="mobile-nav" :class="{ 'mobile-nav--agent': agentBrokerEnabled }" aria-label="移动端导航">
      <RouterLink to="/" aria-label="控制中心"><LayoutDashboard :size="18" aria-hidden="true" /><span class="nav-label">控制中心</span></RouterLink>
      <RouterLink to="/computers" aria-label="电脑管理"><MonitorCog :size="18" aria-hidden="true" /><span class="nav-label">电脑管理</span></RouterLink>
      <RouterLink to="/settings" aria-label="设置"><Settings :size="18" aria-hidden="true" /><span class="nav-label">设置</span></RouterLink>
      <RouterLink v-if="agentBrokerEnabled" to="/agent" aria-label="Agent 控制台"><Bot :size="18" aria-hidden="true" /><span class="nav-label">Agent 控制台</span></RouterLink>
    </nav>
    <BottomToast />
    <AgentSensitiveReauthDialog />
  </div>
</template>

<script setup lang="ts">
import { Bot, LayoutDashboard, LogOut, MonitorCog, Settings } from '@lucide/vue'
import { computed, onMounted, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import ThemePicker from './components/settings/ThemePicker.vue'
import BottomToast from './components/common/BottomToast.vue'
import AgentSensitiveReauthDialog from './components/agent/AgentSensitiveReauthDialog.vue'
import { useAgentBroker } from './composables/useAgentBroker'
import { useSession } from './composables/useSession'
import { useTerminalPageLock } from './composables/useTerminalPageLock'

const router = useRouter()
const route = useRoute()
const { logoutSession, refreshSession, sessionState } = useSession()
// The Agent navigation entry is capability-gated (M6b spec §4.7); the
// composable fails closed so the link stays hidden until the flag is known.
const { agentBrokerEnabled } = useAgentBroker()
const terminalLayout = computed(() => route.meta.terminal === true)
const bareLayout = computed(() => route.meta.bare === true)
useTerminalPageLock(terminalLayout)
const routeViewKey = computed(() => terminalLayout.value ? `term:${String(route.params.termId)}` : 'shared-client-route')
onMounted(async () => {
  // Vue mounts before the initial browser navigation is necessarily resolved.
  // Wait for it so `/login` is already known to be a bare route before
  // deciding whether a runtime session can be restored.
  await router.isReady()
  // Bare routes, including the browser login page, deliberately do not have a
  // session yet.  Avoid a predictable 401 probe there: it is noisy in the
  // browser and makes an otherwise healthy login view look like a failure.
  if (!bareLayout.value) {
    void refreshSession()
  }
})
watch(bareLayout, (isBare, wasBare) => {
  if (wasBare && !isBare) {
    void refreshSession()
  }
})
async function logout() {
  await logoutSession()
  await router.replace('/login')
}
</script>
