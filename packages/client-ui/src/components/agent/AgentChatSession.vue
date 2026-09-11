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

  <div class="agent-chat-body">
    <AgentMessageList :history="displayHistory" @focus-approval="focusApproval" />
    <details v-if="variant !== 'page'" ref="approvalTray" class="agent-approval-tray">
      <summary>待处理审批 {{ pendingCount }}</summary>
      <AgentApprovalPanel ref="approvalPanel" :conversation-id="conversationId" :history="conversation.history.value" @pending-count="updatePendingCount" />
    </details>
    <AgentApprovalPanel v-else ref="approvalPanel" :conversation-id="conversationId" :history="conversation.history.value" @pending-count="updatePendingCount" />
  </div>

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
import { RouterLink } from 'vue-router'
import { ArrowLeft } from '@lucide/vue'
import AgentApprovalPanel from './AgentApprovalPanel.vue'
import AgentBackendStatus from './AgentBackendStatus.vue'
import AgentComposer from './AgentComposer.vue'
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
const approvalTray = ref<HTMLDetailsElement | null>(null)
const approvalPanel = ref<InstanceType<typeof AgentApprovalPanel> | null>(null)
const pendingCount = ref(0)
function updatePendingCount(count: number) { pendingCount.value = count; emit('pendingCount', count) }
// A pending write approval must be visible without hunting for the tray:
// open it as soon as the panel reports one.
watch(pendingCount, (count) => {
  if (count > 0 && approvalTray.value !== null) approvalTray.value.open = true
})

const runtime = useClientRuntime()
const toast = useBottomToast()
const conversation = useAgentConversation({
  conversationId: props.conversationId,
  enabled: () => props.enabled,
})

const detail = ref<AgentConversationDetailResponse | null>(null)
let controller: AbortController | null = null

const backendState = computed(() => conversation.history.value.backend.state)
const hasActiveRun = computed(() =>
  [...conversation.history.value.runs.values()].some((run) => run.status === 'active'),
)
const bindingRevoked = computed(() => conversation.bindingRevoked.value)
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
  if (approvalTray.value) approvalTray.value.open = true
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
