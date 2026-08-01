<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell">
    <header class="app-header">
      <RouterLink class="brand" to="/" aria-label="TermFlow 控制中心">TermFlow</RouterLink>
      <span class="header-context">远程终端控制</span>
      <ThemePicker />
    </header>
    <aside class="side-nav" aria-label="主导航">
      <RouterLink to="/">控制中心</RouterLink>
      <RouterLink to="/computers">电脑管理</RouterLink>
    </aside>
    <main id="main-content" tabindex="-1"><RouterView /></main>
    <nav class="mobile-nav" aria-label="移动端导航">
      <RouterLink to="/">控制中心</RouterLink>
      <RouterLink to="/computers">电脑管理</RouterLink>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
import ThemePicker from './components/settings/ThemePicker.vue'
</script>
