<template>
  <aside
    ref="panel"
    v-show="open"
    id="terminal-agent-panel"
    class="terminal-agent-panel terminal-agent-panel--floating"
    :class="{ 'terminal-agent-panel--mobile-page': mobilePage }"
    data-agent-floating-panel
    :data-agent-layout="mobilePage ? 'page' : 'floating'"
    aria-label="Term Agent"
    :aria-hidden="open ? undefined : 'true'"
    :inert="open ? undefined : true"
    :style="mobilePage ? undefined : panelStyle"
  >
    <header class="terminal-agent-panel__header">
      <div class="terminal-agent-panel__toolbar" :aria-label="t('agent.panel.toolsAria')">
        <button
          v-if="!mobilePage"
          type="button"
          class="icon-button icon-only"
          data-action="drag-agent"
          :aria-label="t('agent.panel.dragAria')"
          :title="t('agent.panel.dragAria')"
          @pointerdown.stop.prevent="beginDrag"
        >
          <Move :size="17" aria-hidden="true" />
        </button>
        <button
          type="button"
          class="icon-button icon-only"
          data-action="new-agent-conversation"
          :aria-label="t('agent.panel.newConversation')"
          :title="t('agent.panel.newConversation')"
          :disabled="!isReady || mutating"
          @click="createConversation"
        >
          <MessageSquarePlus :size="17" aria-hidden="true" />
        </button>
        <button
          type="button"
          class="icon-button icon-only"
          data-action="toggle-agent-history"
          :aria-label="t('agent.panel.history')"
          :title="t('agent.panel.history')"
          :aria-expanded="historyOpen ? 'true' : 'false'"
          :disabled="!isReady"
          @click="historyOpen = !historyOpen"
        >
          <History :size="17" aria-hidden="true" />
        </button>
        <button
          v-if="bindingId"
          type="button"
          class="icon-button icon-only"
          data-action="toggle-agent-settings"
          :aria-label="t('agent.panel.settingsAria')"
          :title="t('agent.panel.settingsTitle')"
          :aria-expanded="settingsOpen ? 'true' : 'false'"
          :disabled="!isReady || mutating"
          @click="toggleSettings"
        >
          <Settings :size="17" aria-hidden="true" />
        </button>
      </div>
      <input
        v-if="renamingId !== null && renamingId === selectedConversationId"
        ref="renameInput"
        v-model="renameDraft"
        class="terminal-agent-panel__rename"
        data-agent-rename-input
        :aria-label="t('agent.conversations.renameTitle')"
        @keydown.enter.prevent="commitRename"
        @keydown.esc="cancelRename"
        @blur="commitRename"
      />
      <button
        v-else
        type="button"
        class="terminal-agent-panel__title"
        data-agent-panel-title
        :title="t('agent.conversations.renameTitle')"
        @click="beginRename(selectedConversationId)"
      >{{ panelTitle }}</button>
      <div class="terminal-agent-panel__actions">
        <button
          ref="closeButton"
          type="button"
          class="icon-button icon-only"
          data-action="close-agent"
          :aria-label="t('agent.panel.closeAria')"
          :title="t('agent.panel.closeAria')"
          @click="emit('close')"
        >
          <X :size="18" aria-hidden="true" />
        </button>
      </div>
    </header>

    <div v-if="setup !== null" class="terminal-agent-panel__statusbar" data-agent-panel-statusbar>
      <span
        class="agent-panel-readiness"
        data-agent-panel-readiness
        :data-state="setup.state"
        :data-backend-state="backendState ?? undefined"
      >{{ mergedStatusLabel }}</span>
      <div v-if="isReady && bindingId" class="terminal-agent-policy" data-agent-write-policy>
        <span class="terminal-agent-policy__label">{{ t('agent.panel.writePolicy') }}</span>
        <div class="terminal-agent-policy__options" role="group" :aria-label="t('agent.panel.writePolicyGroupAria')">
          <button type="button" :class="{ 'is-active': writePolicy === 'manual' }" :aria-pressed="writePolicy === 'manual'" :disabled="mutating" data-action="policy-manual" @click="changeWritePolicy('manual')">{{ t('agent.panel.policyManual') }}</button>
          <button type="button" :class="{ 'is-active': writePolicy === 'auto' }" :aria-pressed="writePolicy === 'auto'" :disabled="mutating" data-action="policy-auto" @click="pendingAutoPolicy = true">{{ t('agent.panel.policyAuto') }}</button>
        </div>
      </div>
    </div>

    <div class="terminal-agent-panel__content">
      <section v-if="settingsOpen && isReady" class="terminal-agent-panel__scroll" data-agent-panel-settings>
        <h3 class="agent-setup-form__title">{{ t('agent.panel.settingsAria') }}</h3>
        <AgentPanePicker v-model="settingsPaneIds" :panes="panes" :disabled="mutating" />
        <p v-if="settingsError" class="form-error" role="alert">{{ settingsError }}</p>
        <div class="agent-setup-form__actions">
          <button type="button" class="text-button" :disabled="mutating" data-action="cancel-agent-settings" @click="settingsOpen = false">{{ t('common.cancel') }}</button>
          <button type="button" class="primary-button" :disabled="mutating || !settingsPaneIds.length" data-action="save-agent-settings" @click="savePaneSettings">{{ t('common.save') }}</button>
        </div>
      </section>
      <section v-else-if="loading" class="terminal-agent-panel__scroll" data-agent-panel-state="loading" role="status">{{ t('agent.panel.loading') }}</section>
      <section v-else-if="setup?.state === 'unconfigured'" class="terminal-agent-panel__scroll" data-agent-panel-state="unconfigured">
        <AgentSetupForm :setup="setup" :profiles="profiles" :panes="panes" :busy="mutating" :error="error" @submit="submitSetup" @refresh="refresh" />
      </section>
      <section v-else-if="setup?.state === 'deployment_required'" class="terminal-agent-panel__scroll" data-agent-panel-state="deployment_required">
        <div class="agent-panel-empty">
          <h3>{{ t('agent.panel.deploymentRequiredTitle') }}</h3>
          <p>{{ t('agent.panel.deploymentRequiredDesc') }}</p>
          <div class="agent-setup-form__actions">
            <button type="button" class="text-button" :disabled="mutating" @click="refresh">{{ t('agent.panel.checkDeployment') }}</button>
          </div>
        </div>
      </section>
      <section v-else-if="setup?.state === 'activating'" class="terminal-agent-panel__scroll" data-agent-panel-state="activating" role="status">
        <div class="agent-panel-empty">
          <h3>{{ t('agent.panel.activatingTitle') }}</h3>
          <p class="muted">{{ t('agent.panel.activatingDesc') }}</p>
          <div class="agent-setup-form__actions">
            <button type="button" class="text-button" :disabled="mutating" @click="refresh">{{ t('agent.panel.refreshStatus') }}</button>
          </div>
        </div>
      </section>
      <section v-else-if="disclosureRecoveryRequired" class="terminal-agent-panel__scroll" data-agent-panel-state="unavailable" data-agent-disclosure-recovery>
        <h3>{{ t('agent.panel.reEnableTitle') }}</h3>
        <p class="muted">{{ t('agent.panel.reEnableDesc') }}</p>
        <button type="button" class="primary-button" data-action="accept-current-disclosure" :disabled="mutating" @click="acceptCurrentDisclosure">{{ t('agent.panel.reEnable') }}</button>
        <p v-if="error" role="alert">{{ error }}</p>
        <button type="button" :disabled="mutating" @click="refresh">{{ t('common.refresh') }}</button>
      </section>
      <section v-else-if="setup?.state !== 'ready'" class="terminal-agent-panel__scroll" data-agent-panel-state="unavailable">
        <div class="agent-panel-empty">
          <h3>{{ error ? t('agent.panel.unavailableTitle') : t('agent.panel.recoveringTitle') }}</h3>
          <p role="alert">{{ error || t('agent.panel.recoveringDesc') }}</p>
          <div class="agent-setup-form__actions">
            <button type="button" class="text-button" :disabled="mutating" @click="retry">{{ t('common.retry') }}</button>
            <button v-if="bindingId" type="button" class="primary-button" :disabled="mutating" @click="activate">{{ t('agent.panel.reactivate') }}</button>
          </div>
        </div>
      </section>
      <div v-else data-agent-panel-state="ready" class="terminal-agent-panel__ready">
        <div v-if="pendingAutoPolicy" class="terminal-agent-policy__confirm" role="alertdialog" :aria-label="t('agent.panel.confirmAutoAria')" data-agent-policy-confirm>
          <p>{{ t('agent.panel.confirmAutoDesc') }}</p>
          <div class="terminal-agent-policy__confirm-actions">
            <button type="button" class="primary-button" :disabled="mutating" data-action="confirm-policy-auto" @click="changeWritePolicy('auto')">{{ t('agent.panel.confirmAutoBtn') }}</button>
            <button type="button" class="text-button" :disabled="mutating" @click="pendingAutoPolicy = false">{{ t('common.cancel') }}</button>
          </div>
        </div>
        <div v-if="historyOpen" class="terminal-agent-history-backdrop" data-agent-history-backdrop @click="historyOpen = false" />
        <div v-if="historyOpen" class="terminal-agent-history" data-agent-history>
          <div class="terminal-agent-history__heading">
            <strong>{{ t('agent.panel.history') }}</strong>
            <span>{{ t('agent.panel.historyCount', { count: conversations.length }) }}</span>
          </div>
          <p v-if="conversations.length === 0" class="muted">{{ t('agent.panel.historyEmpty') }}</p>
          <div v-for="entry in conversations" :key="entry.conversation_id" class="terminal-agent-history__row">
            <button
              type="button"
              class="terminal-agent-history__item"
              :class="{ 'is-selected': entry.conversation_id === selectedConversationId }"
              :aria-current="entry.conversation_id === selectedConversationId ? 'true' : undefined"
              :data-agent-history-conversation="entry.conversation_id"
              @click="selectConversation(entry.conversation_id); historyOpen = false"
            >
              <input
                v-if="renamingId === entry.conversation_id"
                ref="renameInput"
                v-model="renameDraft"
                class="terminal-agent-history__rename-input"
                data-agent-history-rename-input
                :aria-label="t('agent.conversations.renameTitle')"
                @keydown.enter.prevent="commitRename"
                @keydown.esc="cancelRename"
                @blur="commitRename"
                @click.stop
              />
              <span v-else class="terminal-agent-history__item-title">{{ entry.title || t('agent.panel.untitledConversation') }}</span>
              <span class="terminal-agent-history__item-status">{{ entry.status === 'open' ? t('agent.panel.statusOpen') : t('agent.status.stopped') }}</span>
            </button>
            <button
              type="button"
              class="icon-button icon-only terminal-agent-history__rename"
              :aria-label="t('agent.conversations.renameAria', { title: entry.title || t('agent.panel.untitledConversation') })"
              :title="t('agent.conversations.renameTitle')"
              data-action="rename-history-conversation"
              @click="beginRename(entry.conversation_id)"
            >
              <Pencil :size="14" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="icon-button icon-only terminal-agent-history__delete"
              :aria-label="t('agent.conversations.deleteAria', { title: entry.title || t('agent.panel.untitledConversation') })"
              :title="t('agent.conversations.deleteTitle')"
              data-action="delete-history-conversation"
              @click="deleteHistoryConversation(entry.conversation_id)"
            >
              <Trash2 :size="14" aria-hidden="true" />
            </button>
          </div>
        </div>
        <p v-if="error" class="agent-panel-error" role="alert">{{ error }}</p>
        <AgentChatSession v-if="selectedConversationId" :key="selectedConversationId" :conversation-id="selectedConversationId" :enabled="true" variant="floating" @pending-count="forwardPending" @backend-state="backendState = $event" @missing="refreshAfterMissingConversation" />
        <p v-else class="terminal-agent-empty">{{ t('agent.panel.emptyPrompt') }}</p>
      </div>
    </div>

    <template v-if="!mobilePage">
      <button
        v-for="edge in resizeEdges"
        :key="edge"
        type="button"
        tabindex="-1"
        class="terminal-agent-resize-handle"
        :class="`terminal-agent-resize-handle--${edge}`"
        :data-agent-resize-handle="edge"
        :aria-label="t('agent.panel.resizeAria', { edge: t(`agent.panel.edge.${edge}`) })"
        @pointerdown.stop.prevent="(event) => beginResize(edge, event)"
      />
    </template>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { History, MessageSquarePlus, Move, Pencil, Settings, Trash2, X } from '@lucide/vue'
