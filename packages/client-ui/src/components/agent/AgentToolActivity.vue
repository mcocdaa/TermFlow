<template>
  <div class="agent-tool-activity" :class="`agent-tool-activity--${call.status}`" :data-agent-tool-status="call.status">
    <button
      v-if="hasSummary"
      type="button"
      class="agent-tool-activity__toggle"
      :aria-expanded="expanded ? 'true' : 'false'"
      :aria-controls="summaryId"
      :data-agent-tool-toggle="call.toolCallId"
      @click="expanded = !expanded"
    >
      <span class="agent-tool-activity__name">{{ displayName }}</span>
      <span class="agent-tool-activity__status" data-agent-tool-status-label>{{ statusLabel }}</span>
      <span class="agent-tool-activity__chevron" aria-hidden="true">
        <ChevronDown v-if="expanded" :size="15" />
        <ChevronRight v-else :size="15" />
      </span>
    </button>
    <div v-else class="agent-tool-activity__row">
      <span class="agent-tool-activity__name">{{ displayName }}</span>
      <span class="agent-tool-activity__status" data-agent-tool-status-label>{{ statusLabel }}</span>
    </div>
    <div v-if="expanded && hasSummary" :id="summaryId" class="agent-tool-activity__summary" data-agent-tool-summary>
      <AgentCollapsibleOutput class="agent-tool-activity__summary-text" :content="summaryText" :status="call.status" />
    </div>
  </div>
</template>

<script setup lang="ts">
//: Tool activity row (M6b spec §4.7): tool name + running/completed/failed
//: status + a collapsible summary toggled by a native `<button
//: aria-expanded>` (keyboard-reachable, no custom focus handling). The
//: summary is pure-text interpolation only — no v-html — with ANSI/OSC
//: escape noise stripped, so summary JSON, HTML or URL payloads can never
//: become elements (§6.2). Calls still running carry no summary and render
//: a static row (nothing to expand).
import { computed, ref, useId } from 'vue'
import { ChevronDown, ChevronRight } from '@lucide/vue'
import { stripAnsiOsc, type AgentToolCallState } from '@termflow/client-core'
import AgentCollapsibleOutput from './AgentCollapsibleOutput.vue'

const props = defineProps<{
  /** Tool call record, exactly as tracked by the M6b history reducer. */
  call: AgentToolCallState
}>()

const STATUS_LABELS: Record<AgentToolCallState['status'], string> = {
  running: '运行中',
  completed: '已完成',
  failed: '执行失败',
}

const expanded = ref(false)
const summaryId = useId()
const hasSummary = computed(() => props.call.summary !== null)
// A RESULT-without-START backfill has an empty tool name (history.ts).
// OpenCode namespaces remote MCP tools as ``<server>_<tool>``, so the wire
// name carries a doubled ``termflow_`` prefix; show the bare tool name.
const displayName = computed(() => {
  const bare = props.call.toolName.replace(/^termflow_termflow_/, '').replace(/^termflow_/, '')
  return bare === '' ? '工具调用' : bare
})
const statusLabel = computed(() => STATUS_LABELS[props.call.status])
const summaryText = computed(() => stripAnsiOsc(props.call.summary ?? ''))
</script>
