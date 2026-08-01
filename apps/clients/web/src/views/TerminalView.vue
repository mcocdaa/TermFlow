<template>
  <section class="terminal-view" aria-labelledby="terminal-title">
    <h1 id="terminal-title" class="sr-only">远程终端</h1>
    <TerminalCanvas :term-id="termId" />
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import TerminalCanvas from '../components/terminal/TerminalCanvas.vue'
const route = useRoute()
const termId = computed(() => String(route.params.termId))
</script>