import type { AgentSetupResponse } from '@termflow/client-contracts'
import { ApiError } from '@termflow/client-core'
import { useFloatingPanel, type FloatingPanelResizeEdge } from '../../composables/useFloatingPanel'
import { agentReasonMessage, useTermAgent } from '../../composables/useTermAgent'
import { useI18n } from '../../i18n'
import { useClientRuntime } from '../../runtime'
import AgentChatSession from './AgentChatSession.vue'
import AgentPanePicker from './AgentPanePicker.vue'
import AgentSetupForm from './AgentSetupForm.vue'

const { t } = useI18n()

const props = withDefaults(defineProps<{ termId: string; conversationId: string | null; open?: boolean; mobilePage?: boolean }>(), { open: true, mobilePage: false })
const emit = defineEmits<{ close: []; selectConversation: [conversationId: string | null]; pendingCount: [count: number]; readiness: [state: AgentSetupResponse['state']] }>()
const runtime = useClientRuntime()
const { setup, profiles, panes, bindingId, conversations, selectedConversationId, pendingApprovalCount, loading, mutating, error, refresh, submitSetup, activate, acceptDisclosure, selectConversation, createConversation, retry } = useTermAgent({ termId: () => props.termId, requestedConversationId: () => props.conversationId })
const panel = ref<HTMLElement | null>(null)
const container = ref<HTMLElement | null>(null)
const { style: panelStyle, sync, beginDrag, beginResize } = useFloatingPanel({
  container,
  panel,
  margin: 16,
  minWidth: 320,
  minHeight: 360,
  defaultWidth: 480,
  defaultHeight: 620,
})
const closeButton = ref<HTMLButtonElement | null>(null)
const historyOpen = ref(false)
const backendState = ref<string | null>(null)
const pendingAutoPolicy = ref(false)
const settingsOpen = ref(false)
const settingsPaneIds = ref<string[]>([])
const settingsError = ref('')
const writePolicy = computed<'manual' | 'auto'>(() => setup.value?.write_policy === 'auto' ? 'auto' : 'manual')

