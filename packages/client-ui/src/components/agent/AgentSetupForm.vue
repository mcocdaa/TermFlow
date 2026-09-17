<template>
  <form class="agent-setup-form" @submit.prevent="submit">
    <h3 class="agent-setup-form__title">
      设置 Term Agent
      <ContextHelp label="Term Agent 说明" :text="introHelp" />
    </h3>

    <AgentPanePicker
      :model-value="paneIds"
      :panes="panes"
      :disabled="busy"
      @update:model-value="onPanes"
    />

    <p v-if="localError || error" class="form-error" role="alert">{{ localError || error }}</p>
    <div class="agent-setup-form__actions">
      <button type="button" class="text-button" :disabled="busy" data-action="refresh-setup" @click="emit('refresh')">刷新状态</button>
      <button type="submit" class="primary-button" :disabled="busy">{{ busy ? '正在设置…' : '启用 Agent' }}</button>
    </div>
  </form>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { AgentSetupProfileSummary, AgentSetupResponse } from '@termflow/client-contracts'
import type { PaneTopology } from '../../types'
import AgentPanePicker from './AgentPanePicker.vue'
import ContextHelp from '../common/ContextHelp.vue'
export type AgentSetupSelection = { paneIds: string[]; topologyRevision: number; disclosureFingerprint: string; accepted: true } & ({ kind: 'existing'; profileId: string } | { kind: 'new'; profileDisplayName: string })
const props = defineProps<{ setup: AgentSetupResponse; profiles: AgentSetupProfileSummary[]; panes: PaneTopology[]; busy: boolean; error: string }>()
const emit = defineEmits<{ submit: [selection: AgentSetupSelection]; refresh: [] }>()
const paneIds = ref<string[]>([]), localError = ref('')
const panesTouched = ref(false)
const introHelp = computed(() => {
  const disclosure = props.setup.disclosure
  return disclosure === null
    ? '部署尚未配置模型提供方，请联系管理员完成部署。'
    : `Agent 使用部署配置的模型 ${disclosure.provider_id} · ${disclosure.model_id} 读取授权窗格的内容；写入命令时仍会逐一弹审批。`
})
watch(() => props.panes.map((pane) => pane.pane_id).join(','), () => {
  if (panesTouched.value) return
  paneIds.value = props.panes.map((pane) => pane.pane_id)
}, { immediate: true })
function onPanes(ids: string[]) {
  panesTouched.value = true
  paneIds.value = ids
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
