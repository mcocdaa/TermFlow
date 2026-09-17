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
      <div class="terminal-agent-panel__toolbar" aria-label="Agent 工具">
        <button
          v-if="!mobilePage"
          type="button"
          class="icon-button icon-only"
          data-action="drag-agent"
          aria-label="拖动 Agent 面板"
          title="拖动 Agent 面板"
          @pointerdown.stop.prevent="beginDrag"
        >
          <Move :size="17" aria-hidden="true" />
        </button>
        <button
          type="button"
          class="icon-button icon-only"
          data-action="new-agent-conversation"
          aria-label="新建会话"
          title="新建会话"
          :disabled="!isReady || mutating"
          @click="createConversation"
        >
          <MessageSquarePlus :size="17" aria-hidden="true" />
        </button>
        <button
          type="button"
          class="icon-button icon-only"
          data-action="toggle-agent-history"
          aria-label="历史记录"
          title="历史记录"
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
          aria-label="Agent 设置"
          title="Agent 设置：调整可访问的窗格"
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
        aria-label="重命名会话"
        @keydown.enter.prevent="commitRename"
        @keydown.esc="cancelRename"
        @blur="commitRename"
      />
      <button
        v-else
        type="button"
        class="terminal-agent-panel__title"
        data-agent-panel-title
        title="重命名会话"
        @click="beginRename(selectedConversationId)"
      >{{ panelTitle }}</button>
      <div class="terminal-agent-panel__actions">
        <button
          ref="closeButton"
          type="button"
          class="icon-button icon-only"
          data-action="close-agent"
          aria-label="关闭 Agent"
          title="关闭 Agent"
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
        <span class="terminal-agent-policy__label">写入审批</span>
        <div class="terminal-agent-policy__options" role="group" aria-label="新会话写入审批">
          <button type="button" :class="{ 'is-active': writePolicy === 'manual' }" :aria-pressed="writePolicy === 'manual'" :disabled="mutating" data-action="policy-manual" @click="changeWritePolicy('manual')">手动</button>
          <button type="button" :class="{ 'is-active': writePolicy === 'auto' }" :aria-pressed="writePolicy === 'auto'" :disabled="mutating" data-action="policy-auto" @click="pendingAutoPolicy = true">放行</button>
        </div>
      </div>
    </div>

    <div class="terminal-agent-panel__content">
      <section v-if="settingsOpen && isReady" class="terminal-agent-panel__scroll" data-agent-panel-settings>
        <h3 class="agent-setup-form__title">Agent 设置</h3>
        <AgentPanePicker v-model="settingsPaneIds" :panes="panes" :disabled="mutating" />
        <p v-if="settingsError" class="form-error" role="alert">{{ settingsError }}</p>
        <div class="agent-setup-form__actions">
          <button type="button" class="text-button" :disabled="mutating" data-action="cancel-agent-settings" @click="settingsOpen = false">取消</button>
          <button type="button" class="primary-button" :disabled="mutating || !settingsPaneIds.length" data-action="save-agent-settings" @click="savePaneSettings">保存</button>
        </div>
      </section>
      <section v-else-if="loading" class="terminal-agent-panel__scroll" data-agent-panel-state="loading" role="status">正在加载 Agent…</section>
      <section v-else-if="setup?.state === 'unconfigured'" class="terminal-agent-panel__scroll" data-agent-panel-state="unconfigured">
        <AgentSetupForm :setup="setup" :profiles="profiles" :panes="panes" :busy="mutating" :error="error" @submit="submitSetup" @refresh="refresh" />
      </section>
      <section v-else-if="setup?.state === 'deployment_required'" class="terminal-agent-panel__scroll" data-agent-panel-state="deployment_required">
        <div class="agent-panel-empty">
          <h3>需要部署 Agent 服务</h3>
          <p>请管理员完成运行环境和提供方配置。</p>
          <div class="agent-setup-form__actions">
            <button type="button" class="text-button" :disabled="mutating" @click="refresh">检查部署状态</button>
          </div>
        </div>
      </section>
      <section v-else-if="setup?.state === 'activating'" class="terminal-agent-panel__scroll" data-agent-panel-state="activating" role="status">
        <div class="agent-panel-empty">
          <h3>Agent 正在激活</h3>
          <p class="muted">运行时正在连接，稍候会自动刷新。</p>
          <div class="agent-setup-form__actions">
            <button type="button" class="text-button" :disabled="mutating" @click="refresh">刷新状态</button>
          </div>
        </div>
      </section>
      <section v-else-if="disclosureRecoveryRequired" class="terminal-agent-panel__scroll" data-agent-panel-state="unavailable" data-agent-disclosure-recovery>
        <h3>请重新启用 Agent</h3>
        <p class="muted">部署的提供方配置已更新。</p>
        <button type="button" class="primary-button" data-action="accept-current-disclosure" :disabled="mutating" @click="acceptCurrentDisclosure">重新启用</button>
        <p v-if="error" role="alert">{{ error }}</p>
        <button type="button" :disabled="mutating" @click="refresh">刷新</button>
      </section>
      <section v-else-if="setup?.state !== 'ready'" class="terminal-agent-panel__scroll" data-agent-panel-state="unavailable">
        <div class="agent-panel-empty">
          <h3>{{ error ? 'Agent 暂不可用' : 'Agent 正在恢复' }}</h3>
          <p role="alert">{{ error || '运行时还在连接中，稍候会自动刷新。' }}</p>
          <div class="agent-setup-form__actions">
            <button type="button" class="text-button" :disabled="mutating" @click="retry">重试</button>
            <button v-if="bindingId" type="button" class="primary-button" :disabled="mutating" @click="activate">重新激活</button>
          </div>
        </div>
      </section>
      <div v-else data-agent-panel-state="ready" class="terminal-agent-panel__ready">
        <div v-if="pendingAutoPolicy" class="terminal-agent-policy__confirm" role="alertdialog" aria-label="确认完全放行" data-agent-policy-confirm>
          <p>完全放行后，Agent 对已授权 Pane 的每次写入都会立即执行，不再弹审批。</p>
          <div class="terminal-agent-policy__confirm-actions">
            <button type="button" class="primary-button" :disabled="mutating" data-action="confirm-policy-auto" @click="changeWritePolicy('auto')">确认放行</button>
            <button type="button" class="text-button" :disabled="mutating" @click="pendingAutoPolicy = false">取消</button>
          </div>
        </div>
        <div v-if="historyOpen" class="terminal-agent-history-backdrop" data-agent-history-backdrop @click="historyOpen = false" />
        <div v-if="historyOpen" class="terminal-agent-history" data-agent-history>
          <div class="terminal-agent-history__heading">
            <strong>历史记录</strong>
            <span>{{ conversations.length }} 个会话</span>
          </div>
          <p v-if="conversations.length === 0" class="muted">暂无会话</p>
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
                aria-label="重命名会话"
                @keydown.enter.prevent="commitRename"
                @keydown.esc="cancelRename"
                @blur="commitRename"
                @click.stop
              />
              <span v-else class="terminal-agent-history__item-title">{{ entry.title || '未命名会话' }}</span>
              <span class="terminal-agent-history__item-status">{{ entry.status === 'open' ? '进行中' : '已停止' }}</span>
            </button>
            <button
              type="button"
              class="icon-button icon-only terminal-agent-history__rename"
              :aria-label="`重命名会话 ${entry.title || '未命名会话'}`"
              title="重命名会话"
              data-action="rename-history-conversation"
              @click="beginRename(entry.conversation_id)"
            >
              <Pencil :size="14" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="icon-button icon-only terminal-agent-history__delete"
              :aria-label="`删除会话 ${entry.title || '未命名会话'}`"
              title="删除会话"
              data-action="delete-history-conversation"
              @click="deleteHistoryConversation(entry.conversation_id)"
            >
              <Trash2 :size="14" aria-hidden="true" />
            </button>
          </div>
        </div>
        <p v-if="error" class="agent-panel-error" role="alert">{{ error }}</p>
        <AgentChatSession v-if="selectedConversationId" :key="selectedConversationId" :conversation-id="selectedConversationId" :enabled="true" variant="floating" @pending-count="forwardPending" @backend-state="backendState = $event" />
        <p v-else class="terminal-agent-empty">选择历史会话或点击“新建会话”。</p>
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
        :aria-label="`调整 Agent 面板大小（${resizeLabels[edge]}）`"
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
import { useClientRuntime } from '../../runtime'
import AgentChatSession from './AgentChatSession.vue'
import AgentPanePicker from './AgentPanePicker.vue'
import AgentSetupForm from './AgentSetupForm.vue'

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
    settingsError.value = cause instanceof ApiError ? agentReasonMessage(cause.code) : '无法保存窗格授权。'
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

