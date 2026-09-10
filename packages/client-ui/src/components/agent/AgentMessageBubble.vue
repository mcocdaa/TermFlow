<template>
  <details
    v-if="isSystem"
    class="agent-message agent-message--system"
    data-agent-message-role="system"
  >
    <summary class="agent-message__summary">
      <span class="agent-message__role" data-agent-message-role-label>
        <Settings2 :size="14" aria-hidden="true" />
        系统
      </span>
      <span class="agent-message__summary-title">系统上下文</span>
    </summary>
    <p class="agent-message__text">{{ text || '系统上下文内容不可用' }}</p>
  </details>
  <div
    v-else
    class="agent-message"
    :class="isUser ? 'agent-message--user' : 'agent-message--assistant'"
    :data-agent-message-role="isUser ? 'user' : 'assistant'"
  >
    <div class="agent-message__role" data-agent-message-role-label>
      <UserRound v-if="isUser" :size="14" aria-hidden="true" />
      <Bot v-else :size="14" aria-hidden="true" />
      {{ isUser ? '你' : 'Agent' }}
    </div>
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
import { Bot, Settings2, UserRound } from '@lucide/vue'
import { stripAnsiOsc, type AgentMessageState, type AgentUserMessageState } from '@termflow/client-core'

const props = defineProps<{
  /** Assistant message (streaming/complete) or user echo, exactly as stored by the M6b history reducer. */
  message: AgentMessageState | AgentUserMessageState
}>()

const isUser = computed(() => {
  const message = props.message
  return 'deliveryState' in message || message.role === 'user'
})
const isSystem = computed(() => 'role' in props.message && props.message.role === 'system')
const streaming = computed(() => 'status' in props.message && props.message.status === 'streaming')
const text = computed(() => stripAnsiOsc(props.message.text))
</script>
