<template>
  <div class="page agent-view">
    <header class="page-heading">
      <div>
        <p class="eyebrow">{{ t('agent.eyebrow') }}</p>
        <h1 id="agent-title">{{ t('agent.title') }}</h1>
      </div>
    </header>

    <section v-if="!agentBrokerEnabled" class="agent-placeholder" data-agent-disabled>
      <h2>{{ t('agent.disabledTitle') }}</h2>
      <p>{{ t('agent.disabledDesc') }}</p>
    </section>

    <template v-else>
      <p v-if="pageError !== null" class="form-error agent-page-error" role="alert" data-agent-error>
        {{ pageError }}
      </p>

      <section class="settings-panel agent-conversation-panel" aria-labelledby="agent-conversations-heading">
        <div class="settings-panel-heading">
          <div>
            <p class="eyebrow">{{ t('agent.conversations.eyebrow') }}</p>
            <h2 id="agent-conversations-heading">{{ t('agent.conversations.heading') }}</h2>
          </div>
          <span class="status-chip" data-agent-conversation-count>{{ conversationRows.length }}</span>
        </div>

        <p v-if="directoryLoading || conversations.loading.value" class="agent-conversation-panel__state" data-agent-conversations-loading>{{ t('common.loading') }}</p>
        <p v-else-if="bindings.length === 0" class="agent-conversation-panel__state" data-agent-bindings-empty>
          {{ t('agent.conversations.emptyBindings') }}
        </p>
        <p v-else-if="conversationRows.length === 0" class="agent-conversation-panel__state" data-agent-conversations-empty>
          {{ t('agent.conversations.empty') }}
        </p>
        <div v-else class="agent-conversation-table" role="table" :aria-label="t('agent.conversations.tableAria')" data-agent-conversation-table>
          <div class="agent-conversation-table__head" role="row">
            <span role="columnheader">{{ t('agent.conversations.col.name') }}</span>
            <span role="columnheader">{{ t('agent.conversations.col.term') }}</span>
            <span role="columnheader">{{ t('agent.conversations.col.status') }}</span>
            <span role="columnheader">{{ t('agent.conversations.col.actions') }}</span>
          </div>
          <article
            v-for="conversation in conversationRows"
            :key="conversation.conversation_id"
            class="agent-conversation-table__row"
            role="row"
            :data-agent-conversation-id="conversation.conversation_id"
          >
            <div role="cell" :data-label="t('agent.conversations.col.name')" class="agent-conversation-table__name">
              <form v-if="editingConversationId === conversation.conversation_id" class="agent-rename-form" @submit.prevent="saveRename(conversation.conversation_id)">
                <label class="sr-only" :for="`agent-rename-${conversation.conversation_id}`">{{ t('agent.conversations.col.name') }}</label>
                <input
                  :id="`agent-rename-${conversation.conversation_id}`"
                  v-model="renameDraft"
                  maxlength="255"
                  data-agent-rename-input
                  @keydown.esc.prevent="cancelRename"
                />
                <button class="icon-button icon-only" type="submit" data-action="save-conversation-name" :aria-label="t('agent.conversations.saveNameAria')" :title="t('agent.conversations.saveNameAria')">
                  <Check :size="17" aria-hidden="true" />
                </button>
                <button class="icon-button icon-only" type="button" data-action="cancel-conversation-name" :aria-label="t('agent.conversations.cancelNameAria')" :title="t('agent.conversations.cancelNameAria')" @click="cancelRename">
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
            <div role="cell" :data-label="t('agent.conversations.col.term')" class="agent-conversation-table__term">
              {{ termName(bindingTermId(conversation.binding_id)) }}
            </div>
            <div role="cell" :data-label="t('agent.conversations.col.status')" class="agent-conversation-table__status">
              <span class="agent-status-tag" :data-status="conversationStatus(conversation.status).tone">
                {{ conversationStatus(conversation.status).label }}
              </span>
              <span
                v-if="conversations.pendingCount(conversation.conversation_id) > 0"
                class="agent-pending-badge"
                :aria-label="t('agent.conversations.pendingBadgeAria', { count: conversations.pendingCount(conversation.conversation_id) })"
                data-agent-pending-badge
              >{{ conversations.pendingCount(conversation.conversation_id) }}</span>
            </div>
            <div role="cell" :data-label="t('agent.conversations.col.actions')" class="agent-conversation-table__actions">
              <RouterLink
                class="icon-button icon-only"
                :to="`/terms/${bindingTermId(conversation.binding_id)}?agent=${conversation.conversation_id}`"
                data-action="open-conversation"
                :aria-label="t('agent.conversations.openInTermAria', { title: conversationTitle(conversation) })"
                :title="t('agent.conversations.openInTermTitle')"
              >
                <Terminal :size="17" aria-hidden="true" />
              </RouterLink>
              <button
                class="icon-button icon-only"
                type="button"
                data-action="rename-conversation"
                :aria-label="t('agent.conversations.renameAria', { title: conversationTitle(conversation) })"
                :title="t('agent.conversations.renameTitle')"
                @click="startRename(conversation)"
              >
                <Pencil :size="17" aria-hidden="true" />
              </button>
              <button
                class="icon-button icon-only destructive"
                type="button"
                data-action="delete-conversation"
                :aria-label="t('agent.conversations.deleteAria', { title: conversationTitle(conversation) })"
                :title="t('agent.conversations.deleteTitle')"
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
        <h2 id="agent-delete-confirm-title">{{ t('agent.conversations.deleteDialogTitle') }}</h2>
        <p id="agent-delete-confirm-description">
          {{ t('agent.conversations.deleteDialogDesc', { title: deleteTargetTitle }) }}
        </p>
        <div class="dialog-actions">
          <button ref="cancelButton" class="text-button" type="button" data-action="delete-cancel" @click="closeDeleteDialog">{{ t('common.cancel') }}</button>
          <button class="danger-button" type="button" data-action="delete-confirm" @click="confirmDelete">{{ t('agent.conversations.deleteConfirm') }}</button>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
