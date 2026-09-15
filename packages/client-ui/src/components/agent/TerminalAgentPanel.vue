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
      </div>
      <h2 class="terminal-agent-panel__title" data-agent-panel-title>{{ panelTitle }}</h2>
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
      <section v-if="loading" data-agent-panel-state="loading" role="status">正在加载 Agent…</section>
      <section v-else-if="setup?.state === 'unconfigured'" data-agent-panel-state="unconfigured">
        <AgentSetupForm :setup="setup" :profiles="profiles" :panes="panes" :busy="mutating" :error="error" @submit="submitSetup" />
        <button type="button" :disabled="mutating" @click="refresh">刷新</button>
      </section>
      <section v-else-if="setup?.state === 'deployment_required'" data-agent-panel-state="deployment_required">
        <h3>需要部署 Agent 服务</h3><p>请管理员完成运行环境和提供方配置。</p><button type="button" :disabled="mutating" @click="refresh">检查部署状态</button>
      </section>
      <section v-else-if="setup?.state === 'activating'" data-agent-panel-state="activating" role="status">
        <h3>Agent 正在激活</h3><button type="button" :disabled="mutating" @click="refresh">刷新状态</button>
      </section>
      <section v-else-if="disclosureRecoveryRequired" data-agent-panel-state="unavailable" data-agent-disclosure-recovery>
        <h3>请重新确认数据发送说明</h3>
        <template v-if="setup?.disclosure">
          <p>{{ setup.disclosure.provider_id }} · {{ setup.disclosure.model_id }}</p>
          <p>{{ setup.disclosure.endpoint_origin }} · {{ setup.disclosure.region }}</p>
          <p>{{ setup.disclosure.retention_terms }} · 保留政策版本 {{ setup.disclosure.retention_version }}</p>
          <p>{{ setup.disclosure.no_training ? '提供方声明不用于训练' : '提供方未声明不用于训练' }}</p>
          <p>披露政策版本 {{ setup.disclosure.policy_version }}</p>
          <p>凭据来源 {{ setup.disclosure.credential_source ?? '未配置' }}</p>
          <p>披露摘要 <code>{{ setup.disclosure.disclosure_fingerprint }}</code></p>
          <label><input v-model="disclosureAccepted" type="checkbox" name="acceptCurrentDisclosure" :disabled="mutating" />我同意将所选窗格的终端上下文和对话发送给上述提供方。</label>
          <button type="button" class="primary-button" data-action="accept-current-disclosure" :disabled="mutating || !disclosureAccepted" @click="acceptCurrentDisclosure">确认并重新连接</button>
        </template>
        <p v-if="error" role="alert">{{ error }}</p>
        <button type="button" :disabled="mutating" @click="refresh">刷新</button>
      </section>
      <section v-else-if="setup?.state !== 'ready'" data-agent-panel-state="unavailable">
        <h3>{{ error ? 'Agent 暂不可用' : 'Agent 正在恢复' }}</h3><p role="alert">{{ error || '运行时还在连接中，稍候会自动刷新。' }}</p><button type="button" :disabled="mutating" @click="retry">重试</button><button v-if="bindingId && error" type="button" :disabled="mutating" @click="activate">重新激活</button>
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
          <button
            v-for="entry in conversations"
            :key="entry.conversation_id"
            type="button"
            class="terminal-agent-history__item"
            :class="{ 'is-selected': entry.conversation_id === selectedConversationId }"
            :aria-current="entry.conversation_id === selectedConversationId ? 'true' : undefined"
            :data-agent-history-conversation="entry.conversation_id"
            @click="selectConversation(entry.conversation_id)"
          >
            <span class="terminal-agent-history__item-title">{{ entry.title || '未命名会话' }}</span>
            <span class="terminal-agent-history__item-status">{{ entry.status === 'open' ? '进行中' : '已停止' }}</span>
          </button>
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
import { History, MessageSquarePlus, Move, X } from '@lucide/vue'
import type { AgentSetupResponse } from '@termflow/client-contracts'
import { useFloatingPanel, type FloatingPanelResizeEdge } from '../../composables/useFloatingPanel'
import { useTermAgent } from '../../composables/useTermAgent'
import { useClientRuntime } from '../../runtime'
import AgentChatSession from './AgentChatSession.vue'
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
const disclosureAccepted = ref(false)
const historyOpen = ref(false)
const backendState = ref<string | null>(null)
const pendingAutoPolicy = ref(false)
const writePolicy = computed<'manual' | 'auto'>(() => setup.value?.write_policy === 'auto' ? 'auto' : 'manual')

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

watch(() => setup.value?.state, (state) => { if (state) emit('readiness', state) }, { immediate: true })
watch(() => setup.value?.disclosure?.disclosure_fingerprint, () => { disclosureAccepted.value = false })
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
async function acceptCurrentDisclosure() { if (disclosureAccepted.value) await acceptDisclosure() }
</script>
