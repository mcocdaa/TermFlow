<template>
  <article
    class="agent-approval-card"
    data-agent-approval-card
    :data-agent-approval-id="permission.approvalId"
    :data-agent-approval-detail-failed="detailFailed ? 'true' : undefined"
    :aria-busy="detailLoading ? 'true' : 'false'"
  >
    <header class="agent-approval-card__header">
      <strong class="agent-approval-card__tool" data-agent-approval-tool>{{ tool }}</strong>
      <span class="agent-approval-card__state" :data-agent-approval-state="rawState">{{ stateLabel }}</span>
    </header>
    <p v-if="evidence !== null" class="agent-approval-card__evidence" data-agent-approval-evidence>{{ evidence }}</p>
    <div v-if="detail !== null" class="agent-approval-card__detail">
      <p class="agent-approval-card__field"><span class="agent-approval-card__label">Pane</span><span data-agent-approval-pane>{{ pane }}</span></p>
      <p class="agent-approval-card__field"><span class="agent-approval-card__label">操作</span><span data-agent-approval-operation>{{ operation }}</span></p>
      <p class="agent-approval-card__summary" data-agent-approval-summary>{{ summary }}</p>
      <p class="agent-approval-card__hash" :data-agent-approval-hash="detail.canonical_hash">摘要 #{{ detail.canonical_hash.slice(0, 8) }}</p>
    </div>
    <p v-else-if="detailFailed" class="agent-approval-card__fallback" data-agent-approval-detail-failed-text>详情不可用</p>
    <footer class="agent-approval-card__footer">
      <span v-if="expiry !== null" class="agent-approval-card__expires" data-agent-approval-expires>有效期至 {{ expiry }}</span>
      <button type="button" class="agent-approval-card__action" data-action="focus-approval" @click="emit('focus', permission.approvalId)">在审批面板处理</button>
    </footer>
  </article>
</template>

<script setup lang="ts">
//: Timeline inline read-only approval card (M6b spec §4.7). The card's
//: visibility is driven by a CUSTOM termflow.permission_requested event
//: (the history reducer's permission record); it renders those fields
//: immediately, then lazily loads the authoritative REST detail on mount —
//: no spinner wall, aria-busy marks the in-flight load. A failed detail
//: degrades to the CUSTOM data plus a 详情不可用 marker; the card never
//: offers decision actions, only a 在审批面板处理 button that emits
//: ``focus`` with the approval id so the parent can focus the matching
//: panel entry. All text is pure interpolation (no v-html) with ANSI/OSC
//: stripped before rendering; unknown states label 未知 while the raw
//: value stays in the data attribute.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, createApprovalsApi, stripAnsiOsc, type AgentPermissionState } from '@termflow/client-core'
import type { ApprovalDetailResponse } from '@termflow/client-contracts'
import { useClientRuntime } from '../../runtime'

const props = defineProps<{
  /** CUSTOM-event-derived record, exactly as tracked by the history reducer. */
  permission: AgentPermissionState
}>()
const emit = defineEmits<{ focus: [approvalId: string] }>()

const runtime = useClientRuntime()
const approvalsApi = createApprovalsApi(runtime.api.request)
const controller = new AbortController()
const detail = ref<ApprovalDetailResponse | null>(null)
const detailLoading = ref(true)
const detailFailed = ref(false)

onMounted(async () => {
  try {
    detail.value = await approvalsApi.detail(props.permission.approvalId, controller.signal)
  } catch (error) {
    if (!(error instanceof ApiError && error.kind === 'aborted')) detailFailed.value = true
  } finally {
    detailLoading.value = false
  }
})
onBeforeUnmount(() => controller.abort())

const STATE_LABELS: Record<string, string> = {
  pending: '待处理',
  approved: '已批准',
  denied: '已拒绝',
  expired: '已过期',
  revoked: '已撤销',
  consumed: '已消费',
}

/** REST detail is authoritative once loaded; the CUSTOM record fills in before/on failure. */
const rawState = computed(() => detail.value?.state ?? props.permission.state)
const stateLabel = computed(() => STATE_LABELS[rawState.value] ?? '未知')
const tool = computed(() => (props.permission.toolName !== null ? stripAnsiOsc(props.permission.toolName) : '工具调用'))
const evidence = computed(() => (props.permission.evidence !== null ? stripAnsiOsc(props.permission.evidence) : null))
const pane = computed(() => (detail.value?.pane_id !== null && detail.value?.pane_id !== undefined ? stripAnsiOsc(detail.value.pane_id) : '详情不可用'))
const operation = computed(() => (detail.value?.operation !== null && detail.value?.operation !== undefined ? stripAnsiOsc(detail.value.operation) : '详情不可用'))
const summary = computed(() => {
  const value = detail.value?.intent_summary
  return value !== null && value !== undefined ? stripAnsiOsc(value) : '详情不可用'
})
const expiry = computed(() => {
  const iso = detail.value?.expires_at ?? props.permission.expiresAt
  return iso !== null && iso !== undefined ? formatExpiry(iso) : null
})

/** Deterministic UTC timestamp (tests inject a fixed clock-free ISO string). */
function formatExpiry(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())} UTC`
}
</script>