function toggleSettings() {
  if (mutating.value || !isReady.value) return
  settingsOpen.value = !settingsOpen.value
  if (settingsOpen.value) {
    settingsError.value = ''
    settingsPaneIds.value = [...(setup.value?.pane_policy?.pane_ids ?? [])]
    // Pick up panes that appeared after setup so the listed grants and the
    // topology revision used on save are current.
    void refresh()
  }
}

async function savePaneSettings() {
  const binding = bindingId.value
  const revision = setup.value?.topology_revision
  if (binding === null || revision === null || revision === undefined || mutating.value) return
  settingsError.value = ''
  mutating.value = true
  try {
    await runtime.api.agents.replacePanePolicies(binding, { pane_ids: [...settingsPaneIds.value], topology_revision: revision, expected_revision: setup.value?.runtime?.config_revision ?? null })
    settingsOpen.value = false
    await refresh()
  } catch (cause) {
    settingsError.value = cause instanceof ApiError ? agentReasonMessage(cause.code) : t('agent.panel.savePanesError')
  } finally {
    mutating.value = false
  }
}

async function deleteHistoryConversation(conversationId: string) {
  if (mutating.value) return
  mutating.value = true
  try {
    await runtime.api.agents.deleteConversation(conversationId)
    conversations.value = conversations.value.filter((entry) => entry.conversation_id !== conversationId)
    if (selectedConversationId.value === conversationId) selectConversation(conversations.value[0]?.conversation_id ?? null)
  } catch {
    // Surfaced on the next refresh; keep the list unchanged meanwhile.
  } finally {
    mutating.value = false
  }
}

