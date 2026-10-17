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
import { useI18n } from '../../i18n'
const { t, locale } = useI18n()
const props = defineProps<{ open: boolean; pendingCount: number; readiness: AgentSetupResponse['state'] | null }>()
const emit = defineEmits<{ toggle: [] }>()
const button = ref<HTMLButtonElement | null>(null)
const readinessLabel = computed(() => props.readiness === null ? null : t(`agent.readiness.${props.readiness}`))
const accessibleLabel = computed(() => [
  'Agent',
  readinessLabel.value,
  props.pendingCount ? t('agent.conversations.pendingBadgeAria', { count: props.pendingCount }) : null,
].filter(Boolean).join(locale.value === 'en' ? ', ' : '，'))
defineExpose({ focus: () => button.value?.focus() })
</script>
