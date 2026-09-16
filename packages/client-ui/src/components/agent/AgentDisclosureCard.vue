<template>
  <section class="agent-disclosure" aria-label="数据发送说明">
    <h4 class="agent-disclosure__title">数据发送说明</h4>
    <p class="agent-disclosure__route">{{ disclosure.provider_id }} · {{ disclosure.model_id }}</p>
    <dl class="agent-disclosure__grid">
      <div class="agent-disclosure__row">
        <dt>端点</dt>
        <dd>{{ disclosure.endpoint_origin }} · {{ disclosure.region }}</dd>
      </div>
      <div class="agent-disclosure__row">
        <dt>数据保留</dt>
        <dd>{{ disclosure.retention_terms }} · {{ disclosure.retention_version }}</dd>
      </div>
      <div class="agent-disclosure__row">
        <dt>模型训练</dt>
        <dd :data-flag="disclosure.no_training ? 'safe' : 'unknown'">{{ disclosure.no_training ? '提供方声明不用于训练' : '提供方未声明不用于训练' }}</dd>
      </div>
    </dl>
    <details class="agent-disclosure__details">
      <summary>披露详情</summary>
      <dl class="agent-disclosure__grid">
        <div class="agent-disclosure__row">
          <dt>政策版本</dt>
          <dd>{{ disclosure.policy_version }}</dd>
        </div>
        <div class="agent-disclosure__row">
          <dt>凭据来源</dt>
          <dd>{{ disclosure.credential_source ?? '未配置' }}</dd>
        </div>
        <div class="agent-disclosure__row">
          <dt>披露摘要</dt>
          <dd><code>{{ disclosure.disclosure_fingerprint }}</code></dd>
        </div>
      </dl>
    </details>
  </section>
</template>
<script setup lang="ts">
import type { AgentProviderDisclosureResponse } from '@termflow/client-contracts'
defineProps<{ disclosure: AgentProviderDisclosureResponse }>()
</script>
