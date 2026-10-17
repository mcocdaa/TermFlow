<template>
  <a class="skip-link" href="#main-content">{{ t('app.skipToMain') }}</a>
  <div class="app-shell" :class="{ 'is-terminal': terminalLayout, 'is-bare': bareLayout }">
    <header v-if="!terminalLayout && !bareLayout" class="app-header">
      <RouterLink class="brand" to="/" :aria-label="t('app.brandAria')">TermFlow</RouterLink>
      <span class="header-context">{{ t('app.headerContext') }}</span>
      <ThemePicker />
      <button
        data-action="toggle-locale"
        class="text-button locale-toggle-button"
        type="button"
        :aria-label="t('app.switchLangAria')"
        :title="t('app.switchLangAria')"
        @click="toggleLocale"
      >
        <Globe :size="15" aria-hidden="true" />
        <span class="locale-badge">{{ t('app.switchLangBadge') }}</span>
      </button>
      <button
        v-if="sessionState.authenticated"
        data-action="logout"
        class="text-button logout-button"
        type="button"
        :aria-label="t('app.logout')"
        :title="t('app.logout')"
        @click="logout"
      >
        <LogOut :size="18" aria-hidden="true" />
      </button>
    </header>
    <aside v-if="!terminalLayout && !bareLayout" class="side-nav" :aria-label="t('nav.mainAria')">
      <RouterLink to="/" :aria-label="t('nav.dashboard')"><LayoutDashboard :size="18" aria-hidden="true" /><span class="nav-label">{{ t('nav.dashboard') }}</span></RouterLink>
      <RouterLink to="/computers" :aria-label="t('nav.computers')"><MonitorCog :size="18" aria-hidden="true" /><span class="nav-label">{{ t('nav.computers') }}</span></RouterLink>
      <RouterLink to="/settings" :aria-label="t('nav.settings')"><Settings :size="18" aria-hidden="true" /><span class="nav-label">{{ t('nav.settings') }}</span></RouterLink>
      <RouterLink v-if="agentBrokerEnabled" to="/agent" :aria-label="t('nav.agent')"><Bot :size="18" aria-hidden="true" /><span class="nav-label">{{ t('nav.agent') }}</span></RouterLink>
    </aside>
    <main id="main-content" tabindex="-1"><RouterView :key="routeViewKey" /></main>
    <nav v-if="!terminalLayout && !bareLayout" class="mobile-nav" :class="{ 'mobile-nav--agent': agentBrokerEnabled }" :aria-label="t('nav.mobileAria')">
      <RouterLink to="/" :aria-label="t('nav.dashboard')"><LayoutDashboard :size="18" aria-hidden="true" /><span class="nav-label">{{ t('nav.dashboard') }}</span></RouterLink>
      <RouterLink to="/computers" :aria-label="t('nav.computers')"><MonitorCog :size="18" aria-hidden="true" /><span class="nav-label">{{ t('nav.computers') }}</span></RouterLink>
      <RouterLink to="/settings" :aria-label="t('nav.settings')"><Settings :size="18" aria-hidden="true" /><span class="nav-label">{{ t('nav.settings') }}</span></RouterLink>
      <RouterLink v-if="agentBrokerEnabled" to="/agent" :aria-label="t('nav.agent')"><Bot :size="18" aria-hidden="true" /><span class="nav-label">{{ t('nav.agent') }}</span></RouterLink>
    </nav>
    <BottomToast />
    <AgentSensitiveReauthDialog />
  </div>
</template>

<script setup lang="ts">
import { Bot, Globe, LayoutDashboard, LogOut, MonitorCog, Settings } from '@lucide/vue'
import { computed, onMounted, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import ThemePicker from './components/settings/ThemePicker.vue'
import BottomToast from './components/common/BottomToast.vue'
import AgentSensitiveReauthDialog from './components/agent/AgentSensitiveReauthDialog.vue'
import { useAgentBroker } from './composables/useAgentBroker'
import { useSession } from './composables/useSession'
import { useTerminalPageLock } from './composables/useTerminalPageLock'
import { useI18n } from './i18n'

const { t, toggleLocale } = useI18n()
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
