<template>
  <div class="agent-chat-region">
    <div
      ref="listEl"
      class="agent-message-list"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      data-agent-message-list
      @scroll="onScroll"
    >
      <template v-for="item in items" :key="item.key">
        <AgentMessageBubble v-if="item.kind === 'message' || item.kind === 'user'" :message="item.message" />
        <AgentToolActivity v-else-if="item.kind === 'tool'" :call="item.call" />
        <AgentApprovalCard v-else-if="item.kind === 'permission'" :permission="item.permission" @focus="onFocusApproval" />
      </template>
    </div>
    <div class="sr-only" role="status" aria-live="polite" data-agent-status>{{ statusText }}</div>
  </div>
</template>

<script setup lang="ts">
//: Message flow container (M6b spec §4.7). The `role="log"` region with
//: `aria-relevant="additions"` announces only newly appended bubbles —
//: streaming text updates inside an existing bubble stay silent. Coarse
//: fine-grained events (tool done/failed, run finished/error, approval
//: arrival, backend state change) are announced through the visually
//: hidden `role="status"` region — never per chunk. Tool activity rows and
//: approval cards are rendered inline in timeline order (arrival = server
//: seq order), so the whole flow stays inside the single live region;
//: the approval card's 在审批面板处理 action bubbles up as
//: `focus-approval` for the parent to focus the matching panel entry.
//: Auto-scroll only while the user is pinned to the bottom (scroll
//: anchoring); smooth scrolling is disabled under prefers-reduced-motion
//: (app.css).
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import type { AgentHistoryState, AgentMessageState, AgentPermissionState, AgentToolCallState, AgentUserMessageState } from '@termflow/client-core'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentMessageBubble from './AgentMessageBubble.vue'
import AgentToolActivity from './AgentToolActivity.vue'

const props = defineProps<{
  /** Whole reducer state, exactly as returned by the M6b conversation composable. */
  history: AgentHistoryState
}>()

const emit = defineEmits<{
  /** The approval card asked to handle its request in the approval panel. */
  'focus-approval': [approvalId: string]
}>()

type DisplayItem =
  | { kind: 'message'; key: string; message: AgentMessageState }
  | { kind: 'user'; key: string; message: AgentUserMessageState }
  | { kind: 'tool'; key: string; call: AgentToolCallState }
  | { kind: 'permission'; key: string; permission: AgentPermissionState }

// Timeline order = arrival order = server seq order; refs without a state
// record (dangling timeline entries) are skipped defensively.
const items = computed<DisplayItem[]>(() => {
  const out: DisplayItem[] = []
  for (const entry of props.history.timeline) {
    if (entry.type === 'message') {
      const message = props.history.messages.get(entry.refId)
      if (message !== undefined) out.push({ kind: 'message', key: `message:${entry.refId}`, message })
    } else if (entry.type === 'user') {
      const message = props.history.userMessages.get(entry.refId)
      if (message !== undefined) out.push({ kind: 'user', key: `user:${entry.refId}`, message })
    } else if (entry.type === 'tool') {
      const call = props.history.toolCalls.get(entry.refId)
      if (call !== undefined) out.push({ kind: 'tool', key: `tool:${entry.refId}`, call })
    } else if (entry.type === 'permission') {
      const permission = props.history.permissions.get(entry.refId)
      if (permission !== undefined) out.push({ kind: 'permission', key: `permission:${entry.refId}`, permission })
    }
  }
  return out
})

function onFocusApproval(approvalId: string) {
  emit('focus-approval', approvalId)
}

const listEl = ref<HTMLElement | null>(null)
const statusText = ref('')
const STICK_THRESHOLD_PX = 24
let stickToBottom = true
let previousHistory: AgentHistoryState | null = null

function isAtBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX
}

function onScroll() {
  const el = listEl.value
  if (el !== null) stickToBottom = isAtBottom(el)
}

function scrollToBottom() {
  const el = listEl.value
  if (el !== null) el.scrollTop = el.scrollHeight
}

onMounted(scrollToBottom)

watch(
  () => props.history,
  (next) => {
    statusText.value = deriveAnnouncements(previousHistory, next).join('；')
    previousHistory = next
    if (stickToBottom) void nextTick(scrollToBottom)
  },
  { immediate: true },
)

/**
 * Coarse-grained status announcements: only whole-item transitions enter
 * the status region. Message chunks are never announced, and the initial
 * (replayed) state is never announced — `previous === null` suppresses the
 * mount flood. The region is cleared when an update carries nothing new,
 * so repeated announcements of the same kind re-fire.
 */
function deriveAnnouncements(previous: AgentHistoryState | null, next: AgentHistoryState): string[] {
  if (previous === null) return []
  const out: string[] = []
  for (const [id, call] of next.toolCalls) {
    const before = previous.toolCalls.get(id)
    const label = call.toolName === '' ? '调用' : call.toolName
    if (call.status === 'completed' && (before === undefined || before.status === 'running')) out.push(`工具 ${label} 已完成`)
    else if (call.status === 'failed' && (before === undefined || before.status === 'running')) out.push(`工具 ${label} 执行失败`)
  }
  for (const [id, run] of next.runs) {
    const before = previous.runs.get(id)
    if (run.status === 'finished' && (before === undefined || before.status === 'active')) out.push('运行已完成')
    else if (run.status === 'error' && (before === undefined || before.status === 'active')) out.push('运行出错')
  }
  for (const [id, permission] of next.permissions) {
    if (permission.state === 'pending' && !previous.permissions.has(id)) out.push('收到新的审批请求')
  }
  const state = next.backend.state
  if (state !== null && state !== previous.backend.state) out.push(`后端状态：${state}`)
  return out
}
</script>
