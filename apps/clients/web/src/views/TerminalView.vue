<template>
  <section class="terminal-view" aria-labelledby="terminal-title">
    <h1 id="terminal-title" class="sr-only">远程终端</h1>
    <TerminalTitlebar :title="termName" v-model:display-mode="displayMode"><PaneFocusMenu :panes="panes" @focus="terminalCanvas?.focusPane($event)" @reset="terminalCanvas?.resetViewport()" /></TerminalTitlebar>
    <TerminalCanvas ref="terminalCanvas" :term-id="termId" :display-mode="displayMode" />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import TerminalCanvas from '../components/terminal/TerminalCanvas.vue'
import TerminalTitlebar from '../components/terminal/TerminalTitlebar.vue'
import type { DisplayMode } from '../terminal/viewport'
import { getTerm } from '../api/terms'
import type { PaneTopologyDto } from '../api/types'
import PaneFocusMenu from '../components/terminal/PaneFocusMenu.vue'
const route = useRoute()
const termId = computed(() => String(route.params.termId))
const displayMode = ref<DisplayMode>('font-100')
const termName = ref(`Term · ${termId.value}`)
const panes = ref<PaneTopologyDto[]>([])
const terminalCanvas = ref<InstanceType<typeof TerminalCanvas> | null>(null)
const controller = new AbortController()
onMounted(async () => {
  try {
    const term = await getTerm(termId.value, controller.signal)
    termName.value = term.name
    panes.value = term.windows.flatMap((window) => window.panes)
  } catch { /* terminal WebSocket reports its own actionable state */ }
})
onBeforeUnmount(() => controller.abort())
</script>
