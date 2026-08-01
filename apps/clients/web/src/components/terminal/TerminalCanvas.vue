<template>
  <div ref="frameElement" class="terminal-frame" :data-status="status" @pointerdown="onPointerDown" @pointermove="onPointerMove" @pointerup="onPointerUp" @pointercancel="onPointerUp">
    <div class="terminal-viewport-content" :style="contentStyle">
      <div class="terminal-grid" :style="gridStyle"><div ref="host" class="terminal-host" role="application" :aria-label="`Term ${termId} 终端`" /></div>
    </div>
    <div v-if="status === 'connecting' || status === 'reconnecting'" class="terminal-overlay" role="status">{{ status === 'reconnecting' ? '连接中断，正在恢复…' : '正在连接终端…' }}</div>
    <p v-if="terminalError" class="terminal-error" role="alert">{{ terminalError }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch, watchEffect } from 'vue'
import type { TerminalAdapterFactory } from '../../terminal/terminalAdapter'
import type { PaneTopologyDto } from '../../api/types'
import type { TerminalSocketFactory } from '../../composables/useTerminalSession'
import { useTerminalSession } from '../../composables/useTerminalSession'
import { displayPresentation, type DisplayMode } from '../../terminal/viewport'
import { createPointerViewport } from '../../composables/usePointerViewport'

const props = withDefaults(defineProps<{ termId: string; displayMode?: DisplayMode; selectionActive?: boolean; mouseReportingActive?: boolean; transformInput?: (value: string | Uint8Array) => string | Uint8Array; createSocket?: TerminalSocketFactory; createAdapter?: TerminalAdapterFactory }>(), { displayMode: 'font-100', selectionActive: false, mouseReportingActive: false })
const emit = defineEmits<{ bindings: [value: { prefix: string; prefix2?: string | null; bindings: Array<{ action: import('../../api/types').TerminalActionId; key: string | null; tooltip: string }> }]; 'reset-input': [key: number] }>()
const host = ref<HTMLElement | null>(null)
const frameElement = ref<HTMLElement | null>(null)
const frame = ref({ width: 1, height: 1 })
const session = useTerminalSession(props.termId, host, props.createSocket, props.createAdapter, props.transformInput)
const { status, dimensions, bindings, terminalError, lastActionResult, resetKey } = session
const presentation = computed(() => dimensions.value ? displayPresentation(props.displayMode, dimensions.value, frame.value, { cellWidth: 9, cellHeight: 18 }) : null)
const pointer = createPointerViewport({ viewport: frame.value, content: frame.value, canPan: () => !props.selectionActive && !props.mouseReportingActive && session.canClientPan() })
const totalScale = computed(() => (presentation.value?.scale ?? 1) * pointer.state.scale)
const contentStyle = computed(() => presentation.value ? { width: `${presentation.value.gridWidth * totalScale.value}px`, height: `${presentation.value.gridHeight * totalScale.value}px` } : {})
const gridStyle = computed(() => presentation.value ? { width: `${presentation.value.gridWidth}px`, height: `${presentation.value.gridHeight}px`, transform: `translate(${pointer.state.panX}px, ${pointer.state.panY}px) scale(${totalScale.value})` } : {})
let observer: ResizeObserver | null = null
onMounted(() => {
  const element = frameElement.value
  if (!element) return
  const update = () => { frame.value = { width: Math.max(1, element.clientWidth), height: Math.max(1, element.clientHeight) } }
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
watch(resetKey, (value) => emit('reset-input', value))
function point(event: PointerEvent) { return { pointerId: event.pointerId, x: event.clientX, y: event.clientY } }
function onPointerDown(event: PointerEvent) { if (event.pointerType === 'mouse') return; frameElement.value?.setPointerCapture?.(event.pointerId); pointer.pointerDown(point(event)) }
function onPointerMove(event: PointerEvent) { if (event.pointerType !== 'mouse') pointer.pointerMove(point(event)) }
function onPointerUp(event: PointerEvent) { if (event.pointerType === 'mouse') return; pointer.pointerUp(event.pointerId); frameElement.value?.releasePointerCapture?.(event.pointerId) }
function focusPane(pane: PaneTopologyDto) { pointer.focusPane(pane, { cellWidth: 9 * (presentation.value?.scale ?? 1), cellHeight: 18 * (presentation.value?.scale ?? 1) }) }
defineExpose({ dimensions, bindings, lastActionResult, sendAction: session.sendAction, sendInput: session.sendInput, focus: session.focus, focusPane, resetViewport: pointer.reset })
</script>
