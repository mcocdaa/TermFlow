<template>
  <button ref="button" type="button" class="titlebar-button terminal-agent-toggle" data-action="toggle-agent" :aria-expanded="open" aria-controls="terminal-agent-panel" :aria-label="accessibleLabel" @click="emit('toggle')">
    <Bot :size="18" aria-hidden="true" />
    <span>Agent</span>
    <span v-if="pendingCount" class="agent-pending-badge" data-agent-pending-badge>{{ pendingCount }}</span>
  </button>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import { Bot } from '@lucide/vue'
import type { AgentSetupResponse } from '@termflow/client-contracts'
const props = defineProps<{ open: boolean; pendingCount: number; readiness: AgentSetupResponse['state'] | null }>()
const emit = defineEmits<{ toggle: [] }>()
const button = ref<HTMLButtonElement | null>(null)
const readinessLabels: Record<AgentSetupResponse['state'], string> = {
  unconfigured: '未设置',
  deployment_required: '需部署',
  activating: '激活中',
  ready: '已就绪',
  unavailable: '不可用',
}
const readinessLabel = computed(() => props.readiness === null ? null : readinessLabels[props.readiness])
const accessibleLabel = computed(() => [
  'Agent',
  readinessLabel.value,
  props.pendingCount ? `${props.pendingCount} 个待处理审批` : null,
].filter(Boolean).join('，'))
defineExpose({ focus: () => button.value?.focus() })
</script>
