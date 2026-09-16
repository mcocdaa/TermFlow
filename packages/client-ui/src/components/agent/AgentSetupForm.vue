<template>
  <form class="agent-setup-form" @submit.prevent="submit">
    <header class="agent-setup-form__intro">
      <h3>设置 Term Agent <ContextHelp label="Term Agent 说明" text="Agent 使用部署配置的模型读取你授权的窗格内容；写入命令时仍会逐一弹审批。" /></h3>
    </header>

    <p class="agent-setup-form__model">
      模型 <strong>{{ modelLabel }}</strong>
      <ContextHelp label="模型配置说明" text="模型和凭据由部署配置决定，Term Agent 内不可修改。" />
    </p>

    <fieldset class="agent-setup-panes" :disabled="busy">
      <legend>
        允许 Agent 访问的窗格
        <ContextHelp label="窗格授权说明" text="窗格是终端里的一块分屏（% 编号）。Agent 只能读取勾选窗格的内容；写入仍会逐一弹审批。默认已全选。" />
      </legend>
      <div class="agent-setup-panes__actions">
        <button type="button" :disabled="busy || !panes.length" data-action="select-all-panes" @click="selectAllPanes">全选</button>
        <button type="button" :disabled="busy || !paneIds.length" data-action="clear-panes" @click="clearPanes">清空</button>
        <span class="agent-setup-panes__count">已选 {{ paneIds.length }}/{{ panes.length }}</span>
      </div>
      <label v-for="pane in panes" :key="pane.pane_id" class="agent-setup-pane">
        <input v-model="paneIds" type="checkbox" name="paneIds" :value="pane.pane_id" @change="panesTouched = true" />
        <span class="agent-setup-pane__text">
          <code class="agent-setup-pane__id">{{ pane.pane_id }}</code>
          <span class="agent-setup-pane__title">{{ pane.current_command || pane.title || pane.window_id }}</span>
        </span>
      </label>
      <p v-if="!panes.length" class="agent-setup-panes__empty">暂无可用窗格，请连接终端后刷新。</p>
    </fieldset>

    <p v-if="localError || error" class="form-error" role="alert">{{ localError || error }}</p>
    <button type="submit" class="primary-button" :disabled="busy">{{ busy ? '正在设置…' : '启用 Agent' }}</button>
  </form>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { AgentSetupProfileSummary, AgentSetupResponse } from '@termflow/client-contracts'
import type { PaneTopology } from '../../types'
import ContextHelp from '../common/ContextHelp.vue'
export type AgentSetupSelection = { paneIds: string[]; topologyRevision: number; disclosureFingerprint: string; accepted: true } & ({ kind: 'existing'; profileId: string } | { kind: 'new'; profileDisplayName: string })
const props = defineProps<{ setup: AgentSetupResponse; profiles: AgentSetupProfileSummary[]; panes: PaneTopology[]; busy: boolean; error: string }>()
const emit = defineEmits<{ submit: [selection: AgentSetupSelection] }>()
const paneIds = ref<string[]>([]), localError = ref('')
const panesTouched = ref(false)
const modelLabel = computed(() => {
  const disclosure = props.setup.disclosure
  return disclosure === null ? '部署未配置' : `${disclosure.provider_id} · ${disclosure.model_id}`
})
watch(() => props.panes.map((pane) => pane.pane_id).join(','), () => {
  if (panesTouched.value) return
  paneIds.value = props.panes.map((pane) => pane.pane_id)
}, { immediate: true })
function selectAllPanes() {
  panesTouched.value = true
  paneIds.value = props.panes.map((pane) => pane.pane_id)
}
function clearPanes() {
  panesTouched.value = true
  paneIds.value = []
}
function submit() {
  localError.value = ''
  const disclosure = props.setup.disclosure
  if (props.busy) return
  if (!paneIds.value.length || paneIds.value.some((id) => !props.panes.some((pane) => pane.pane_id === id))) { localError.value = '请至少选择一个当前窗格。'; return }
  if (disclosure === null || props.setup.topology_revision === null) { localError.value = '提供方配置暂不可用，请管理员完成部署。'; return }
  const common = { paneIds: [...paneIds.value], topologyRevision: props.setup.topology_revision, disclosureFingerprint: disclosure.disclosure_fingerprint, accepted: true as const }
  const matching = props.profiles.find((profile) => profile.provider_id === disclosure.provider_id && profile.model_id === disclosure.model_id)
  emit('submit', matching
    ? { ...common, kind: 'existing', profileId: matching.profile_id }
    : { ...common, kind: 'new', profileDisplayName: `${disclosure.provider_id} · ${disclosure.model_id}` })
}
</script>
