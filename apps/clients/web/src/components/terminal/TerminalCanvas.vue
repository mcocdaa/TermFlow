<template>
  <div class="terminal-frame" :data-status="status">
    <div ref="host" class="terminal-host" role="application" :aria-label="`Term ${termId} 终端`" />
    <div v-if="status === 'connecting' || status === 'reconnecting'" class="terminal-overlay" role="status">{{ status === 'reconnecting' ? '连接中断，正在恢复…' : '正在连接终端…' }}</div>
    <p v-if="terminalError" class="terminal-error" role="alert">{{ terminalError }}</p>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { TerminalAdapterFactory } from '../../terminal/terminalAdapter'
import type { TerminalSocketFactory } from '../../composables/useTerminalSession'
import { useTerminalSession } from '../../composables/useTerminalSession'

const props = defineProps<{ termId: string; createSocket?: TerminalSocketFactory; createAdapter?: TerminalAdapterFactory }>()
const host = ref<HTMLElement | null>(null)
const session = useTerminalSession(props.termId, host, props.createSocket, props.createAdapter)
const { status, dimensions, bindings, terminalError, lastActionResult } = session
defineExpose({ dimensions, bindings, lastActionResult, sendAction: session.sendAction, focus: session.focus })
</script>
