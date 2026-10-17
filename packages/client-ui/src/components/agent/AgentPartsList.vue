<template>
  <div class="agent-parts">
    <template v-for="(part, index) in parts" :key="part.id ?? index">
      <div
        v-if="part.type === 'text'"
        class="agent-message"
        :class="part.role === 'user' ? 'agent-message--user' : 'agent-message--assistant'"
      >
        <p v-if="part.role === 'user'" class="agent-message__role">{{ t('agent.chat.roleUser') }}</p>
        <p class="agent-message__text">{{ part.text }}</p>
      </div>

      <details v-else-if="part.type === 'reasoning'" class="agent-part-entry agent-part-entry--thinking" data-agent-thinking>
        <summary class="agent-part-entry__head">
          <Sparkles :size="14" aria-hidden="true" />
          <span class="agent-part-entry__title">{{ t('agent.chat.thinking') }}</span>
        </summary>
        <p class="agent-part-entry__text">{{ part.text }}</p>
      </details>

      <details
        v-else-if="part.type === 'tool'"
        class="agent-part-entry agent-part-entry--tool"
        :class="`agent-part-entry--${part.status}`"
        data-agent-tool
      >
        <summary class="agent-part-entry__head">
          <Wrench :size="14" aria-hidden="true" />
          <span class="agent-part-entry__title">{{ bareTool(part.tool) }}</span>
          <span class="agent-part-entry__status">{{ statusLabel(part.status) }}</span>
        </summary>
        <div v-if="part.input || part.output || part.error || part.input_omitted?.length" class="agent-tool-activity__detail" data-agent-tool-detail>
          <template v-if="part.input && part.input !== '{}'">
            <p class="agent-tool-activity__label">{{ t('agent.chat.toolInput') }}</p>
            <AgentCollapsibleOutput :content="pretty(part.input)" />
          </template>
          <p v-if="part.input_omitted?.length" class="agent-tool-activity__label" data-agent-tool-defaults>
            {{ t('agent.chat.toolDefaultsOmitted', { items: part.input_omitted.join(locale === 'en' ? ', ' : '、') }) }}
          </p>
          <template v-if="part.output">
            <p class="agent-tool-activity__label">{{ t('agent.chat.toolOutput') }}</p>
            <AgentCollapsibleOutput :content="pretty(part.output)" :status="part.status" />
          </template>
          <template v-if="part.error">
            <p class="agent-tool-activity__label">{{ t('agent.chat.toolError') }}</p>
            <AgentCollapsibleOutput :content="part.error" status="error" code-class="agent-tool-activity__code--error" />
          </template>
        </div>
      </details>

      <div v-else-if="part.type === 'step-start'" class="agent-part-step" aria-hidden="true">
        <span>{{ t('agent.chat.stepStart') }}</span>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
//: OpenCode-parity conversation renderer: consumes the bounded session parts
//: returned by `/api/v1/agent/conversations/{id}/parts` and renders text,
//: reasoning, structured tool input/output, and step markers.
import { Sparkles, Wrench } from '@lucide/vue'
import { useI18n } from '../../i18n'
import AgentCollapsibleOutput from './AgentCollapsibleOutput.vue'

const { t, locale } = useI18n()

export interface AgentConversationPart {
  type: string
  id?: string | null
  role?: string | null
  text?: string | null
  tool?: string | null
  status?: string | null
  input?: string | null
  input_omitted?: string[] | null
  output?: string | null
  error?: string | null
  reason?: string | null
}

defineProps<{ parts: readonly AgentConversationPart[] }>()

const STATUS_LABEL_KEYS: Record<string, string> = {
  completed: 'agent.chat.toolStatus.completed',
  error: 'agent.chat.toolStatus.failed',
  failed: 'agent.chat.toolStatus.failed',
  running: 'agent.chat.toolStatus.running',
  pending: 'agent.chat.toolStatus.pending',
}

function bareTool(name: string | null | undefined): string {
  const bare = (name ?? '').replace(/^termflow_termflow_/, '').replace(/^termflow_/, '')
  return bare === '' ? t('agent.chat.toolCall') : bare
}

function statusLabel(status: string | null | undefined): string {
  const key = STATUS_LABEL_KEYS[status ?? '']
  return key ? t(key) : (status ?? '')
}

function pretty(value: string): string {
  try {
    return JSON.stringify(JSON.parse(value), null, 2)
  } catch {
    return value
  }
}
</script>
