<template>
  <div ref="frameElement" class="terminal-frame" :data-status="status" :data-display-mode="displayMode" :data-focused-pane="pointer.state.focusedPaneId ?? undefined" :data-cell-width="renderedCellMetrics?.width" :data-cell-height="renderedCellMetrics?.height" :data-visual-scale="appliedVisualScale" @pointerdown="onPointerDown" @pointermove="onPointerMove" @pointerup="onPointerUp" @pointercancel="onPointerUp">
    <div class="terminal-viewport-content" :style="contentStyle">
      <div class="terminal-grid" :style="gridStyle"><div ref="host" class="terminal-host" role="application" :aria-label="`Term ${termId} 终端`" /></div>
    </div>
    <div v-if="status === 'connecting' || status === 'reconnecting'" class="terminal-overlay" role="status">{{ status === 'reconnecting' ? '连接中断，正在恢复…' : '正在连接终端…' }}</div>
    <p v-if="terminalError" class="terminal-error" role="alert">{{ terminalError }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, watchEffect } from 'vue'
import type { TerminalAdapterFactory, TerminalCellMetrics } from '../../terminal/terminalAdapter'
import type { PaneTopologyDto } from '../../api/types'
import type { TerminalSocketFactory } from '../../composables/useTerminalSession'
import { useTerminalSession } from '../../composables/useTerminalSession'
import { displayPresentation, type DisplayMode } from '../../terminal/viewport'
import { createPointerViewport, type PointerViewportSnapshot } from '../../composables/usePointerViewport'
import { activeTheme } from '../../stores/theme'

const props = withDefaults(defineProps<{ termId: string; displayMode?: DisplayMode; selectionActive?: boolean; mouseReportingActive?: boolean; transformInput?: (value: string | Uint8Array) => string | Uint8Array; createSocket?: TerminalSocketFactory; createAdapter?: TerminalAdapterFactory }>(), { displayMode: 'font-100', selectionActive: false, mouseReportingActive: false })
const emit = defineEmits<{ bindings: [value: { prefix: string; prefix2?: string | null; bindings: Array<{ action: import('../../api/types').TerminalActionId; key: string | null; tooltip: string }> }]; 'reset-input': [key: number]; status: [value: import('../../terminal/socket').TerminalConnectionStatus]; 'authentication-required': []; 'action-result': [value: import('../../terminal/protocol').TerminalActionResultControl] }>()
const host = ref<HTMLElement | null>(null)
const frameElement = ref<HTMLElement | null>(null)
const frame = ref({ width: 1, height: 1 })
const baselineCellMetrics = ref<TerminalCellMetrics | null>(null)
const renderedCellMetrics = ref<TerminalCellMetrics | null>(null)
const appliedVisualScale = ref(1)
const session = useTerminalSession(props.termId, host, props.createSocket, props.createAdapter, props.transformInput)
const { status, dimensions, bindings, terminalError, lastActionResult, resetKey, authenticationRequired } = session
const presentation = computed(() => dimensions.value && baselineCellMetrics.value ? displayPresentation(props.displayMode, dimensions.value, frame.value, { cellWidth: baselineCellMetrics.value.width, cellHeight: baselineCellMetrics.value.height }) : null)
const pointer = createPointerViewport({ viewport: frame.value, content: frame.value, canPan: () => !props.selectionActive && !props.mouseReportingActive && session.canClientPan() })
const requestedVisualScale = computed(() => (presentation.value?.scale ?? 1) * pointer.state.scale)
const renderedGrid = computed(() => dimensions.value && renderedCellMetrics.value ? { width: dimensions.value.cols * renderedCellMetrics.value.width, height: dimensions.value.rows * renderedCellMetrics.value.height } : null)
const contentStyle = computed(() => renderedGrid.value ? { width: `${renderedGrid.value.width}px`, height: `${renderedGrid.value.height}px` } : {})
const gridStyle = computed(() => renderedGrid.value ? { width: `${renderedGrid.value.width}px`, height: `${renderedGrid.value.height}px`, transform: `translate(${pointer.state.panX}px, ${pointer.state.panY}px)` } : {})
let observer: ResizeObserver | null = null
let pendingFocusPane: PaneTopologyDto | null = null
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
watchEffect(() => {
  const size = dimensions.value
  if (!size || !baselineCellMetrics.value) return
  const requested = requestedVisualScale.value
  let rendered = session.setVisualScale(requested)
  if (!rendered) return
  let applied = requested
  const renderedWidth = size.cols * rendered.width
  const renderedHeight = size.rows * rendered.height
  if (
    props.displayMode === 'fit'
    && pointer.state.scale === 1
    && (renderedWidth > frame.value.width + 1 || renderedHeight > frame.value.height + 1)
  ) {
    const correction = Math.min(1, frame.value.width / renderedWidth, frame.value.height / renderedHeight)
    applied = requested * correction * 0.999
    rendered = session.setVisualScale(applied) ?? rendered
  }
  appliedVisualScale.value = applied
  renderedCellMetrics.value = rendered
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
  if (!baselineCellMetrics.value) baselineCellMetrics.value = measured
  renderedCellMetrics.value = measured
  if (pendingFocusPane) applyPaneFocus(pendingFocusPane)
}
function point(event: PointerEvent) { return { pointerId: event.pointerId, x: event.clientX, y: event.clientY } }
function onPointerDown(event: PointerEvent) { if (event.pointerType === 'mouse') return; frameElement.value?.setPointerCapture?.(event.pointerId); pointer.pointerDown(point(event)) }
function onPointerMove(event: PointerEvent) { if (event.pointerType !== 'mouse') pointer.pointerMove(point(event)) }
function onPointerUp(event: PointerEvent) { if (event.pointerType === 'mouse') return; pointer.pointerUp(event.pointerId); frameElement.value?.releasePointerCapture?.(event.pointerId) }
function focusPane(pane: PaneTopologyDto) {
  pendingFocusPane = pane
  refreshCellMetrics()
  if (!baselineCellMetrics.value) return
  applyPaneFocus(pane)
}
function applyPaneFocus(pane: PaneTopologyDto) {
  if (!baselineCellMetrics.value) return
  const scale = presentation.value?.scale ?? 1
  pointer.focusPane(pane, { cellWidth: baselineCellMetrics.value.width * scale, cellHeight: baselineCellMetrics.value.height * scale })
  pendingFocusPane = null
}
function resetViewport() { pendingFocusPane = null; pointer.reset() }
function restoreViewport(value: PointerViewportSnapshot) { pendingFocusPane = null; pointer.restore(value) }
defineExpose({ dimensions, bindings, lastActionResult, sendAction: session.sendAction, sendInput: session.sendInput, focus: session.focus, focusPane, resetViewport, captureViewport: pointer.snapshot, restoreViewport })
</script>
