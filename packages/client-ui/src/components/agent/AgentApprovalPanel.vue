<template>
  <div class="agent-approval-panel" data-agent-approval-panel>
    <p v-if="loading && visibleApprovals.length === 0" class="agent-approval-panel__state" data-agent-approval-loading>加载中…</p>
    <p v-else-if="visibleApprovals.length === 0" class="agent-approval-panel__state" data-agent-approval-empty>暂无待处理审批</p>
    <div v-else class="agent-approval-panel__list">
      <section
        v-for="approval in visibleApprovals"
        :key="approval.approval_id"
        class="agent-approval-item"
        data-agent-approval-item
        :data-agent-approval-id="approval.approval_id"
        :aria-busy="isBusy(approval.approval_id) ? 'true' : 'false'"
      >
        <div class="agent-approval-item__meta">
          <span class="agent-approval-item__pane" data-agent-approval-pane>{{ approval.pane_id !== null ? stripAnsiOsc(approval.pane_id) : '详情不可用' }}</span>
          <span class="agent-approval-item__operation" data-agent-approval-operation>{{ approval.operation !== null ? stripAnsiOsc(approval.operation) : '详情不可用' }}</span>
          <span class="agent-approval-item__countdown" data-agent-approval-countdown>{{ countdown(approval.expires_at) }}</span>
        </div>
        <p class="agent-approval-item__summary" data-agent-approval-summary>{{ approval.intent_summary !== null ? stripAnsiOsc(approval.intent_summary) : '详情不可用' }}</p>
        <div class="agent-approval-item__footer">
          <span class="agent-approval-item__hash" :data-agent-approval-hash="approval.canonical_hash">摘要 #{{ approval.canonical_hash.slice(0, 8) }}</span>
          <span class="agent-approval-item__actions">
            <button
              type="button"
              class="agent-approval-button agent-approval-button--approve"
              data-action="approve-approval"
              :disabled="isBusy(approval.approval_id)"
              @click="openConfirm(approval.approval_id)"
            >批准</button>
            <button
              type="button"
              class="agent-approval-button agent-approval-button--deny"
              data-action="deny-approval"
              :disabled="isBusy(approval.approval_id)"
              @click="decide(approval.approval_id, 'deny')"
            >拒绝</button>
            <button
              type="button"
              class="agent-approval-button"
              data-action="revoke-approval"
              :disabled="isBusy(approval.approval_id)"
              @click="revoke(approval.approval_id)"
            >撤销</button>
          </span>
        </div>
      </section>
    </div>

    <div v-if="pendingApproveId !== null" class="dialog-backdrop" @click.self="closeConfirm">
      <section
        ref="dialogPanel"
        class="dialog-panel agent-approval-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="agent-approval-confirm-title"
        aria-describedby="agent-approval-confirm-description"
        data-agent-approval-dialog
        @keydown="trapFocus"
      >
        <h2 id="agent-approval-confirm-title">批准该审批请求？</h2>
        <p id="agent-approval-confirm-description">
          批准后 Agent 将立即执行
          <strong>{{ confirmTarget === undefined ? '未知操作' : confirmTarget.operation !== null ? stripAnsiOsc(confirmTarget.operation) : '未知操作' }}</strong>。
          此操作不可撤销，请确认内容无误。
        </p>
        <p v-if="confirmTarget !== undefined && confirmTarget.intent_summary !== null" class="agent-approval-dialog__summary">{{ stripAnsiOsc(confirmTarget.intent_summary) }}</p>
        <div class="dialog-actions">
          <button ref="cancelButton" class="text-button" type="button" data-action="approve-cancel" @click="closeConfirm">取消</button>
          <button
            class="primary-button"
            type="button"
            data-action="approve-confirm"
            :disabled="pendingApproveId !== null && isBusy(pendingApproveId)"
            @click="confirmApprove"
          >确认批准</button>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
