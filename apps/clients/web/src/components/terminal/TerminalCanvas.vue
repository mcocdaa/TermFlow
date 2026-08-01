<template>
  <div class="terminal-frame" :data-status="status">
    <div class="terminal-viewport-content" :style="contentStyle">
      <div class="terminal-grid" :style="gridStyle"><div ref="host" class="terminal-host" role="application" :aria-label="`Term ${termId} 终端`" /></div>
    </div>
    <div v-if="status === 'connecting' || status === 'reconnecting'" class="terminal-overlay" role="status">{{ status === 'reconnecting' ? '连接中断，正在恢复…' : '正在连接终端…' }}</div>
    <p v-if="terminalError" class="terminal-error" role="alert">{{ terminalError }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { TerminalAdapterFactory } from '../../terminal/terminalAdapter'
import type { TerminalSocketFactory } from '../../composables/useTerminalSession'
import { useTerminalSession } from '../../composables/useTerminalSession'
import { displayPresentation, type DisplayMode } from '../../terminal/viewport'

const props = withDefaults(defineProps<{ termId: string; displayMode?: DisplayMode; createSocket?: TerminalSocketFactory; createAdapter?: TerminalAdapterFactory }>(), { displayMode: 'font-100' })
const host = ref<HTMLElement | null>(null)
const frame = ref({ width: 1, height: 1 })
const session = useTerminalSession(props.termId, host, props.createSocket, props.createAdapter)
const { status, dimensions, bindings, terminalError, lastActionResult } = session
const presentation = computed(() => dimensions.value ? displayPresentation(props.displayMode, dimensions.value, frame.value, { cellWidth: 9, cellHeight: 18 }) : null)
const contentStyle = computed(() => presentation.value ? { width: `${presentation.value.scaledWidth}px`, height: `${presentation.value.scaledHeight}px` } : {})
const gridStyle = computed(() => presentation.value ? { width: `${presentation.value.gridWidth}px`, height: `${presentation.value.gridHeight}px`, transform: `scale(${presentation.value.scale})` } : {})
let observer: ResizeObserver | null = null
onMounted(() => {
  const element = host.value?.closest('.terminal-frame') as HTMLElement | null
  if (!element) return
  const update = () => { frame.value = { width: Math.max(1, element.clientWidth), height: Math.max(1, element.clientHeight) } }
  update()
  observer = new ResizeObserver(update)
  observer.observe(element)
})
onBeforeUnmount(() => observer?.disconnect())
defineExpose({ dimensions, bindings, lastActionResult, sendAction: session.sendAction, focus: session.focus })
</script>