//: Product-facing Agent directory. Binding ids remain an internal API scope;
//: the table resolves them to Term names and exposes only conversation data.
//: All action glyphs come from the open-source Lucide Vue package.
import { Check, Pencil, Terminal, Trash2, X } from '@lucide/vue'
import type { AgentBindingResponse, AgentConversationResponse } from '@termflow/client-contracts'
import { ApiError } from '@termflow/client-core'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useAgentBroker } from '../composables/useAgentBroker'
import { useAgentConversations } from '../composables/useAgentConversations'
import { useBottomToast } from '../composables/useBottomToast'
import { useI18n } from '../i18n'
import { useClientRuntime } from '../runtime'

const { t } = useI18n()
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
    directoryError.value = t('agent.conversations.loadError')
    toast.show({ text: directoryError.value, tone: 'error' })
  }
  if (computerResult.status === 'fulfilled') {
    const names = new Map<string, string>()
    for (const computer of computerResult.value.computers) {
      for (const term of computer.terms) names.set(term.instance_id, term.name)
    }
    termNames.value = names
  } else if (!isAborted(computerResult.reason) && directoryError.value === null) {
    directoryError.value = t('agent.conversations.termNameError')
  }
  directoryLoading.value = false
}

function bindingTermId(bindingId: string): string {
  return bindings.value.find((binding) => binding.binding_id === bindingId)?.term_id ?? ''
}

function termName(termId: string): string {
  return termNames.value.get(termId) ?? (termId === '' ? t('agent.conversations.termUnknown') : `Term · ${termId.slice(0, 8)}`)
}

function conversationTitle(conversation: AgentConversationResponse): string {
  return conversation.title?.trim() || t('agent.conversations.defaultTitle', { id: conversation.conversation_id.slice(0, 8) })
}

function conversationStatus(status: string): { label: string, tone: 'running' | 'stopped' | 'attention' } {
  const normalized = status.toLowerCase()
  if (['active', 'open', 'running', 'processing', 'queued'].includes(normalized)) {
    return { label: t('agent.status.running'), tone: 'running' }
  }
  if (['failed', 'error', 'degraded'].includes(normalized)) {
    return { label: t('agent.status.attention'), tone: 'attention' }
  }
  return { label: t('agent.status.stopped'), tone: 'stopped' }
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
