<template>
  <div class="page agent-chat-view">
    <template v-if="!agentBrokerEnabled">
      <header class="page-heading">
        <div><p class="eyebrow">Agent</p><h1>Agent 控制台</h1></div>
      </header>
      <section class="agent-placeholder" data-agent-disabled>
        <h2>Agent Broker 未启用</h2>
        <p>服务器未开启 Agent Broker 能力，无法使用 Agent 会话。</p>
      </section>
    </template>

    <template v-else>
      <header class="agent-chat-header" data-agent-chat-header>
        <div class="agent-chat-header__left">
          <RouterLink to="/agent" class="text-button agent-chat-header__back" data-action="back-to-overview">← Agent 总览</RouterLink>
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

      <div v-if="bindingRevoked" class="agent-chat-banner" role="alert" data-agent-revoked-banner>
        {{ revokedBannerText }}
      </div>

      <div class="agent-chat-body">
        <AgentMessageList :history="displayHistory" @focus-approval="focusApproval" />
        <AgentApprovalPanel :conversation-id="conversationId" :history="conversation.history.value" />
      </div>

      <AgentComposer
        :conversation-id="conversationId"
        :disabled="bindingRevoked"
        :backend-state="backendState"
        @submitted="echoUserMessage"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
//: Conversation-scoped Agent chat (M6b spec §4.7/§4.8): detail header
//: (title/binding/term + backend status badge + cancel while a run is
//: active), the message flow (AgentMessageList with timeline-integrated
//: tool rows and approval cards), the approval panel, and the composer.
//: useAgentConversation owns the lifecycle — historical user seed before
//: the stream connects, 4401 → clearSessionState + /login redirect, 4412 →
//: revoked banner + fail-closed composer + cursor clear, 4410 → recovery
//: toast, dispose on unmount. Historical rows with a null body degrade to
//: a 历史消息内容不可用 placeholder (spec §6.6); everything renders pure
//: text. A disabled broker renders the placeholder and the stream never
//: starts (capability gate passed into the composable).
import { ApiError, type AgentHistoryState, type AgentUserMessageState } from '@termflow/client-core'
import type { AgentConversationDetailResponse } from '@termflow/client-contracts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import AgentApprovalPanel from '../components/agent/AgentApprovalPanel.vue'
import AgentBackendStatus from '../components/agent/AgentBackendStatus.vue'
import AgentComposer from '../components/agent/AgentComposer.vue'
import AgentMessageList from '../components/agent/AgentMessageList.vue'
import { useAgentBroker } from '../composables/useAgentBroker'
import { useAgentConversation } from '../composables/useAgentConversation'
import { useBottomToast } from '../composables/useBottomToast'
import { useClientRuntime } from '../runtime'

const route = useRoute()
const runtime = useClientRuntime()
const toast = useBottomToast()
const conversationId = computed(() => String(route.params.conversationId))

const { agentBrokerEnabled } = useAgentBroker()
const conversation = useAgentConversation({
  conversationId: conversationId.value,
  enabled: agentBrokerEnabled,
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
const title = computed(() => detail.value?.title ?? `会话 ${conversationId.value.slice(0, 8)}`)
const bindingLabel = computed(() => {
  const binding = detail.value?.binding
  return binding === undefined
    ? `会话 ${conversationId.value.slice(0, 8)}`
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
    await runtime.api.agents.cancelRun(conversationId.value, {}, controller?.signal)
  } catch (error) {
    // 409 no_active_run: the UI was already stale — not an error.
    if (!(error instanceof ApiError && error.status === 409)) {
      toast.show({ text: error instanceof Error ? error.message : '取消失败，请稍后重试。', tone: 'error' })
    }
  } finally {
    canceling.value = false
  }
}

/** The approval card asked to handle its request: focus the panel entry. */
function focusApproval(approvalId: string) {
  void nextTick(() => {
    const entry = document.querySelector(`[data-agent-approval-item][data-agent-approval-id="${approvalId}"]`)
    const firstButton = entry?.querySelector('button')
    if (firstButton instanceof HTMLElement) firstButton.focus()
  })
}

async function loadDetail() {
  try {
    detail.value = await runtime.api.agents.getConversation(conversationId.value, controller?.signal)
  } catch {
    // Title/binding stay at the id fallback; the stream still runs.
  }
}

// The detail header loads once the capability gate opens.
watch(agentBrokerEnabled, (enabled) => {
  if (enabled) void loadDetail()
})
onMounted(() => {
  controller = new AbortController()
})
onBeforeUnmount(() => {
  controller?.abort()
  controller = null
})
</script>
