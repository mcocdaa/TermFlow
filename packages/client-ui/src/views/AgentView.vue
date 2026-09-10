<template>
  <div class="page agent-view">
    <header class="page-heading">
      <div>
        <p class="eyebrow">Agent</p>
        <h1 id="agent-title">Agent 控制台</h1>
      </div>
    </header>

    <section v-if="!agentBrokerEnabled" class="agent-placeholder" data-agent-disabled>
      <h2>Agent Broker 未启用</h2>
      <p>服务器未开启 Agent Broker 能力，无法使用 Agent 会话。</p>
    </section>

    <template v-else>
      <p v-if="pageError !== null" class="form-error agent-page-error" role="alert" data-agent-error>
        {{ pageError }}
      </p>

      <section class="settings-panel agent-conversation-panel" aria-labelledby="agent-conversations-heading">
        <div class="settings-panel-heading">
          <div>
            <p class="eyebrow">Conversations</p>
            <h2 id="agent-conversations-heading">会话记录</h2>
          </div>
          <span class="status-chip" data-agent-conversation-count>{{ conversationRows.length }}</span>
        </div>

        <p v-if="directoryLoading || conversations.loading.value" class="agent-conversation-panel__state" data-agent-conversations-loading>加载中…</p>
        <p v-else-if="bindings.length === 0" class="agent-conversation-panel__state" data-agent-bindings-empty>
          暂无可用 Term，请先在电脑管理中连接一个 Term。
        </p>
        <p v-else-if="conversationRows.length === 0" class="agent-conversation-panel__state" data-agent-conversations-empty>
          暂无会话记录。
        </p>
        <div v-else class="agent-conversation-table" role="table" aria-label="Agent 会话记录" data-agent-conversation-table>
          <div class="agent-conversation-table__head" role="row">
            <span role="columnheader">会话名称</span>
            <span role="columnheader">Term</span>
            <span role="columnheader">工作状态</span>
            <span role="columnheader">操作</span>
          </div>
          <article
            v-for="conversation in conversationRows"
            :key="conversation.conversation_id"
            class="agent-conversation-table__row"
            role="row"
            :data-agent-conversation-id="conversation.conversation_id"
          >
            <div role="cell" data-label="会话名称" class="agent-conversation-table__name">
              <form v-if="editingConversationId === conversation.conversation_id" class="agent-rename-form" @submit.prevent="saveRename(conversation.conversation_id)">
                <label class="sr-only" :for="`agent-rename-${conversation.conversation_id}`">会话名称</label>
                <input
                  :id="`agent-rename-${conversation.conversation_id}`"
                  v-model="renameDraft"
                  maxlength="255"
                  data-agent-rename-input
                  @keydown.esc.prevent="cancelRename"
                />
                <button class="icon-button icon-only" type="submit" data-action="save-conversation-name" aria-label="保存会话名称" title="保存会话名称">
                  <Check :size="17" aria-hidden="true" />
                </button>
                <button class="icon-button icon-only" type="button" data-action="cancel-conversation-name" aria-label="取消重命名" title="取消重命名" @click="cancelRename">
                  <X :size="17" aria-hidden="true" />
                </button>
              </form>
              <RouterLink
                v-else
                class="agent-conversation-table__title"
                :to="`/agent/${conversation.conversation_id}`"
                :data-agent-conversation-title="conversation.title ?? ''"
              >
                <span>{{ conversationTitle(conversation) }}</span>
              </RouterLink>
            </div>
            <div role="cell" data-label="Term" class="agent-conversation-table__term">
              {{ termName(bindingTermId(conversation.binding_id)) }}
            </div>
            <div role="cell" data-label="工作状态" class="agent-conversation-table__status">
              <span class="agent-status-tag" :data-status="conversationStatus(conversation.status).tone">
                {{ conversationStatus(conversation.status).label }}
              </span>
              <span
                v-if="conversations.pendingCount(conversation.conversation_id) > 0"
                class="agent-pending-badge"
                :aria-label="`${conversations.pendingCount(conversation.conversation_id)} 个待处理审批`"
                data-agent-pending-badge
              >{{ conversations.pendingCount(conversation.conversation_id) }}</span>
            </div>
            <div role="cell" data-label="操作" class="agent-conversation-table__actions">
              <RouterLink
                class="icon-button icon-only"
                :to="`/agent/${conversation.conversation_id}`"
                data-action="open-conversation"
                :aria-label="`打开会话：${conversationTitle(conversation)}`"
                title="打开会话"
              >
                <ExternalLink :size="17" aria-hidden="true" />
              </RouterLink>
              <button
                class="icon-button icon-only"
                type="button"
                data-action="rename-conversation"
                :aria-label="`重命名会话：${conversationTitle(conversation)}`"
                title="重命名会话"
                @click="startRename(conversation)"
              >
                <Pencil :size="17" aria-hidden="true" />
              </button>
              <button
                class="icon-button icon-only destructive"
                type="button"
                data-action="delete-conversation"
                :aria-label="`删除会话：${conversationTitle(conversation)}`"
                title="删除会话"
                @click="requestDelete(conversation.conversation_id)"
              >
                <Trash2 :size="17" aria-hidden="true" />
              </button>
            </div>
          </article>
        </div>
      </section>
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
//: Product-facing Agent directory. Binding ids remain an internal API scope;
//: the table resolves them to Term names and exposes only conversation data.
//: All action glyphs come from the open-source Lucide Vue package.
import { Check, ExternalLink, Pencil, Trash2, X } from '@lucide/vue'
import type { AgentBindingResponse, AgentConversationResponse } from '@termflow/client-contracts'
import { ApiError } from '@termflow/client-core'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useAgentBroker } from '../composables/useAgentBroker'
import { useAgentConversations } from '../composables/useAgentConversations'
import { useBottomToast } from '../composables/useBottomToast'
import { useClientRuntime } from '../runtime'

