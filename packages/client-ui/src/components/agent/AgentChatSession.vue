<template>
  <section class="agent-chat-session" :class="`agent-chat-session--${variant}`">
  <header v-if="variant !== 'floating'" class="agent-chat-header" data-agent-chat-header>
    <div class="agent-chat-header__left">
      <RouterLink v-if="variant === 'page'" to="/agent" class="text-button agent-chat-header__back" data-testid="agent-overview-link" data-action="back-to-overview">
        <ArrowLeft :size="16" aria-hidden="true" />
        <span>Agent 总览</span>
      </RouterLink>
      <div>
        <p class="eyebrow">Agent Chat</p>
        <h1 data-agent-chat-title>{{ title }}</h1>
        <p class="agent-chat-header__binding" data-agent-chat-binding>{{ bindingLabel }}</p>
      </div>
    </div>
    <div class="agent-chat-header__actions">
      <div class="agent-policy-toggle" role="group" aria-label="本对话写入审批" data-agent-conversation-policy>
        <button type="button" :class="{ 'is-active': conversationPolicy === 'manual' }" :aria-pressed="conversationPolicy === 'manual'" :disabled="policyPending" data-action="conversation-policy-manual" @click="changePolicy('manual')">手动审批</button>
        <button type="button" :class="{ 'is-active': conversationPolicy === 'auto' }" :aria-pressed="conversationPolicy === 'auto'" :disabled="policyPending" data-action="conversation-policy-auto" @click="policyConfirm = true">完全放行</button>
      </div>
      <AgentBackendStatus :state="backendState" />
      <button
        v-if="hasActiveRun"
        type="button"
        class="secondary-button"
        :disabled="canceling"
        :aria-busy="canceling ? 'true' : undefined"
        data-action="cancel-run"
        @click="cancelRun"
      >
        {{ canceling ? '正在取消…' : '取消运行' }}
      </button>
      <button
        v-if="variant === 'page'"
        type="button"
        class="icon-button icon-only"
        :disabled="deletePending"
        aria-label="删除会话"
        title="删除会话"
        data-action="delete-conversation"
        @click="deleteConfirm = true"
      >
        <Trash2 :size="16" aria-hidden="true" />
      </button>
    </div>
  </header>

  <div v-if="variant === 'floating'" class="agent-floating-session-status" data-agent-floating-session-status>
    <AgentBackendStatus :state="backendState" />
    <button
      v-if="hasActiveRun"
      type="button"
      class="text-button compact"
      :disabled="canceling"
      :aria-busy="canceling ? 'true' : undefined"
      data-action="cancel-run"
      @click="cancelRun"
    >
      {{ canceling ? '正在取消…' : '取消运行' }}
    </button>
  </div>

  <div v-if="bindingRevoked" class="agent-chat-banner" role="alert" data-agent-revoked-banner>
    {{ revokedBannerText }}
  </div>

  <div v-if="deleteConfirm" class="agent-policy-confirm" role="alertdialog" aria-label="确认删除会话" data-agent-delete-confirm>
    <p>删除后该会话及其事件会被清理，无法恢复。</p>
    <div class="agent-policy-confirm__actions">
      <button type="button" class="danger-button" :disabled="deletePending" data-action="confirm-delete-conversation" @click="removeConversation">确认删除</button>
      <button type="button" class="text-button" :disabled="deletePending" @click="deleteConfirm = false">取消</button>
    </div>
  </div>

  <div v-if="policyConfirm" class="agent-policy-confirm" role="alertdialog" aria-label="确认完全放行本对话" data-agent-policy-confirm>
    <p>本对话内，Agent 对已授权 Pane 的每次写入都会立即执行，不再弹审批。</p>
    <div class="agent-policy-confirm__actions">
      <button type="button" class="primary-button" :disabled="policyPending" data-action="confirm-conversation-policy" @click="changePolicy('auto')">确认放行</button>
      <button type="button" class="text-button" :disabled="policyPending" @click="policyConfirm = false">取消</button>
    </div>
  </div>

  <div class="agent-chat-body">
    <AgentPartsList v-if="parts.length > 0" :parts="parts" />
    <AgentMessageList v-else :history="displayHistory" @focus-approval="focusApproval" />
  </div>

  <!-- Approval prompt sits directly above the composer (Codex-style). The
       panel stays mounted so it can report the pending count. -->
  <section v-show="pendingCount > 0" class="agent-approval-prompt" role="region" aria-label="待批准操作" data-agent-approval-prompt>
    <AgentApprovalPanel ref="approvalPanel" :conversation-id="conversationId" :history="conversation.history.value" @pending-count="updatePendingCount" />
  </section>

  <AgentComposer
    :conversation-id="conversationId"
    :disabled="bindingRevoked"
    :backend-state="backendState"
    @submitted="echoUserMessage"
  />
  </section>
