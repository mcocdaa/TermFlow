<template>
  <div
    class="agent-message"
    :class="isUser ? 'agent-message--user' : 'agent-message--assistant'"
    :data-agent-message-role="isUser ? 'user' : 'assistant'"
  >
    <p class="agent-message__text">{{ text }}<span v-if="streaming" class="agent-message__cursor" aria-hidden="true"></span></p>
  </div>
</template>

<script setup lang="ts">
//: Assistant/user chat bubble (M6b spec §4.7/§6.2). Pure-text rendering:
//: `{{ }}` interpolation only — no v-html, no linkification — so HTML,
//: URL-shaped text and script payloads can never become elements. ANSI/OSC
//: escape noise is stripped before display; a streaming assistant message
//: shows an aria-hidden cursor at its tail.
import { computed } from 'vue'
import { stripAnsiOsc, type AgentMessageState, type AgentUserMessageState } from '@termflow/client-core'

const props = defineProps<{
  /** Assistant message (streaming/complete) or user echo, exactly as stored by the M6b history reducer. */
  message: AgentMessageState | AgentUserMessageState
}>()

const isUser = computed(() => {
  const message = props.message
  return 'deliveryState' in message || message.role === 'user'
})
const streaming = computed(() => 'status' in props.message && props.message.status === 'streaming')
const text = computed(() => stripAnsiOsc(props.message.text))
</script>