//: Conversation-scoped pending approval panel (M6b spec §4.7). The B
//: approvals REST is the source of truth: the full list is fetched on
//: mount, refreshed after every successful decide/revoke, and refreshed
//: again whenever the history timeline gains a NEW pending permission id
//: (a fresh CUSTOM event) — never by polling. Each entry shows
//: pane/operation/intent_summary with a 详情不可用 fallback plus the
//: canonical hash digest, an expires_at countdown ticking on the injected
//: clock port, and native approve/deny/revoke buttons. Approve is a
//: high-impact action and goes through a confirm dialog that reuses the
//: ClosePaneDialog focus-trap pattern (Escape cancels, focus restored);
//: deny/revoke execute directly. Rows carry aria-busy while their request
//: is in flight (M6b spec §6.5). All text is pure interpolation (no
//: v-html) with ANSI/OSC stripped before rendering.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { stripAnsiOsc, type AgentHistoryState } from '@termflow/client-core'
import { useAgentApprovals } from '../../composables/useAgentApprovals'
import { useClientRuntime } from '../../runtime'

const props = defineProps<{
  /** Conversation scope for the approvals list (B REST filter). */
  conversationId: string
  /**
   * Whole reducer history (M6b §4.5). When provided, a NEW pending
   * permission id (a CUSTOM termflow.permission_requested event) triggers
   * a list refresh; replay/live duplicates of already-seen ids do not.
   */
  history?: AgentHistoryState
}>()

const runtime = useClientRuntime()
const { approvals, loading, isBusy, refresh, decide, revoke } = useAgentApprovals({
  conversationId: props.conversationId,
})

/** The panel is the pending list; decided/expired rows fall away after refresh. */
const visibleApprovals = computed(() => approvals.value.filter((approval) => approval.state === 'pending'))

// Countdown ticks once per second on the injected clock port (the browser
// adapter wires the platform timer; tests inject a controllable one).
const nowMs = ref(runtime.clock.now())
let interval: unknown | null = null
onMounted(() => {
  interval = runtime.clock.setInterval(() => {
    nowMs.value = runtime.clock.now()
  }, 1_000)
})
onBeforeUnmount(() => {
  if (interval !== null) runtime.clock.clearInterval(interval)
})

function countdown(expiresAt: string): string {
  const remaining = Date.parse(expiresAt) - nowMs.value
  if (!Number.isFinite(remaining)) return '—'
  if (remaining <= 0) return '已过期'
  const total = Math.floor(remaining / 1_000)
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

// New pending permission ids (CUSTOM arrivals) trigger a refresh — the REST
// list is authoritative for state. No polling: interval ticks only update
// the countdown display.
const pendingPermissionIds = computed(() => {
  if (props.history === undefined) return ''
  return [...props.history.permissions.values()]
    .filter((entry) => entry.state === 'pending')
    .map((entry) => entry.approvalId)
    .sort()
    .join(',')
})
let previousPermissionIds = pendingPermissionIds.value
watch(pendingPermissionIds, (next) => {
  if (next === previousPermissionIds) return
  const before = new Set(previousPermissionIds === '' ? [] : previousPermissionIds.split(','))
  previousPermissionIds = next
  const current = new Set(next === '' ? [] : next.split(','))
  if ([...current].some((id) => !before.has(id))) void refresh()
})

// Approve confirm dialog (ClosePaneDialog focus-trap pattern).
const pendingApproveId = ref<string | null>(null)
const dialogPanel = ref<HTMLElement | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null

const confirmTarget = computed(() =>
  pendingApproveId.value === null
    ? undefined
    : approvals.value.find((approval) => approval.approval_id === pendingApproveId.value),
)

function openConfirm(approvalId: string) {
  restoreFocus = document.activeElement as HTMLElement | null
  pendingApproveId.value = approvalId
  void nextTick(() => cancelButton.value?.focus())
}

function closeConfirm() {
  pendingApproveId.value = null
  void nextTick(() => {
    if (restoreFocus?.isConnected) restoreFocus.focus()
    restoreFocus = null
  })
}

async function confirmApprove() {
  const approvalId = pendingApproveId.value
  if (approvalId === null) return
  // decide() refreshes the list itself (success and 409/410 paths), so the
  // dialog simply closes afterwards; errors surface via toasts.
  await decide(approvalId, 'approve')
  closeConfirm()
}

function trapFocus(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    cancelButton.value?.click()
    return
  }
  if (event.key !== 'Tab' || dialogPanel.value === null) return
  const focusable = [...dialogPanel.value.querySelectorAll<HTMLElement>('button:not(:disabled)')]
  const first = focusable[0]
  const last = focusable.at(-1)
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}
</script>