</template>

<script setup lang="ts">
//: Conversation-scoped Agent chat body (M6b spec §4.7/§4.8): detail header
//: (title/binding/term + backend status badge + cancel while a run is
//: active), the message flow (AgentMessageList with timeline-integrated
//: tool rows and approval cards), the approval panel, and the composer.
//: useAgentConversation owns the lifecycle — historical user seed before
//: the stream connects, 4401 → clearSessionState + /login redirect, 4412 →
//: revoked banner + fail-closed composer + cursor clear, 4410 → recovery
//: toast, dispose on unmount. Historical rows with a null body degrade to
//: a 历史消息内容不可用 placeholder (spec §6.6); everything renders pure
//: text. AgentChatView renders this component keyed by the conversation
//: id, so a route-param change (/agent/a → /agent/b on the same route
//: record) remounts it and re-scopes the whole conversation state.
import { ApiError, type AgentHistoryState, type AgentUserMessageState } from '@termflow/client-core'
import type { AgentConversationDetailResponse } from '@termflow/client-contracts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ArrowLeft, Trash2 } from '@lucide/vue'
import AgentApprovalPanel from './AgentApprovalPanel.vue'
import AgentBackendStatus from './AgentBackendStatus.vue'
import AgentComposer from './AgentComposer.vue'
import AgentPartsList, { type AgentConversationPart } from './AgentPartsList.vue'
import AgentMessageList from './AgentMessageList.vue'
import { useAgentConversation } from '../../composables/useAgentConversation'
import { useBottomToast } from '../../composables/useBottomToast'
import { useClientRuntime } from '../../runtime'

const props = withDefaults(defineProps<{
  /** Conversation scope; the parent keys this component on it (remount = re-scope). */
  conversationId: string
  /** Capability gate forwarded from the parent — always true while mounted. */
  enabled: boolean
  variant?: 'page' | 'sidecar' | 'floating'
}>(), { variant: 'page' })
const emit = defineEmits<{ close: []; pendingCount: [count: number] }>()
const approvalPanel = ref<InstanceType<typeof AgentApprovalPanel> | null>(null)
const pendingCount = ref(0)
function updatePendingCount(count: number) { pendingCount.value = count; emit('pendingCount', count) }
// A pending write approval opens the centered modal immediately; the user can
// dismiss it and reopen it from the inline button while it stays pending.
watch(pendingCount, (count) => { if (count > 0) void nextTick(() => approvalPanel.value?.focusApproval('')) })

const runtime = useClientRuntime()
const toast = useBottomToast()
const conversation = useAgentConversation({
  conversationId: props.conversationId,
  enabled: () => props.enabled,
})

const parts = ref<AgentConversationPart[]>([])
let partsTimer: number | null = null
async function refreshParts() {
  try {
    const response = await runtime.api.agents.getConversationParts(props.conversationId)
    parts.value = response.parts
  } catch {
    // Transient while the runtime reconnects; the next poll retries.
  }
}
onMounted(() => {
  void refreshParts()
  partsTimer = window.setInterval(() => { if (!document.hidden) void refreshParts() }, 2000)
})
onBeforeUnmount(() => { if (partsTimer !== null) window.clearInterval(partsTimer) })

const detail = ref<AgentConversationDetailResponse | null>(null)
let controller: AbortController | null = null

