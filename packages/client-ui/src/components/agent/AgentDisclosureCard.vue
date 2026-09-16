<template>
  <section class="agent-disclosure" aria-label="数据发送说明">
    <h4 class="agent-disclosure__title">数据发送说明</h4>
    <p class="agent-disclosure__route">{{ disclosure.provider_id }} · {{ disclosure.model_id }}</p>
    <p class="agent-disclosure__facts">
      <span>{{ disclosure.endpoint_origin }} · {{ disclosure.region }}</span>
      <span>保留 {{ disclosure.retention_terms }} · {{ disclosure.retention_version }}</span>
      <span :data-flag="disclosure.no_training ? 'safe' : 'unknown'">{{ disclosure.no_training ? '提供方声明不用于训练' : '提供方未声明不用于训练' }}</span>
    </p>
    <details class="agent-disclosure__details">
      <summary>更多信息</summary>
      <p>披露政策版本 {{ disclosure.policy_version }}</p>
      <p>凭据来源 {{ disclosure.credential_source ?? '未配置' }}</p>
      <p class="agent-disclosure__fingerprint">披露摘要 <code>{{ disclosure.disclosure_fingerprint }}</code></p>
    </details>
  </section>
</template>
<script setup lang="ts">
import type { AgentProviderDisclosureResponse } from '@termflow/client-contracts'
defineProps<{ disclosure: AgentProviderDisclosureResponse }>()
</script>
