<template>
  <section class="terminal-view" aria-labelledby="terminal-title">
    <h1 id="terminal-title" class="sr-only">远程终端</h1>
    <TerminalTitlebar :title="termName" :computer-name="computerName" :status="connectionStatus" :display-menu-open="openMenu === 'display'" v-model:display-mode="displayMode" v-model:viewport-locked="viewportLocked" @update:display-menu-open="setMenuOpen('display', $event)" @rename="updateTermName">
      <TmuxActionMenu :bindings="bindings" :active-pane-id="activePane?.pane_id ?? null" :disabled="connectionStatus !== 'connected'" :open="openMenu === 'tmux'" @update:open="setMenuOpen('tmux', $event)" @action="runAction" @request-close="requestClose" />
      <TerminalAgentToggle v-if="agentBrokerEnabled" ref="agentToggle" :open="agentOpen" :pending-count="agentPendingCount" :readiness="agentReadiness" @toggle="toggleAgent" />
    </TerminalTitlebar>
    <div class="terminal-workspace">
      <div class="terminal-interaction-host" data-terminal-interaction-host :inert="agentBrokerEnabled && agentOpen && mobileOverlay ? true : undefined">
        <TerminalCanvas ref="terminalCanvas" :term-id="termId" :display-mode="displayMode" :viewport-locked="viewportLocked" :transform-input="transformInput" @bindings="bindings = $event" @reset-input="modifierResetKey = $event" @status="connectionStatus = $event" @authentication-required="handleAuthenticationRequired" @action-result="handleActionResult" />
        <p v-if="renameError" class="terminal-error" role="alert">{{ renameError }}</p>
        <MobileKeyBar :prefix="bindings.prefix" :controller="modifiers" :reset-key="modifierResetKey" :disabled="connectionStatus !== 'connected'" @input="terminalCanvas?.sendInput($event)" />
      </div>
      <TerminalAgentPanel v-if="agentBrokerEnabled" :open="agentOpen" :term-id="termId" :conversation-id="agentConversationId" :mobile-page="mobileOverlay" @close="closeAgent" @select-conversation="selectAgentConversation" @pending-count="agentPendingCount = $event" @readiness="agentReadiness = $event" />
    </div>
    <ClosePaneDialog v-if="closePane" :pane-id="closePane.pane_id" :pane-name="closePane.title || closePane.pane_id" :return-focus="closeReturnFocus" @cancel="closePaneId = null" @confirm="confirmClose" />
  </section>
</template>

