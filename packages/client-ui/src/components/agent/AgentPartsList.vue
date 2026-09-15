<template>
  <div class="agent-parts">
    <template v-for="(part, index) in parts" :key="index">
      <div
        v-if="part.type === 'text'"
        class="agent-message"
        :class="part.role === 'user' ? 'agent-message--user' : 'agent-message--assistant'"
      >
        <p v-if="part.role === 'user'" class="agent-message__role">你</p>
        <p class="agent-message__text">{{ part.text }}</p>
      </div>

      <details v-else-if="part.type === 'reasoning'" class="agent-thinking" data-agent-thinking>
        <summary class="agent-thinking__summary">
          <Sparkles :size="14" aria-hidden="true" />
          <span>思考</span>
        </summary>
        <p class="agent-thinking__text">{{ part.text }}</p>
      </details>

      <details
        v-else-if="part.type === 'tool'"
        class="agent-tool-activity"
        :class="`agent-tool-activity--${part.status}`"
        data-agent-tool
      >
        <summary class="agent-tool-activity__row">
          <Wrench :size="14" aria-hidden="true" />
          <span class="agent-tool-activity__name">{{ bareTool(part.tool) }}</span>
          <span class="agent-tool-activity__status">{{ statusLabel(part.status) }}</span>
        </summary>
        <div v-if="part.input || part.output || part.error" class="agent-tool-activity__detail" data-agent-tool-detail>
          <template v-if="part.input && part.input !== '{}'">
            <p class="agent-tool-activity__label">输入</p>
            <pre class="agent-tool-activity__code">{{ pretty(part.input) }}</pre>
          </template>
          <template v-if="part.output">
            <p class="agent-tool-activity__label">结果</p>
            <pre class="agent-tool-activity__code">{{ pretty(part.output) }}</pre>
          </template>
          <template v-if="part.error">
            <p class="agent-tool-activity__label">错误</p>
            <pre class="agent-tool-activity__code agent-tool-activity__code--error">{{ part.error }}</pre>
          </template>
        </div>
      </details>

      <div v-else-if="part.type === 'step-start'" class="agent-part-step" aria-hidden="true">
        <span>开始</span>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
//: OpenCode-parity conversation renderer: consumes the bounded session parts
//: returned by `/api/v1/agent/conversations/{id}/parts` and renders text,
//: reasoning, structured tool input/output, and step markers.
import { Sparkles, Wrench } from '@lucide/vue'

export interface AgentConversationPart {
  type: string
  role?: string | null
  text?: string | null
  tool?: string | null
  status?: string | null
  input?: string | null
  output?: string | null
  error?: string | null
  reason?: string | null
}

defineProps<{ parts: readonly AgentConversationPart[] }>()

const STATUS_LABELS: Record<string, string> = {
  completed: '已完成',
  error: '失败',
  failed: '失败',
  running: '运行中',
  pending: '等待中',
}

function bareTool(name: string | null | undefined): string {
  const bare = (name ?? '').replace(/^termflow_termflow_/, '').replace(/^termflow_/, '')
  return bare === '' ? '工具调用' : bare
}

function statusLabel(status: string | null | undefined): string {
  return STATUS_LABELS[status ?? ''] ?? (status ?? '')
}

function pretty(value: string): string {
  try {
    return JSON.stringify(JSON.parse(value), null, 2)
  } catch {
    return value
  }
}
</script>