function refreshAfterMissingConversation() { void refresh() }

async function changeWritePolicy(policy: 'manual' | 'auto') {
  const binding = bindingId.value
  if (binding === null || mutating.value) return
  mutating.value = true
  try {
    await runtime.api.agents.setWritePolicy(binding, policy)
    pendingAutoPolicy.value = false
    await refresh()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('agent.panel.writePolicyError')
  } finally {
    mutating.value = false
  }
}
const isReady = computed(() => setup.value?.state === 'ready')
const selectedEntry = computed(() => conversations.value.find((entry) => entry.conversation_id === selectedConversationId.value) ?? null)
const renamingId = ref<string | null>(null)
const renameDraft = ref('')
const renameInput = ref<HTMLInputElement | null>(null)
function beginRename(conversationId: string | null) {
  if (conversationId === null || mutating.value) return
  const entry = conversations.value.find((item) => item.conversation_id === conversationId)
  renameDraft.value = entry?.title?.trim() ?? ''
  renamingId.value = conversationId
  void nextTick(() => renameInput.value?.select())
}
async function commitRename() {
  const id = renamingId.value
  if (id === null) return
  renamingId.value = null
  const title = renameDraft.value.trim()
  const entry = conversations.value.find((item) => item.conversation_id === id)
  if (title === '' || title === (entry?.title?.trim() ?? '')) return
  try {
    await runtime.api.agents.updateConversation(id, { title })
    await refresh()
  } catch {
    // Keep the previous title; the next refresh reconciles.
  }
}
function cancelRename() { renamingId.value = null }
const panelTitle = computed(() => selectedEntry.value?.title?.trim() || (selectedConversationId.value ? t('agent.panel.defaultChatTitle') : 'Term Agent'))
const disclosureRecoveryRequired = computed(() => setup.value?.state === 'unavailable' && (
  setup.value.reason_code === 'binding_disclosure_stale'
  || setup.value.reason_code === 'binding_disclosure_required'
))
const backendLabelKeys: Record<string, string> = {
  connecting: 'agent.backend.connecting',
  idle: 'agent.backend.idle',
  busy: 'agent.backend.busy',
  retry: 'agent.backend.retry',
  ready: 'agent.backend.readyPanel',
  unavailable: 'agent.backend.offline',
  context_lost: 'agent.backend.context_lost',
  reconciling: 'agent.backend.reconciling',
  closed: 'agent.backend.closed',
}
const readinessLabelKeys: Record<AgentSetupResponse['state'], string> = {
  unconfigured: 'agent.readiness.unconfigured',
  deployment_required: 'agent.readiness.deployment_required',
  activating: 'agent.readiness.activating',
  ready: 'agent.readiness.ready',
  unavailable: 'agent.readiness.unavailable',
}
const mergedStatusLabel = computed(() => {
  const state = setup.value?.state
  if (state !== 'ready') return state === undefined ? '' : t(readinessLabelKeys[state])
  if (backendState.value === null || backendState.value === 'ready') return t('agent.backend.readyPanel')
  const key = backendLabelKeys[backendState.value]
  return key ? t(key) : backendState.value
})
const resizeEdges: FloatingPanelResizeEdge[] = ['n', 'e', 's', 'w', 'ne', 'se', 'sw', 'nw']

watch(() => setup.value?.state, (state) => {
  if (state && state !== 'ready') settingsOpen.value = false
  if (state) emit('readiness', state)
}, { immediate: true })
watch(selectedConversationId, (id) => { forwardPending(0); if (props.open) emit('selectConversation', id) })
watch(() => props.open, (open, previous) => {
  if (!open) return
  if (previous === false) emit('selectConversation', selectedConversationId.value)
  void nextTick(() => { sync(); closeButton.value?.focus() })
}, { immediate: true })
watch(() => props.mobilePage, (mobilePage) => {
  if (!mobilePage && props.open) void nextTick(sync)
})
function forwardPending(count: number) { pendingApprovalCount.value = count; emit('pendingCount', count) }
async function acceptCurrentDisclosure() { await acceptDisclosure() }
</script>
