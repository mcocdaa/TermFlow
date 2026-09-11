<template>
  <details class="agent-thinking" :open="thinking.status === 'streaming'" :data-state="thinking.status" data-agent-thinking>
    <summary class="agent-thinking__summary">
      <Sparkles :size="14" aria-hidden="true" />
      <span>思考{{ thinking.status === 'streaming' ? '中…' : '' }}</span>
    </summary>
    <p class="agent-thinking__text">{{ text }}</p>
  </details>
</template>

<script setup lang="ts">
//: Collapsible model-reasoning block. Streams open, collapses once complete;
//: pure text interpolation only (no v-html), ANSI/OSC stripped.
import { computed } from 'vue'
import { Sparkles } from '@lucide/vue'
import { stripAnsiOsc, type AgentThinkingState } from '@termflow/client-core'

const props = defineProps<{ thinking: AgentThinkingState }>()
const text = computed(() => stripAnsiOsc(props.thinking.text))
</script>
