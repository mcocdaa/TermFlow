<template>
  <section class="terminal-view" aria-labelledby="terminal-title">
    <h1 id="terminal-title" class="sr-only">远程终端</h1>
    <TerminalTitlebar :title="`Term · ${termId}`" v-model:display-mode="displayMode" />
    <TerminalCanvas :term-id="termId" :display-mode="displayMode" />
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import TerminalCanvas from '../components/terminal/TerminalCanvas.vue'
import TerminalTitlebar from '../components/terminal/TerminalTitlebar.vue'
import type { DisplayMode } from '../terminal/viewport'
const route = useRoute()
const termId = computed(() => String(route.params.termId))
const displayMode = ref<DisplayMode>('font-100')
</script>