async function changeWritePolicy(policy: 'manual' | 'auto') {
  const binding = bindingId.value
  if (binding === null || mutating.value) return
  mutating.value = true
  try {
    await runtime.api.agents.setWritePolicy(binding, policy)
    pendingAutoPolicy.value = false
    await refresh()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法更新写入审批方式。'
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
const panelTitle = computed(() => selectedEntry.value?.title?.trim() || (selectedConversationId.value ? 'Agent 对话' : 'Term Agent'))
const disclosureRecoveryRequired = computed(() => setup.value?.state === 'unavailable' && (
  setup.value.reason_code === 'binding_disclosure_stale'
  || setup.value.reason_code === 'binding_disclosure_required'
))
const backendLabels: Record<string, string> = {
  connecting: '连接中', idle: '空闲', busy: '处理中', retry: '重试中', ready: '已就绪',
  unavailable: '离线', context_lost: '上下文丢失', reconciling: '恢复中', closed: '已关闭',
}
const mergedStatusLabel = computed(() => {
  const state = setup.value?.state
  if (state !== 'ready') return state === undefined ? '' : readinessLabels[state]
  if (backendState.value === null || backendState.value === 'ready') return '已就绪'
  return backendLabels[backendState.value] ?? backendState.value
})
const readinessLabels: Record<AgentSetupResponse['state'], string> = {
  unconfigured: '未设置', deployment_required: '需部署', activating: '激活中', ready: '已就绪', unavailable: '不可用',
}
const resizeEdges: FloatingPanelResizeEdge[] = ['n', 'e', 's', 'w', 'ne', 'se', 'sw', 'nw']
const resizeLabels: Record<FloatingPanelResizeEdge, string> = {
  n: '上边缘', e: '右边缘', s: '下边缘', w: '左边缘', ne: '右上角', se: '右下角', sw: '左下角', nw: '左上角',
}

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