<script setup lang="ts">
import { ApiError, MobileModifierController, type TerminalConnectionStatus } from '@termflow/client-core'
import type { AgentSetupResponse, TerminalActionResultFrame as TerminalActionResultControl } from '@termflow/client-contracts'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import TerminalCanvas from '../components/terminal/TerminalCanvas.vue'
import TerminalTitlebar from '../components/terminal/TerminalTitlebar.vue'
import type { DisplayMode } from '../terminal/viewport'
import TmuxActionMenu from '../components/terminal/TmuxActionMenu.vue'
import MobileKeyBar from '../components/terminal/MobileKeyBar.vue'
import ClosePaneDialog from '../components/terminal/ClosePaneDialog.vue'
import { createOrientationViewState, orientationFor } from '../terminal/orientation'
import { useSession } from '../composables/useSession'
import { useClientRuntime } from '../runtime'
import type { BindingSnapshot, PaneTopology, TerminalActionId } from '../types'
import TerminalAgentPanel from '../components/agent/TerminalAgentPanel.vue'
import TerminalAgentToggle from '../components/agent/TerminalAgentToggle.vue'
import { isAgentConversationId } from '../router/terminalQuery'
import { useAgentBroker } from '../composables/useAgentBroker'
const route = useRoute()
const router = useRouter()
const runtime = useClientRuntime()
const { clearSessionState } = useSession()
const termId = computed(() => String(route.params.termId))
const { agentBrokerEnabled } = useAgentBroker()
const agentConversationId = computed(() => isAgentConversationId(route.query.agent) ? route.query.agent : null)
const agentOpen = ref(agentConversationId.value !== null)
const agentPendingCount = ref(0), agentReadiness = ref<AgentSetupResponse['state'] | null>(null)
const agentToggle = ref<InstanceType<typeof TerminalAgentToggle> | null>(null)
const agentOverlayMedia = window.matchMedia('(max-width: 47.99rem), (pointer: coarse)')
const mobileOverlay = ref(agentOverlayMedia.matches)
function onAgentOverlayMediaChange(event: MediaQueryListEvent) { mobileOverlay.value = event.matches }
watch(agentConversationId, (id) => { if (id) agentOpen.value = true })
async function selectAgentConversation(id: string | null) {
  const query = id && isAgentConversationId(id) ? { agent: id } : {}
  await router.replace({ path: route.path, query, hash: route.hash })
}
async function closeAgent() {
  agentOpen.value = false
  await selectAgentConversation(null)
  await nextTick(); agentToggle.value?.focus()
}
function toggleAgent() { if (agentOpen.value) void closeAgent(); else agentOpen.value = true }
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
type DesktopMenu = 'display' | 'tmux'
const openMenu = ref<DesktopMenu | null>(null)
const viewportLocked = ref(false)
const renameError = ref('')
const panes = ref<PaneTopology[]>([])
const terminalCanvas = ref<InstanceType<typeof TerminalCanvas> | null>(null)
const bindings = ref<BindingSnapshot>({ prefix: '未报告', bindings: [] })
const modifiers = reactive(new MobileModifierController())
const modifierResetKey = ref(0)
const closePaneId = ref<string | null>(null)
const closeReturnFocus = ref<HTMLElement | null>(null)
let topologyGeneration = 0
const activePane = computed(() => panes.value.find((pane) => pane.active) ?? panes.value[0])
const closePane = computed(() => panes.value.find((pane) => pane.pane_id === closePaneId.value) ?? (closePaneId.value ? { pane_id: closePaneId.value, title: closePaneId.value } as PaneTopology : null))
const transformInput = (value: string | Uint8Array) => typeof value === 'string' ? modifiers.consume(value) : value
function setMenuOpen(menu: DesktopMenu, open: boolean) { openMenu.value = open ? menu : (openMenu.value === menu ? null : openMenu.value) }
function runAction(actionId: TerminalActionId, paneId: string | null) { terminalCanvas.value?.sendAction(actionId, paneId === null ? {} : { targetPaneId: paneId }) }
function requestClose(paneId: string | null, returnFocus: HTMLElement | null) { closeReturnFocus.value = returnFocus; closePaneId.value = paneId }
function confirmClose(payload: { paneId: string; confirmed: true }) { terminalCanvas.value?.sendAction('close_pane', { targetPaneId: payload.paneId, confirmed: true }); closePaneId.value = null }
async function updateTermName(name: string) {
  const previous = termName.value
  termName.value = name
  renameError.value = ''
  try { termName.value = (await runtime.api.terms.rename(termId.value, name, controller.signal)).name }
  catch (error) { termName.value = previous; renameError.value = error instanceof ApiError ? error.message : '无法更新 Term 名称。' }
}
function restoreOrientationView() {
  const saved = orientationViews[orientation.value].viewport
  if (saved) terminalCanvas.value?.restoreViewport(saved)
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
  clearSessionState()
  void router.replace({ path: '/login', query: { redirect: route.fullPath } })
}
async function refreshTopology() {
  const generation = ++topologyGeneration
  try {
    const response = await runtime.api.terms.topology(termId.value, controller.signal)
    if (generation !== topologyGeneration) return
    termName.value = response.topology.session_name
    panes.value = response.topology.windows.flatMap((window) => window.panes)
  } catch { /* terminal channel owns the visible connection error */ }
}
function handleActionResult(_result: TerminalActionResultControl) { void refreshTopology() }
const controller = new AbortController()
onMounted(async () => {
  window.addEventListener('resize', onViewportResize)
  agentOverlayMedia.addEventListener('change', onAgentOverlayMediaChange)
  const [, dashboardResult] = await Promise.allSettled([refreshTopology(), runtime.api.dashboard.get(controller.signal)])
  if (dashboardResult.status === 'fulfilled') {
    const computer = dashboardResult.value.computers.find((candidate) => candidate.terms.some((term) => term.instance_id === termId.value))
    const term = computer?.terms.find((candidate) => candidate.instance_id === termId.value)
    if (computer) computerName.value = computer.display_name
    if (term) termName.value = term.name
  }
  await nextTick()
  restoreOrientationView()
})
onBeforeUnmount(() => { window.removeEventListener('resize', onViewportResize); agentOverlayMedia.removeEventListener('change', onAgentOverlayMediaChange); controller.abort(); modifiers.reset() })
</script>