const backendState = computed(() => conversation.history.value.backend.state)
const hasActiveRun = computed(() =>
  [...conversation.history.value.runs.values()].some((run) => run.status === 'active'),
)
const bindingRevoked = computed(() => conversation.bindingRevoked.value)
const conversationPolicy = computed<'manual' | 'auto'>(() =>
  detail.value?.write_policy === 'auto' ? 'auto' : 'manual',
)
const policyPending = ref(false)
const policyConfirm = ref(false)
const deleteConfirm = ref(false)
const deletePending = ref(false)
const router = useRouter()
async function removeConversation() {
  if (deletePending.value) return
  deletePending.value = true
  try {
    await runtime.api.agents.deleteConversation(props.conversationId)
    await router.push('/agent')
  } catch (error) {
    toast.show({ text: error instanceof Error ? error.message : '删除失败，请稍后重试。', tone: 'error' })
  } finally {
    deletePending.value = false
    deleteConfirm.value = false
  }
}
async function changePolicy(policy: 'manual' | 'auto') {
  if (policyPending.value) return
  policyPending.value = true
  try {
    await runtime.api.agents.setConversationWritePolicy(props.conversationId, policy)
    policyConfirm.value = false
    await loadDetail()
  } catch (error) {
    toast.show({ text: error instanceof Error ? error.message : '无法更新审批方式。', tone: 'error' })
  } finally {
    policyPending.value = false
  }
}
const revokedBannerText = computed(() =>
  conversation.closeReason.value === 'conversation_not_found'
    ? '该会话不存在或已被删除，无法继续连接。'
    : 'Agent Binding 已撤销，无法继续连接。',
)
const title = computed(() => detail.value?.title ?? `会话 ${props.conversationId.slice(0, 8)}`)
const bindingLabel = computed(() => {
  const binding = detail.value?.binding
  return binding === undefined
    ? `会话 ${props.conversationId.slice(0, 8)}`
    : `Binding ${binding.binding_id} · Term ${binding.term_id}`
})

/**
 * Historical user rows seeded with a null body carry an empty text; the
 * composer never produces empty echoes, so this condition uniquely marks
 * the pre-migration rows (spec §6.6 placeholder degradation).
 */
const displayHistory = computed<AgentHistoryState>(() => {
  const base = conversation.history.value
  let mapped: Map<string, AgentUserMessageState> | null = null
  for (const [id, message] of base.userMessages) {
    if (message.text === '' && message.deliveryState === 'accepted') {
      if (mapped === null) mapped = new Map(base.userMessages)
      mapped.set(id, { ...message, text: '历史消息内容不可用' })
    }
  }
  return mapped === null ? base : { ...base, userMessages: mapped }
})

let echoSequence = 0
/** 202 accepted: local user-bubble echo, appended in reducer shape (spec §4.8). */
function echoUserMessage(text: string) {
  const now = runtime.clock.now()
  const clientId = `echo-${now}-${echoSequence++}`
  const base = conversation.history.value
  conversation.history.value = {
    ...base,
    userMessages: new Map(base.userMessages).set(clientId, {
      clientId,
      text,
      deliveryState: 'accepted',
      error: null,
    }),
    timeline: [...base.timeline, { type: 'user', refId: clientId, at: now }],
  }
}

const canceling = ref(false)
async function cancelRun() {
  if (canceling.value) return
  canceling.value = true
  try {
    await runtime.api.agents.cancelRun(props.conversationId, {}, controller?.signal)
  } catch (error) {
    // 409 no_active_run: the local run state was already stale — settle the
    // active runs locally so the cancel button disappears instead of
    // lingering on a run the server no longer knows (no toast: not an
    // error).
    if (error instanceof ApiError && error.status === 409) {
      const now = runtime.clock.now()
      const base = conversation.history.value
      const runs = new Map(base.runs)
      for (const [runId, run] of runs) {
        if (run.status === 'active') runs.set(runId, { ...run, status: 'finished', endedAt: now })
      }
      conversation.history.value = { ...base, runs }
    } else {
      toast.show({ text: error instanceof Error ? error.message : '取消失败，请稍后重试。', tone: 'error' })
    }
  } finally {
    canceling.value = false
  }
}

/** The approval card asked to handle its request: focus the panel entry. */
function focusApproval(approvalId: string) {
  void nextTick(() => approvalPanel.value?.focusApproval(approvalId))
}
defineExpose({ focusApproval })

async function loadDetail() {
  try {
    detail.value = await runtime.api.agents.getConversation(props.conversationId, controller?.signal)
  } catch {
    // Title/binding stay at the id fallback; the stream still runs.
  }
}

onMounted(() => {
  controller = new AbortController()
  // The component only mounts behind the capability gate, so the detail
  // header can load immediately (and reloads on every key remount).
  void loadDetail()
})
onBeforeUnmount(() => {
  controller?.abort()
  controller = null
})
</script>
