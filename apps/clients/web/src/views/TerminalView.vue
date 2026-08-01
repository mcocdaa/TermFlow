<template>
  <section class="terminal-view" aria-labelledby="terminal-title">
    <h1 id="terminal-title" class="sr-only">远程终端</h1>
    <TerminalTitlebar :title="termName" v-model:display-mode="displayMode">
      <PaneFocusMenu :panes="panes" @focus="terminalCanvas?.focusPane($event)" @reset="terminalCanvas?.resetViewport()" />
      <TmuxActionMenu :bindings="bindings" :active-pane-id="activePane?.pane_id ?? null" @action="runAction" @request-close="closePaneId = $event" />
    </TerminalTitlebar>
    <TerminalCanvas ref="terminalCanvas" :term-id="termId" :display-mode="displayMode" :transform-input="transformInput" @bindings="bindings = $event" @reset-input="modifierResetKey = $event" />
    <MobileKeyBar :prefix="bindings.prefix" :controller="modifiers" :reset-key="modifierResetKey" @input="terminalCanvas?.sendInput($event)" />
    <ClosePaneDialog v-if="closePane" :pane-id="closePane.pane_id" :pane-name="closePane.title || closePane.pane_id" @cancel="closePaneId = null" @confirm="confirmClose" />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import TerminalCanvas from '../components/terminal/TerminalCanvas.vue'
import TerminalTitlebar from '../components/terminal/TerminalTitlebar.vue'
import type { DisplayMode } from '../terminal/viewport'
import { getTermTopology } from '../api/terms'
import type { PaneTopologyDto } from '../api/types'
import PaneFocusMenu from '../components/terminal/PaneFocusMenu.vue'
import TmuxActionMenu from '../components/terminal/TmuxActionMenu.vue'
import MobileKeyBar from '../components/terminal/MobileKeyBar.vue'
import ClosePaneDialog from '../components/terminal/ClosePaneDialog.vue'
import type { BindingSnapshotDto } from '../api/types'
import { MobileModifierController } from '../terminal/modifiers'
import type { TerminalActionId } from '../api/types'
const route = useRoute()
const termId = computed(() => String(route.params.termId))
const displayMode = ref<DisplayMode>('font-100')
const termName = ref(`Term · ${termId.value}`)
const panes = ref<PaneTopologyDto[]>([])
const terminalCanvas = ref<InstanceType<typeof TerminalCanvas> | null>(null)
const bindings = ref<BindingSnapshotDto>({ prefix: '未报告', bindings: [] })
const modifiers = new MobileModifierController()
const modifierResetKey = ref(0)
const closePaneId = ref<string | null>(null)
const activePane = computed(() => panes.value.find((pane) => pane.active) ?? panes.value[0])
const closePane = computed(() => panes.value.find((pane) => pane.pane_id === closePaneId.value) ?? (closePaneId.value ? { pane_id: closePaneId.value, title: closePaneId.value } as PaneTopologyDto : null))
const transformInput = (value: string | Uint8Array) => typeof value === 'string' ? modifiers.consume(value) : value
function runAction(actionId: TerminalActionId, paneId: string | null) { terminalCanvas.value?.sendAction(actionId, { targetPaneId: paneId ?? undefined }) }
function confirmClose(payload: { paneId: string; confirmed: true }) { terminalCanvas.value?.sendAction('close_pane', { targetPaneId: payload.paneId, confirmed: true }); closePaneId.value = null }
const controller = new AbortController()
onMounted(async () => {
  try {
    const response = await getTermTopology(termId.value, controller.signal)
    termName.value = response.topology.session_name
    panes.value = response.topology.windows.flatMap((window) => window.panes)
  } catch { /* terminal WebSocket reports its own actionable state */ }
})
onBeforeUnmount(() => { controller.abort(); modifiers.reset() })
</script>
