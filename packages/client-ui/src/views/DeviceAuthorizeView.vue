<template>
  <section class="authorize-view">
    <div class="auth-card consent-card">
      <p class="eyebrow">Cross-device authorization</p>
      <h1>授权新设备</h1>
      <template v-if="preview === null && !loading">
        <form data-action="lookup-device" class="security-form" @submit.prevent="lookup">
          <label for="device-user-code">设备码</label>
          <input id="device-user-code" v-model="userCode" inputmode="text" autocomplete="off" placeholder="ABCD-EFGH" required />
          <p v-if="message" role="alert" class="form-error">{{ message }}</p>
          <button class="primary-button" type="submit" :disabled="busy">查找设备</button>
        </form>
      </template>
      <p v-else-if="loading">正在读取设备授权…</p>
      <template v-else-if="preview">
        <p class="form-hint">请确认以下设备请求后再授权。设备码：<code>{{ userCode }}</code></p>
        <dl class="consent-details">
          <div><dt>客户端</dt><dd>{{ preview.client_name }}</dd></div>
          <div><dt>平台</dt><dd>{{ preview.platform }}{{ preview.client_version ? ` · ${preview.client_version}` : '' }}</dd></div>
          <div><dt>权限</dt><dd>{{ preview.scopes.join(' · ') }}</dd></div>
          <div><dt>有效期至</dt><dd>{{ formatExpiry(preview.expires_at) }}<span class="status-pill">{{ statusLabel }}</span></dd></div>
        </dl>
        <form class="security-form" @submit.prevent="decide('allow')">
          <template v-if="preview.totp_required">
            <label for="device-authorize-totp">当前验证码</label>
            <input id="device-authorize-totp" v-model="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
          </template>
          <p v-if="message" role="alert" class="form-error">{{ message }}</p>
          <div class="dialog-actions"><button class="secondary-button" type="button" :disabled="busy" @click="decide('deny')">拒绝</button><button class="primary-button" type="submit" :disabled="busy">允许此设备</button></div>
        </form>
      </template>
      <p v-if="success" role="status" class="form-success">{{ success }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import type { OAuthAuthorizationPreviewResponse } from '@termflow/client-contracts'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '@termflow/client-core'
import { useClientRuntime } from '../runtime'

const runtime = useClientRuntime()
const route = useRoute()
const router = useRouter()
const preview = ref<OAuthAuthorizationPreviewResponse | null>(null)
const loading = ref(true)
const busy = ref(false)
const userCode = ref('')
const totpCode = ref('')
const message = ref('')
const success = ref('')

const statusLabel = computed(() => new Date(preview.value?.expires_at ?? 0).getTime() > Date.now() ? '待确认' : '已过期')

function normalizeCode(value: string): string {
  return value.trim().toUpperCase().replace(/\s+/g, '')
}

function formatExpiry(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

async function lookup() {
  const code = normalizeCode(userCode.value)
  if (!/^[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(code)) {
    message.value = '请输入格式为 ABCD-EFGH 的设备码。'
    return
  }
  userCode.value = code
  loading.value = true; message.value = ''; success.value = ''
  try { preview.value = await runtime.api.oauth.deviceAuthorizationPreview(code) }
  catch (error) {
    if (error instanceof ApiError && error.kind === 'authentication') {
      await router.replace({ path: '/login', query: { redirect: route.fullPath } })
      return
    }
    message.value = error instanceof ApiError && error.code === 'authorization_expired' ? '设备码无效或已过期。' : error instanceof ApiError ? error.message : '设备码无效或已过期。'
  } finally { loading.value = false }
}

async function decide(decision: 'allow' | 'deny') {
  if (preview.value === null || busy.value) return
  busy.value = true; message.value = ''
  try {
    const result = await runtime.api.oauth.decideAuthorization({
      transactionId: preview.value.transaction_id,
      decision,
      ...(totpCode.value ? { totpCode: totpCode.value } : {}),
    })
    totpCode.value = ''
    success.value = result.status === 'approved' ? '授权成功，设备可以继续连接。' : '已拒绝该设备授权。'
  } catch (error) {
    totpCode.value = ''
    if (error instanceof ApiError && error.kind === 'authentication') {
      await router.replace({ path: '/login', query: { redirect: route.fullPath } })
      return
    }
    message.value = error instanceof ApiError && error.code === 'authorization_expired' ? '设备码无效或已过期。' : error instanceof ApiError ? error.message : '无法完成授权。'
  } finally { busy.value = false }
}

onMounted(() => {
  const code = typeof route.query.code === 'string' ? normalizeCode(route.query.code) : ''
  if (code) { userCode.value = code; void lookup() } else loading.value = false
})
</script>
