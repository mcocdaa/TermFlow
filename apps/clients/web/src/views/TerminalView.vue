<template>
  <section class="terminal-view" aria-labelledby="terminal-title">
    <h1 id="terminal-title" class="sr-only">远程终端</h1>
    <TerminalTitlebar :title="termName" :computer-name="computerName" :status="connectionStatus" v-model:display-mode="displayMode" @rename="updateTermName">
      <PaneFocusMenu :panes="panes" @focus="terminalCanvas?.focusPane($event)" @reset="terminalCanvas?.resetViewport()" />
      <TmuxActionMenu :bindings="bindings" :active-pane-id="activePane?.pane_id ?? null" :disabled="connectionStatus !== 'connected'" @action="runAction" @request-close="requestClose" />
    </TerminalTitlebar>
    <TerminalCanvas ref="terminalCanvas" :term-id="termId" :display-mode="displayMode" :transform-input="transformInput" @bindings="bindings = $event" @reset-input="modifierResetKey = $event" @status="connectionStatus = $event" @authentication-required="handleAuthenticationRequired" @action-result="handleActionResult" />
    <p v-if="renameError" class="terminal-error" role="alert">{{ renameError }}</p>
    <MobileKeyBar :prefix="bindings.prefix" :controller="modifiers" :reset-key="modifierResetKey" :disabled="connectionStatus !== 'connected'" @input="terminalCanvas?.sendInput($event)" />
    <ClosePaneDialog v-if="closePane" :pane-id="closePane.pane_id" :pane-name="closePane.title || closePane.pane_id" :return-focus="closeReturnFocus" @cancel="closePaneId = null" @confirm="confirmClose" />
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import TerminalCanvas from '../components/terminal/TerminalCanvas.vue'
import TerminalTitlebar from '../components/terminal/TerminalTitlebar.vue'
import type { DisplayMode } from '../terminal/viewport'
import { getTermTopology, renameTerm } from '../api/terms'
import { getDashboard } from '../api/dashboard'
import type { PaneTopologyDto } from '../api/types'
import PaneFocusMenu from '../components/terminal/PaneFocusMenu.vue'
import TmuxActionMenu from '../components/terminal/TmuxActionMenu.vue'
import MobileKeyBar from '../components/terminal/MobileKeyBar.vue'
import ClosePaneDialog from '../components/terminal/ClosePaneDialog.vue'
import type { BindingSnapshotDto } from '../api/types'
import { MobileModifierController } from '../terminal/modifiers'
import type { TerminalActionId } from '../api/types'
import type { TerminalConnectionStatus } from '../terminal/socket'
import { ApiError } from '../api/http'
import { createOrientationViewState, orientationFor } from '../terminal/orientation'
import { sessionState } from '../stores/session'
import type { TerminalActionResultControl } from '../terminal/protocol'
const route = useRoute()
const router = useRouter()
const termId = computed(() => String(route.params.termId))
const orientation = ref(orientationFor(window.innerWidth, window.innerHeight))
const orientationViews = reactive(createOrientationViewState())
const displayMode = computed<DisplayMode>({
  get: () => orientationViews[orientation.value].displayMode,
  set: (value) => {
    orientationViews[orientation.value].displayMode = value
    if (value === 'fit') void nextTick(() => terminalCanvas.value?.resetViewport())
  },
})
const termName = ref(`Term · ${termId.value}`)
const computerName = ref('Computer 未报告')
const connectionStatus = ref<TerminalConnectionStatus>('connecting')
const renameError = ref('')
const panes = ref<PaneTopologyDto[]>([])
const terminalCanvas = ref<InstanceType<typeof TerminalCanvas> | null>(null)
const bindings = ref<BindingSnapshotDto>({ prefix: '未报告', bindings: [] })
const modifiers = new MobileModifierController()
const modifierResetKey = ref(0)
const closePaneId = ref<string | null>(null)
const closeReturnFocus = ref<HTMLElement | null>(null)
let topologyGeneration = 0
const activePane = computed(() => panes.value.find((pane) => pane.active) ?? panes.value[0])
const closePane = computed(() => panes.value.find((pane) => pane.pane_id === closePaneId.value) ?? (closePaneId.value ? { pane_id: closePaneId.value, title: closePaneId.value } as PaneTopologyDto : null))
const transformInput = (value: string | Uint8Array) => typeof value === 'string' ? modifiers.consume(value) : value
function runAction(actionId: TerminalActionId, paneId: string | null) { terminalCanvas.value?.sendAction(actionId, { targetPaneId: paneId ?? undefined }) }
function requestClose(paneId: string | null, returnFocus: HTMLElement | null) { closeReturnFocus.value = returnFocus; closePaneId.value = paneId }
function confirmClose(payload: { paneId: string; confirmed: true }) { terminalCanvas.value?.sendAction('close_pane', { targetPaneId: payload.paneId, confirmed: true }); closePaneId.value = null }
async function updateTermName(name: string) {
  const previous = termName.value
  termName.value = name
  renameError.value = ''
  try { termName.value = (await renameTerm(termId.value, name, controller.signal)).name }
  catch (error) { termName.value = previous; renameError.value = error instanceof ApiError ? error.message : '无法更新 Term 名称。' }
}
function restoreOrientationView() {
  const saved = orientationViews[orientation.value].viewport
  if (saved) terminalCanvas.value?.restoreViewport(saved)
  else if (orientation.value === 'portrait' && activePane.value) terminalCanvas.value?.focusPane(activePane.value)
  else terminalCanvas.value?.resetViewport()
}
function onViewportResize() {
  const nextOrientation = orientationFor(window.innerWidth, window.innerHeight)
  if (nextOrientation === orientation.value) return
  orientationViews[orientation.value].viewport = terminalCanvas.value?.captureViewport() ?? null
  orientation.value = nextOrientation
  void nextTick(restoreOrientationView)
}
function handleAuthenticationRequired() {
  sessionState.authenticated = false
  sessionState.expiresAt = null
  void router.replace({ path: '/login', query: { redirect: route.fullPath } })
}
async function refreshTopology() {
  const generation = ++topologyGeneration
  try {
    const response = await getTermTopology(termId.value, controller.signal)
    if (generation !== topologyGeneration) return
    termName.value = response.topology.session_name
    panes.value = response.topology.windows.flatMap((window) => window.panes)
  } catch { /* terminal channel owns the visible connection error */ }
}
function handleActionResult(_result: TerminalActionResultControl) { void refreshTopology() }
const controller = new AbortController()
onMounted(async () => {
  window.addEventListener('resize', onViewportResize)
  const [, dashboardResult] = await Promise.allSettled([refreshTopology(), getDashboard(controller.signal)])
  if (dashboardResult.status === 'fulfilled') {
    const computer = dashboardResult.value.computers.find((candidate) => candidate.terms.some((term) => term.instance_id === termId.value))
    const term = computer?.terms.find((candidate) => candidate.instance_id === termId.value)
    if (computer) computerName.value = computer.display_name
    if (term) termName.value = term.name
  }
  await nextTick()
  restoreOrientationView()
})
onBeforeUnmount(() => { window.removeEventListener('resize', onViewportResize); controller.abort(); modifiers.reset() })
</script>
