<template>
  <div class="page agent-view">
    <header class="page-heading">
      <div><p class="eyebrow">Agent</p><h1>Agent 控制台</h1></div>
    </header>

    <section v-if="!agentBrokerEnabled" class="agent-placeholder" data-agent-disabled>
      <h2>Agent Broker 未启用</h2>
      <p>服务器未开启 Agent Broker 能力，无法使用 Agent 会话。</p>
    </section>

    <template v-else>
      <section class="settings-panel agent-binding-panel" aria-labelledby="agent-bindings-heading">
        <div class="settings-panel-heading">
          <div><p class="eyebrow">Bindings</p><h2 id="agent-bindings-heading">选择 Binding</h2></div>
        </div>
        <p v-if="bindingsLoading" class="agent-binding-panel__state" data-agent-bindings-loading>加载中…</p>
        <p v-else-if="bindingsFailed" class="agent-binding-panel__state" role="alert" data-agent-bindings-failed>无法加载 Binding 列表。</p>
        <p v-else-if="bindings.length === 0" class="agent-binding-panel__state" data-agent-bindings-empty>无可用 Binding。</p>
        <div v-else class="agent-binding-list" role="group" aria-label="Binding 列表">
          <button
            v-for="binding in bindings"
            :key="binding.binding_id"
            type="button"
            class="agent-binding-option"
            :class="{ 'agent-binding-option--selected': selectedBindingId === binding.binding_id }"
            :aria-pressed="selectedBindingId === binding.binding_id ? 'true' : 'false'"
            :data-agent-binding-id="binding.binding_id"
            @click="selectBinding(binding.binding_id)"
          >
            <span class="agent-binding-option__term">Term {{ binding.term_id }}</span>
            <span class="agent-binding-option__profile">Profile {{ binding.profile_id }}</span>
            <span class="agent-binding-option__status" :data-agent-binding-status="binding.status">{{ binding.status }}</span>
          </button>
        </div>
      </section>

      <section class="settings-panel agent-conversation-panel" aria-labelledby="agent-conversations-heading">
        <div class="settings-panel-heading">
          <div><p class="eyebrow">Conversations</p><h2 id="agent-conversations-heading">会话列表</h2></div>
        </div>
        <div v-if="selectedBindingId === undefined" class="agent-conversation-panel__state" data-agent-conversations-need-binding>
          请先选择 Binding。
        </div>
        <template v-else>
          <form class="agent-create-row" @submit.prevent="createConversation">
            <label class="sr-only" for="agent-new-title">新会话标题（可选）</label>
            <input
              id="agent-new-title"
              v-model="newTitle"
              class="agent-create-row__input"
              type="text"
              placeholder="会话标题（可选）"
              data-agent-create-title
            />
            <button type="submit" class="primary-button" :disabled="creating" data-action="create-conversation">
              {{ creating ? '创建中…' : '创建会话' }}
            </button>
          </form>
          <p v-if="conversations.loading.value" class="agent-conversation-panel__state" data-agent-conversations-loading>加载中…</p>
          <p v-else-if="conversations.conversations.value.length === 0" class="agent-conversation-panel__state" data-agent-conversations-empty>暂无会话。</p>
          <ul v-else class="agent-conversation-list">
            <li v-for="conversation in conversations.conversations.value" :key="conversation.conversation_id" class="agent-conversation-row" :data-agent-conversation-id="conversation.conversation_id">
              <RouterLink
                class="agent-conversation-row__open"
                :to="`/agent/${conversation.conversation_id}`"
                :data-agent-conversation-title="conversation.title"
              >
                <span class="agent-conversation-row__title">{{ conversationTitle(conversation) }}</span>
                <span class="agent-conversation-row__status" :data-agent-conversation-status="conversation.status">{{ conversation.status }}</span>
                <span
                  v-if="conversations.pendingCount(conversation.conversation_id) > 0"
                  class="agent-pending-badge"
                  :aria-label="`${conversations.pendingCount(conversation.conversation_id)} 个待处理审批`"
                  data-agent-pending-badge
                >{{ conversations.pendingCount(conversation.conversation_id) }}</span>
              </RouterLink>
              <button
                type="button"
                class="secondary-button agent-conversation-row__delete"
                data-action="delete-conversation"
                @click="requestDelete(conversation.conversation_id)"
              >删除</button>
            </li>
          </ul>
        </template>
      </section>

      <p class="agent-grants-note" data-agent-grants>Delegated Write Grants：{{ delegatedWriteGrantsEnabled ? '可用' : '不可用' }}</p>
    </template>

    <div v-if="deleteTargetId !== null" class="dialog-backdrop" @click.self="closeDeleteDialog">
      <section
        ref="dialogPanel"
        class="dialog-panel agent-delete-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="agent-delete-confirm-title"
        aria-describedby="agent-delete-confirm-description"
        data-agent-delete-dialog
        @keydown="trapFocus"
      >
        <h2 id="agent-delete-confirm-title">删除该会话？</h2>
        <p id="agent-delete-confirm-description">
          将删除会话 <strong>{{ deleteTargetTitle }}</strong> 及其全部消息历史，此操作不可撤销。
        </p>
        <div class="dialog-actions">
          <button ref="cancelButton" class="text-button" type="button" data-action="delete-cancel" @click="closeDeleteDialog">取消</button>
          <button class="danger-button" type="button" data-action="delete-confirm" @click="confirmDelete">确认删除</button>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