const runtime = useClientRuntime()
const toast = useBottomToast()
const { agentBrokerEnabled } = useAgentBroker()

const bindings = ref<AgentBindingResponse[]>([])
const termNames = ref(new Map<string, string>())
const directoryLoading = ref(true)
const directoryError = ref<string | null>(null)
const controller = ref<AbortController | null>(null)

const conversations = useAgentConversations(() => bindings.value.map((binding) => binding.binding_id))

const conversationRows = computed(() => [...conversations.conversations.value].sort((left, right) => {
  return right.updated_at.localeCompare(left.updated_at)
}))

const pageError = computed(() => directoryError.value ?? conversations.error.value)

function isAborted(error: unknown): boolean {
  return error instanceof ApiError && error.kind === 'aborted'
}

async function loadDirectory() {
  directoryLoading.value = true
  directoryError.value = null
  const signal = controller.value?.signal
  const [bindingResult, computerResult] = await Promise.allSettled([
    runtime.api.agents.listBindings(signal === undefined ? {} : { signal }),
    runtime.api.computers.list(signal),
  ])
  if (bindingResult.status === 'fulfilled') {
    bindings.value = bindingResult.value.bindings
  } else if (!isAborted(bindingResult.reason)) {
    directoryError.value = '无法加载 Agent 会话数据，请稍后重试。'
    toast.show({ text: directoryError.value, tone: 'error' })
  }
  if (computerResult.status === 'fulfilled') {
    const names = new Map<string, string>()
    for (const computer of computerResult.value.computers) {
      for (const term of computer.terms) names.set(term.instance_id, term.name)
    }
    termNames.value = names
  } else if (!isAborted(computerResult.reason) && directoryError.value === null) {
    directoryError.value = '无法加载 Term 名称，部分会话信息可能不完整。'
  }
  directoryLoading.value = false
}

function bindingTermId(bindingId: string): string {
  return bindings.value.find((binding) => binding.binding_id === bindingId)?.term_id ?? ''
}

function termName(termId: string): string {
  return termNames.value.get(termId) ?? (termId === '' ? 'Term 未知' : `Term · ${termId.slice(0, 8)}`)
}

function conversationTitle(conversation: AgentConversationResponse): string {
  return conversation.title?.trim() || `会话 ${conversation.conversation_id.slice(0, 8)}`
}

function conversationStatus(status: string): { label: string, tone: 'running' | 'stopped' | 'attention' } {
  const normalized = status.toLowerCase()
  if (['active', 'open', 'running', 'processing', 'queued'].includes(normalized)) {
    return { label: '运行中', tone: 'running' }
  }
  if (['failed', 'error', 'degraded'].includes(normalized)) {
    return { label: '需要注意', tone: 'attention' }
  }
  return { label: '已停止', tone: 'stopped' }
}

const editingConversationId = ref<string | null>(null)
const renameDraft = ref('')

function startRename(conversation: AgentConversationResponse) {
  editingConversationId.value = conversation.conversation_id
  renameDraft.value = conversationTitle(conversation)
  void nextTick(() => document.querySelector<HTMLInputElement>('[data-agent-rename-input]')?.focus())
}

function cancelRename() {
  editingConversationId.value = null
  renameDraft.value = ''
}

async function saveRename(conversationId: string) {
  const updated = await conversations.rename(conversationId, renameDraft.value)
  if (updated !== null) cancelRename()
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
  controller.value = new AbortController()
})
watch(agentBrokerEnabled, (enabled) => {
  if (enabled) void loadDirectory()
})
onBeforeUnmount(() => {
  controller.value?.abort()
  controller.value = null
})
</script>
