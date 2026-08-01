<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell" :class="{ 'is-terminal': terminalLayout }">
    <header v-if="!terminalLayout" class="app-header">
      <RouterLink class="brand" to="/" aria-label="TermFlow 控制中心">TermFlow</RouterLink>
      <span class="header-context">远程终端控制</span>
      <ThemePicker />
      <button v-if="sessionState.authenticated" class="text-button" type="button" @click="logout">退出</button>
    </header>
    <aside v-if="!terminalLayout" class="side-nav" aria-label="主导航">
      <RouterLink to="/">控制中心</RouterLink>
      <RouterLink to="/computers">电脑管理</RouterLink>
    </aside>
    <main id="main-content" tabindex="-1"><RouterView /></main>
    <nav v-if="!terminalLayout" class="mobile-nav" aria-label="移动端导航">
      <RouterLink to="/">控制中心</RouterLink>
      <RouterLink to="/computers">电脑管理</RouterLink>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import ThemePicker from './components/settings/ThemePicker.vue'
import { logoutSession, sessionState } from './stores/session'

const router = useRouter()
const route = useRoute()
const terminalLayout = computed(() => route.meta.terminal === true)
async function logout() {
  await logoutSession()
  await router.replace('/login')
}
</script>
