<template>
  <form class="agent-setup-form" @submit.prevent="submit">
    <h3>设置 Term Agent</h3>
    <label>Agent Profile
      <select v-model="profileId" name="profileId" :disabled="busy">
        <option value="">新建 Profile</option>
        <option v-for="profile in profiles" :key="profile.profile_id" :value="profile.profile_id" :disabled="!matchesDisclosure(profile)">{{ profile.display_name }}{{ matchesDisclosure(profile) ? '' : '（提供方配置不匹配）' }}</option>
      </select>
    </label>
    <label v-if="!profileId">名称<input v-model="displayName" name="profileDisplayName" maxlength="120" :disabled="busy" /></label>
    <fieldset :disabled="busy"><legend>允许访问的窗格</legend>
      <label v-for="pane in panes" :key="pane.pane_id"><input v-model="paneIds" type="checkbox" name="paneIds" :value="pane.pane_id" />{{ pane.title || pane.pane_id }} ({{ pane.pane_id }})</label>
      <p v-if="!panes.length">暂无可用窗格，请连接终端后刷新。</p>
    </fieldset>
    <section v-if="setup.disclosure" class="agent-provider-disclosure">
      <h4>数据发送说明</h4>
      <p>{{ setup.disclosure.provider_id }} · {{ setup.disclosure.model_id }}</p>
      <p>{{ setup.disclosure.endpoint_origin }} · {{ setup.disclosure.region }}</p>
      <p>{{ setup.disclosure.retention_terms }} · 保留政策版本 {{ setup.disclosure.retention_version }}</p>
      <p>{{ setup.disclosure.no_training ? '提供方声明不用于训练' : '提供方未声明不用于训练' }}</p>
      <p>披露政策版本 {{ setup.disclosure.policy_version }}</p>
      <p>凭据来源 {{ setup.disclosure.credential_source ?? '未配置' }}</p>
      <p>披露摘要 <code>{{ setup.disclosure.disclosure_fingerprint }}</code></p>
      <label><input v-model="accepted" type="checkbox" name="accepted" :disabled="busy" />我同意将所选窗格的终端上下文和对话发送给上述提供方。</label>
    </section>
    <p v-else>提供方披露暂不可用，请管理员完成配置。</p>
    <p v-if="localError || error" role="alert">{{ localError || error }}</p>
    <button type="submit" class="primary-button" :disabled="busy">{{ busy ? '正在设置…' : '启用 Agent' }}</button>
  </form>
</template>
<script setup lang="ts">
import { ref, watch } from 'vue'
import type { AgentSetupProfileSummary, AgentSetupResponse } from '@termflow/client-contracts'
import type { PaneTopology } from '../../types'
export type AgentSetupSelection = { paneIds: string[]; topologyRevision: number; disclosureFingerprint: string; accepted: true } & ({ kind: 'existing'; profileId: string } | { kind: 'new'; profileDisplayName: string })
const props = defineProps<{ setup: AgentSetupResponse; profiles: AgentSetupProfileSummary[]; panes: PaneTopology[]; busy: boolean; error: string }>()
const emit = defineEmits<{ submit: [selection: AgentSetupSelection] }>()
const profileId = ref(''), displayName = ref(''), paneIds = ref<string[]>([]), accepted = ref(false), localError = ref('')
watch(() => props.setup.disclosure?.disclosure_fingerprint, () => { accepted.value = false })
function matchesDisclosure(profile: AgentSetupProfileSummary) {
  const disclosure = props.setup.disclosure
  return disclosure !== null && profile.provider_id === disclosure.provider_id && profile.model_id === disclosure.model_id
}
function submit() {
  localError.value = ''
  const fingerprint = props.setup.disclosure?.disclosure_fingerprint
  if (props.busy) return
  if (!paneIds.value.length || paneIds.value.some((id) => !props.panes.some((pane) => pane.pane_id === id))) { localError.value = '请至少选择一个当前窗格。'; return }
  if (!accepted.value || !fingerprint || props.setup.topology_revision === null) { localError.value = '请确认当前数据发送说明。'; return }
  if (profileId.value ? !props.profiles.some((profile) => profile.profile_id === profileId.value && matchesDisclosure(profile)) : !displayName.value.trim()) { localError.value = '请选择与当前提供方匹配的 Profile 或填写新名称。'; return }
  const common = { paneIds: [...paneIds.value], topologyRevision: props.setup.topology_revision, disclosureFingerprint: fingerprint, accepted: true as const }
  emit('submit', profileId.value ? { ...common, kind: 'existing', profileId: profileId.value } : { ...common, kind: 'new', profileDisplayName: displayName.value.trim() })
}
</script>
