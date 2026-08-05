<template>
  <div class="dialog-backdrop" role="presentation">
    <section data-action="device-approval-dialog" class="dialog-panel device-approval-dialog" role="dialog" aria-modal="true" aria-labelledby="device-approval-title">
      <header><div><p class="eyebrow">Client authorization</p><h2 id="device-approval-title">授权新客户端</h2></div><button class="icon-button icon-only" type="button" aria-label="关闭" title="关闭" @click="emit('closed')">×</button></header>
      <form v-if="preview === null" data-action="lookup-device-approval" class="security-form" @submit.prevent="lookup">
        <label for="device-approval-code">设备码</label>
        <input id="device-approval-code" v-model="userCode" inputmode="text" autocomplete="off" placeholder="ABCD-EFGH" required />
        <p v-if="error" role="alert" class="form-error">{{ error }}</p>
        <button class="primary-button" type="submit" :disabled="busy">{{ busy ? '正在查找…' : '继续' }}</button>
      </form>
      <template v-else>
        <dl class="consent-details device-approval-details">
          <div><dt>客户端</dt><dd>{{ preview.client_name }}</dd></div>
          <div><dt>平台</dt><dd>{{ preview.platform }}{{ preview.client_version ? ` · ${preview.client_version}` : '' }}</dd></div>
          <div><dt>权限</dt><dd>{{ preview.scopes.join(' · ') }}</dd></div>
          <div><dt>有效期至</dt><dd>{{ formatExpiry(preview.expires_at) }}</dd></div>
        </dl>
        <form class="security-form" @submit.prevent="decide('allow')">
          <template v-if="preview.totp_required"><label for="device-approval-totp">当前验证码</label><input id="device-approval-totp" v-model="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></template>
          <p v-if="error" role="alert" class="form-error">{{ error }}</p>
          <div class="dialog-actions"><button class="secondary-button" type="button" :disabled="busy" @click="decide('deny')">拒绝</button><button data-action="approve-device" class="primary-button" type="submit" :disabled="busy">允许此设备</button></div>
        </form>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import type { OAuthAuthorizationPreviewResponse } from '@termflow/client-contracts'
import { ref } from 'vue'
import { ApiError } from '@termflow/client-core'
import { useBottomToast } from '../../composables/useBottomToast'
import { useClientRuntime } from '../../runtime'

const emit = defineEmits<{ approved: []; closed: [] }>()
const runtime = useClientRuntime()
const toast = useBottomToast()
const preview = ref<OAuthAuthorizationPreviewResponse | null>(null)
const userCode = ref('')
const totpCode = ref('')
const busy = ref(false)
const error = ref('')

function normalizeCode(value: string) { return value.trim().toUpperCase().replace(/\s+/g, '') }
function formatExpiry(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString() }
async function lookup() {
  const code = normalizeCode(userCode.value)
  if (!/^[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(code)) { error.value = '请输入格式为 ABCD-EFGH 的设备码。'; return }
  userCode.value = code; busy.value = true; error.value = ''
  try { preview.value = await runtime.api.oauth.deviceAuthorizationPreview(code) }
  catch (cause) { error.value = cause instanceof ApiError && cause.code === 'authorization_expired' ? '设备码无效或已过期。' : '无法查找设备码。' }
  finally { busy.value = false }
}
async function decide(decision: 'allow' | 'deny') {
  if (preview.value === null || busy.value) return
  busy.value = true; error.value = ''
  try {
    const result = await runtime.api.oauth.decideAuthorization({ transactionId: preview.value.transaction_id, decision, ...(totpCode.value ? { totpCode: totpCode.value } : {}) })
    totpCode.value = ''
    if (result.status === 'approved') { toast.show({ text: '已授权', tone: 'success' }); emit('approved') }
    else emit('closed')
  } catch (cause) { totpCode.value = ''; error.value = cause instanceof ApiError && cause.code === 'authorization_expired' ? '设备码无效或已过期。' : '无法完成授权。' }
  finally { busy.value = false }
}
</script>
