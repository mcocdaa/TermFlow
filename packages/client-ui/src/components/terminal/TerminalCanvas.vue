<template>
  <div ref="frameElement" class="terminal-frame" :data-status="status" :data-display-mode="displayMode" :data-focused-pane="pointer.state.focusedPaneId ?? undefined" :data-cell-width="cellMetrics?.width" :data-cell-height="cellMetrics?.height" @pointerdown="onPointerDown" @pointermove="onPointerMove" @pointerup="onPointerUp" @pointercancel="onPointerUp">
    <div class="terminal-viewport-content" :style="contentStyle">
      <div class="terminal-grid" :style="gridStyle"><div ref="host" class="terminal-host" role="application" :aria-label="`Term ${termId} 终端`" /></div>
    </div>
    <div v-if="status === 'connecting' || status === 'reconnecting'" class="terminal-overlay" role="status">{{ status === 'reconnecting' ? '连接中断，正在恢复…' : '正在连接终端…' }}</div>
    <p v-if="terminalError" class="terminal-error" role="alert">{{ terminalError }}</p>
  </div>
</template>

<script setup lang="ts">
import type { TerminalConnectionStatus } from '@termflow/client-core'
import type { TerminalActionResultFrame } from '@termflow/client-contracts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, watchEffect } from 'vue'
import type { TerminalAdapterFactory } from '../../terminal/xtermAdapter'
import type { PaneTopology, TerminalActionId } from '../../types'
import type { TerminalFactory } from '../../composables/useTerminalSession'
import { useTerminalSession } from '../../composables/useTerminalSession'
import { displayPresentation, type DisplayMode } from '../../terminal/viewport'
import { createPointerViewport, type PointerViewportSnapshot } from '../../composables/usePointerViewport'
import { activeTheme } from '../../theme/theme'

const props = withDefaults(defineProps<{ termId: string; displayMode?: DisplayMode; selectionActive?: boolean; mouseReportingActive?: boolean; transformInput?: (value: string | Uint8Array) => string | Uint8Array; createTerminal?: TerminalFactory; createAdapter?: TerminalAdapterFactory }>(), { displayMode: 'font-100', selectionActive: false, mouseReportingActive: false })
const emit = defineEmits<{ bindings: [value: { prefix: string; prefix2?: string | null; bindings: Array<{ action: TerminalActionId; key: string | null; tooltip: string }> }]; 'reset-input': [key: number]; status: [value: TerminalConnectionStatus]; 'authentication-required': []; 'action-result': [value: TerminalActionResultFrame] }>()
const host = ref<HTMLElement | null>(null)
const frameElement = ref<HTMLElement | null>(null)
const frame = ref({ width: 1, height: 1 })
const cellMetrics = ref<{ width: number; height: number } | null>(null)
const session = useTerminalSession(props.termId, host, props.createTerminal, props.createAdapter, props.transformInput)
const { status, dimensions, bindings, terminalError, lastActionResult, resetKey, authenticationRequired } = session
const presentation = computed(() => dimensions.value && cellMetrics.value ? displayPresentation(props.displayMode, dimensions.value, frame.value, { cellWidth: cellMetrics.value.width, cellHeight: cellMetrics.value.height }) : null)
const pointer = createPointerViewport({ viewport: frame.value, content: frame.value, canPan: () => !props.selectionActive && !props.mouseReportingActive && session.canClientPan() })
const totalScale = computed(() => (presentation.value?.scale ?? 1) * pointer.state.scale)
const contentStyle = computed(() => presentation.value ? { width: `${presentation.value.gridWidth * totalScale.value}px`, height: `${presentation.value.gridHeight * totalScale.value}px` } : {})
const gridStyle = computed(() => presentation.value ? { width: `${presentation.value.gridWidth}px`, height: `${presentation.value.gridHeight}px`, transform: `translate(${pointer.state.panX}px, ${pointer.state.panY}px) scale(${totalScale.value})` } : {})
let observer: ResizeObserver | null = null
let pendingFocusPane: PaneTopology | null = null
onMounted(() => {
  const element = frameElement.value
  if (!element) return
  const update = () => { frame.value = { width: Math.max(1, element.clientWidth), height: Math.max(1, element.clientHeight) }; refreshCellMetrics() }
  update()
  observer = new ResizeObserver(update)
  observer.observe(element)
})
onBeforeUnmount(() => observer?.disconnect())
watchEffect(() => {
  if (!presentation.value) return
  pointer.updateGeometry(frame.value, { width: presentation.value.gridWidth * presentation.value.scale, height: presentation.value.gridHeight * presentation.value.scale })
})
watch(bindings, (value) => emit('bindings', value), { deep: true, immediate: true })
watch(status, (value) => emit('status', value), { immediate: true })
watch(resetKey, (value) => emit('reset-input', value))
watch(authenticationRequired, () => emit('authentication-required'))
watch(lastActionResult, (value) => { if (value) emit('action-result', value) })
watch(activeTheme, () => session.refreshTheme())
watch(dimensions, () => { void nextTick(refreshCellMetrics) })
function refreshCellMetrics() {
  const measured = session.measureCell()
  if (!measured) return
  cellMetrics.value = measured
  if (pendingFocusPane) applyPaneFocus(pendingFocusPane)
}
function point(event: PointerEvent) { return { pointerId: event.pointerId, x: event.clientX, y: event.clientY } }
function onPointerDown(event: PointerEvent) { if (event.pointerType === 'mouse') return; frameElement.value?.setPointerCapture?.(event.pointerId); pointer.pointerDown(point(event)) }
function onPointerMove(event: PointerEvent) { if (event.pointerType !== 'mouse') pointer.pointerMove(point(event)) }
function onPointerUp(event: PointerEvent) { if (event.pointerType === 'mouse') return; pointer.pointerUp(event.pointerId); frameElement.value?.releasePointerCapture?.(event.pointerId) }
function focusPane(pane: PaneTopology) {
  pendingFocusPane = pane
  refreshCellMetrics()
  if (!cellMetrics.value) return
  applyPaneFocus(pane)
}
function applyPaneFocus(pane: PaneTopology) {
  if (!cellMetrics.value) return
  const scale = presentation.value?.scale ?? 1
  pointer.focusPane(pane, { cellWidth: cellMetrics.value.width * scale, cellHeight: cellMetrics.value.height * scale })
  pendingFocusPane = null
}
function resetViewport() { pendingFocusPane = null; pointer.reset() }
function restoreViewport(value: PointerViewportSnapshot) { pendingFocusPane = null; pointer.restore(value) }
defineExpose({ dimensions, bindings, lastActionResult, sendAction: session.sendAction, sendInput: session.sendInput, focus: session.focus, focusPane, resetViewport, captureViewport: pointer.snapshot, restoreViewport })
</script>
