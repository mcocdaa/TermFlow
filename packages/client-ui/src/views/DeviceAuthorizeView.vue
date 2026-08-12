<template>
  <section class="authorize-view" aria-labelledby="device-authorize-title">
    <div class="auth-card consent-card device-consent-card">
      <header class="authorize-card-heading">
        <div><p class="eyebrow">Cross-device authorization</p><h1 id="device-authorize-title">授权新设备</h1></div>
        <RouterLink data-action="back-to-login" class="secondary-button" to="/login">返回登录</RouterLink>
      </header>
      <template v-if="preview === null && !loading">
        <form data-action="lookup-device" class="security-form device-code-lookup" @submit.prevent="lookup()">
          <label for="device-user-code">设备码</label>
          <input id="device-user-code" v-model="userCode" inputmode="text" autocomplete="off" placeholder="ABCD-EFGH" required />
          <p v-if="message" role="alert" class="form-error">{{ message }}</p>
          <button class="primary-button" type="submit" :disabled="busy">查找设备</button>
        </form>
      </template>
      <p v-else-if="loading">正在读取设备授权…</p>
      <template v-else-if="preview">
        <div class="device-authorize-layout">
          <div class="device-authorize-qr">
            <ThemedQrCode :value="verificationUrl" alt="设备授权二维码" />
            <span class="form-hint">设备码 <code>{{ userCode }}</code></span>
          </div>
          <div class="device-authorize-status">
            <dl class="consent-details">
              <div><dt>客户端</dt><dd>{{ preview.client_name }}</dd></div>
              <div><dt>平台</dt><dd>{{ preview.platform }}{{ preview.client_version ? ` · ${preview.client_version}` : '' }}</dd></div>
              <div><dt>权限</dt><dd>{{ preview.scopes.join(' · ') }}</dd></div>
              <div><dt>有效期至</dt><dd>{{ formatExpiry(preview.expires_at) }}<span class="status-pill">{{ statusLabel }}</span></dd></div>
            </dl>
            <form class="security-form" @submit.prevent="decide('allow')">
              <template v-if="preview.totp_required">
                <label for="device-authorize-totp">双重验证码<span class="totp-hint">（请输入验证器应用当前显示的 6 位双重验证码。）</span></label>
                <input id="device-authorize-totp" v-model="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
              </template>
              <p v-if="message" role="alert" class="form-error">{{ message }}</p>
          <div class="dialog-actions"><button class="secondary-button" type="button" :disabled="busy" @click="decide('deny')">拒绝</button><button class="primary-button" type="submit" :disabled="busy">允许此设备</button></div>
            </form>
          </div>
        </div>
      </template>
      <p v-if="successLabel" role="status" class="form-success">{{ successLabel }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import ThemedQrCode from '../components/common/ThemedQrCode.vue'
import { useDeviceAuthorizationApproval } from '../composables/useDeviceAuthorizationApproval'
import { useClientRuntime } from '../runtime'

const runtime = useClientRuntime()
const route = useRoute()
const router = useRouter()
const { preview, loading, busy, userCode, totpCode, error: message, success, lookup, decide } = useDeviceAuthorizationApproval(
  runtime.api.oauth,
  { onAuthenticationRequired: async () => { await router.replace({ path: '/login', query: { redirect: route.fullPath } }) } },
)

const statusLabel = computed(() => new Date(preview.value?.expires_at ?? 0).getTime() > Date.now() ? '待确认' : '已过期')
const successLabel = computed(() => success.value === 'approved' ? '授权成功，设备可以继续连接。' : success.value === 'denied' ? '已拒绝该设备授权。' : '')
const verificationUrl = computed(() => {
  if (preview.value === null || !userCode.value) return ''
  try {
    const url = new URL('/device', preview.value.issuer)
    url.searchParams.set('code', userCode.value)
    return url.toString()
  } catch {
    return `${preview.value.issuer.replace(/\/$/, '')}/device?code=${encodeURIComponent(userCode.value)}`
  }
})

function formatExpiry(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

onMounted(() => {
  const code = typeof route.query.code === 'string' ? route.query.code : ''
  if (code) void lookup(code)
  else loading.value = false
})

</script>

<style scoped>
.totp-hint { color: var(--color-text-muted); font-weight: 400; font-size: 0.85em; }
</style>
