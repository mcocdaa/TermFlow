<template>
  <section class="authorize-view">
    <div class="auth-card consent-card">
      <p class="eyebrow">Native client authorization</p>
      <h1>授权 TermFlow 客户端</h1>
      <p v-if="loading">正在读取授权请求…</p>
      <template v-else-if="preview">
        <dl class="consent-details">
          <div><dt>服务器</dt><dd>{{ preview.issuer }}</dd></div>
          <div><dt>客户端</dt><dd>{{ preview.client_name }}</dd></div>
          <div><dt>平台</dt><dd>{{ preview.platform }}{{ preview.client_version ? ` · ${preview.client_version}` : '' }}</dd></div>
          <div><dt>公钥指纹</dt><dd><code>{{ preview.key_fingerprint }}</code></dd></div>
          <div><dt>权限</dt><dd>{{ preview.scopes.join(' · ') }}</dd></div>
        </dl>
        <form class="security-form" @submit.prevent="decide('allow')">
          <label for="authorize-admin-token">管理员令牌</label>
          <input id="authorize-admin-token" v-model="adminToken" type="password" autocomplete="off" required />
          <template v-if="preview.totp_required">
            <label for="authorize-totp">当前验证码</label>
            <input id="authorize-totp" v-model="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
          </template>
          <p v-if="message" role="alert" class="form-error">{{ message }}</p>
          <div class="dialog-actions"><button class="secondary-button" type="button" @click="decide('deny')">拒绝</button><button class="primary-button" type="submit">允许此客户端</button></div>
        </form>
      </template>
      <p v-else role="alert" class="form-error">{{ message || '授权请求无效或已过期。' }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import type { OAuthAuthorizationPreviewResponse } from '@termflow/client-contracts'
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError } from '@termflow/client-core'
import { useClientRuntime } from '../runtime'

const runtime = useClientRuntime()
const route = useRoute()
const preview = ref<OAuthAuthorizationPreviewResponse | null>(null)
const loading = ref(true)
const adminToken = ref('')
const totpCode = ref('')
const message = ref('')

onMounted(async () => {
  const transactionId = typeof route.query.transaction_id === 'string' ? route.query.transaction_id : ''
  try { preview.value = await runtime.api.oauth.authorizationPreview(transactionId) }
  catch (error) { message.value = error instanceof ApiError ? error.message : '授权请求无效或已过期。' }
  finally { loading.value = false }
})

async function decide(decision: 'allow' | 'deny') {
  if (preview.value === null) return
  try {
    const result = await runtime.api.oauth.decideAuthorization({
      transactionId: preview.value.transaction_id,
      decision,
      adminToken: adminToken.value,
      ...(totpCode.value ? { totpCode: totpCode.value } : {}),
    })
    adminToken.value = ''; totpCode.value = ''
    runtime.authorizationCompletion.navigate(result.callback_uri)
  } catch (error) {
    adminToken.value = ''; totpCode.value = ''
    message.value = error instanceof ApiError ? error.message : '无法完成授权。'
  }
}
</script>