//: Agent overview (M6b spec §4.7): capability-gated binding selector
//: (`GET /agent/admin/bindings`, term/profile/status) feeding the
//: conversation list scoped by the selected binding (list/create/delete +
//: pending approval badges). Deletion goes through a confirm dialog that
//: reuses the ClosePaneDialog focus-trap pattern (Escape cancels, focus
//: restored); every rendered text is pure interpolation. A disabled broker
//: renders the 占位 instead of the management UI (spec §6.4).
import type { AgentBindingResponse, AgentConversationResponse } from '@termflow/client-contracts'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useAgentBroker } from '../composables/useAgentBroker'
import { useAgentConversations } from '../composables/useAgentConversations'
import { useBottomToast } from '../composables/useBottomToast'
import { useClientRuntime } from '../runtime'

const runtime = useClientRuntime()
const toast = useBottomToast()
const { agentBrokerEnabled, delegatedWriteGrantsEnabled } = useAgentBroker()

const bindings = ref<AgentBindingResponse[]>([])
const bindingsLoading = ref(true)
const bindingsFailed = ref(false)
const selectedBindingId = ref<string | undefined>(undefined)
const conversations = useAgentConversations(selectedBindingId)

const newTitle = ref('')
const creating = ref(false)
let controller: AbortController | null = null

async function loadBindings() {
  bindingsLoading.value = true
  bindingsFailed.value = false
  try {
    bindings.value = (await runtime.api.agents.listBindings(controller?.signal)).bindings
  } catch {
    bindingsFailed.value = true
    toast.show({ text: '无法加载 Binding 列表。', tone: 'error' })
  } finally {
    bindingsLoading.value = false
  }
}

function selectBinding(bindingId: string) {
  selectedBindingId.value = bindingId
}

function conversationTitle(conversation: AgentConversationResponse): string {
  return conversation.title ?? `会话 ${conversation.conversation_id.slice(0, 8)}`
}

async function createConversation() {
  const binding = selectedBindingId.value
  if (binding === undefined || creating.value) return
  creating.value = true
  try {
    const title = newTitle.value.trim()
    const created = await conversations.create(binding, title === '' ? undefined : title)
    // Keep the typed title on failure so it can be retried.
    if (created !== null) newTitle.value = ''
  } finally {
    creating.value = false
  }
}

// Delete confirm dialog (ClosePaneDialog focus-trap pattern).
const deleteTargetId = ref<string | null>(null)
const dialogPanel = ref<HTMLElement | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null

const deleteTargetTitle = computed(() => {
  const target = conversations.conversations.value.find((conversation) => conversation.conversation_id === deleteTargetId.value)
  return target === undefined ? (deleteTargetId.value ?? '').slice(0, 8) : conversationTitle(target)
})

function requestDelete(conversationId: string) {
  restoreFocus = document.activeElement as HTMLElement | null
  deleteTargetId.value = conversationId
  void nextTick(() => cancelButton.value?.focus())
}

function closeDeleteDialog() {
  deleteTargetId.value = null
  void nextTick(() => {
    if (restoreFocus?.isConnected) restoreFocus.focus()
    restoreFocus = null
  })
}

async function confirmDelete() {
  const conversationId = deleteTargetId.value
  closeDeleteDialog()
  if (conversationId !== null) await conversations.remove(conversationId)
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

onMounted(() => {
  controller = new AbortController()
})
// Bindings are only fetched once the capability gate resolves enabled —
// a disabled broker renders the placeholder and never probes the admin API.
watch(agentBrokerEnabled, (enabled) => {
  if (enabled) void loadBindings()
})
onBeforeUnmount(() => {
  controller?.abort()
  controller = null
})
</script>
