<template>
  <form class="agent-setup-form" @submit.prevent="submit">
    <header class="agent-setup-form__intro">
      <h3>设置 Term Agent</h3>
      <p class="muted">选择运行配置和允许访问的窗格，确认数据发送说明后启用。</p>
    </header>

    <div class="agent-setup-field">
      <label class="agent-setup-field__label" for="agent-setup-profile">Agent Profile</label>
      <select id="agent-setup-profile" v-model="profileId" name="profileId" :disabled="busy">
        <option value="">新建 Profile</option>
        <option v-for="profile in profiles" :key="profile.profile_id" :value="profile.profile_id" :disabled="!matchesDisclosure(profile)">{{ profile.display_name }}{{ matchesDisclosure(profile) ? '' : '（提供方配置不匹配）' }}</option>
      </select>
    </div>

    <div v-if="!profileId" class="agent-setup-field">
      <label class="agent-setup-field__label" for="agent-setup-name">名称</label>
      <input id="agent-setup-name" v-model="displayName" name="profileDisplayName" maxlength="120" placeholder="例如 DeepSeek live profile" :disabled="busy" />
    </div>

    <fieldset class="agent-setup-panes" :disabled="busy">
      <legend>允许访问的窗格</legend>
      <p class="agent-setup-panes__hint">Agent 只能读取和写入勾选的窗格。</p>
      <label v-for="pane in panes" :key="pane.pane_id" class="agent-setup-pane">
        <input v-model="paneIds" type="checkbox" name="paneIds" :value="pane.pane_id" />
        <span class="agent-setup-pane__text">
          <span class="agent-setup-pane__title">{{ pane.title || pane.pane_id }}</span>
          <code class="agent-setup-pane__id">{{ pane.pane_id }}</code>
        </span>
      </label>
      <p v-if="!panes.length" class="agent-setup-panes__empty">暂无可用窗格，请连接终端后刷新。</p>
    </fieldset>

    <AgentDisclosureCard v-if="setup.disclosure" :disclosure="setup.disclosure" />
    <p v-else class="muted">提供方披露暂不可用，请管理员完成配置。</p>

    <label v-if="setup.disclosure" class="agent-setup-consent">
      <input v-model="accepted" type="checkbox" name="accepted" :disabled="busy" />
      <span>我同意将所选窗格的终端上下文和对话发送给上述提供方。</span>
    </label>

    <p v-if="localError || error" class="form-error" role="alert">{{ localError || error }}</p>
    <button type="submit" class="primary-button" :disabled="busy">{{ busy ? '正在设置…' : '启用 Agent' }}</button>
  </form>
</template>
<script setup lang="ts">
import { ref, watch } from 'vue'
import type { AgentSetupProfileSummary, AgentSetupResponse } from '@termflow/client-contracts'
import type { PaneTopology } from '../../types'
import AgentDisclosureCard from './AgentDisclosureCard.vue'
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
