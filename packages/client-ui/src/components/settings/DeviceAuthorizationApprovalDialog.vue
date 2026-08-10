<template>
  <div class="dialog-backdrop" role="presentation">
    <section data-action="device-approval-dialog" class="dialog-panel device-approval-dialog" role="dialog" aria-modal="true" aria-labelledby="device-approval-title">
      <header><div><p class="eyebrow">Client authorization</p><h2 id="device-approval-title">授权新客户端</h2></div><button class="icon-button icon-only" type="button" aria-label="关闭" title="关闭" @click="emit('closed')">×</button></header>
      <form v-if="preview === null" data-action="lookup-device-approval" class="security-form" @submit.prevent="lookup()">
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
        <form class="security-form" @submit.prevent="approve('allow')">
          <template v-if="preview.totp_required"><label for="device-approval-totp">双重验证码</label><input id="device-approval-totp" v-model="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></template>
          <p v-if="error" role="alert" class="form-error">{{ error }}</p>
          <div class="dialog-actions"><button class="secondary-button" type="button" :disabled="busy" @click="approve('deny')">拒绝</button><button data-action="approve-device" class="primary-button" type="submit" :disabled="busy">允许此设备</button></div>
        </form>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import { useBottomToast } from '../../composables/useBottomToast'
import { useDeviceAuthorizationApproval } from '../../composables/useDeviceAuthorizationApproval'
import { useClientRuntime } from '../../runtime'

const emit = defineEmits<{ approved: []; closed: [] }>()
const runtime = useClientRuntime()
const toast = useBottomToast()
const { preview, userCode, totpCode, busy, error, success, lookup, decide } = useDeviceAuthorizationApproval(runtime.api.oauth)
function formatExpiry(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString() }
async function approve(decision: 'allow' | 'deny') {
  await decide(decision)
  if (success.value === 'approved') { toast.show({ text: '已授权', tone: 'success' }); emit('approved') }
  else if (success.value === 'denied') emit('closed')
}
</script>
