<template>
  <section class="authorize-view" aria-labelledby="device-authorize-title">
    <div class="auth-card consent-card device-consent-card">
      <header class="authorize-card-heading">
        <div><p class="eyebrow">{{ t('deviceAuth.eyebrow') }}</p><h1 id="device-authorize-title">{{ t('deviceAuth.title') }}</h1></div>
        <div class="authorize-card-actions">
          <button
            data-action="toggle-locale"
            class="text-button locale-toggle-button"
            type="button"
            :aria-label="t('app.switchLangAria')"
            :title="t('app.switchLangAria')"
            @click="toggleLocale"
          >
            <Globe :size="15" aria-hidden="true" />
            <span class="locale-badge">{{ t('app.switchLangBadge') }}</span>
          </button>
          <RouterLink data-action="back-to-login" class="secondary-button" to="/login">{{ t('deviceAuth.backToLogin') }}</RouterLink>
        </div>
      </header>
      <template v-if="preview === null && !loading">
        <form data-action="lookup-device" class="security-form device-code-lookup" @submit.prevent="lookup()">
          <label for="device-user-code">{{ t('deviceAuth.codeLabel') }}</label>
          <input id="device-user-code" v-model="userCode" inputmode="text" autocomplete="off" placeholder="ABCD-EFGH" required />
          <p v-if="message" role="alert" class="form-error">{{ message }}</p>
          <button class="primary-button" type="submit" :disabled="busy">{{ t('deviceAuth.lookupBtn') }}</button>
        </form>
      </template>
      <p v-else-if="loading">{{ t('deviceAuth.loading') }}</p>
      <template v-else-if="preview">
        <div class="device-authorize-layout">
          <div class="device-authorize-qr">
            <ThemedQrCode :value="verificationUrl" :alt="t('deviceAuth.qrAlt')" />
            <span class="form-hint">{{ t('deviceAuth.codeHint') }} <code>{{ userCode }}</code></span>
          </div>
          <div class="device-authorize-status">
            <dl class="consent-details">
              <div><dt>{{ t('settings.deviceApproval.client') }}</dt><dd>{{ preview.client_name }}</dd></div>
              <div><dt>{{ t('settings.deviceApproval.platform') }}</dt><dd>{{ preview.platform }}{{ preview.client_version ? ` · ${preview.client_version}` : '' }}</dd></div>
              <div><dt>{{ t('settings.deviceApproval.scopes') }}</dt><dd>{{ preview.scopes.join(' · ') }}</dd></div>
              <div><dt>{{ t('settings.deviceApproval.expiresAt') }}</dt><dd>{{ formatExpiry(preview.expires_at) }}<span class="status-pill">{{ statusLabel }}</span></dd></div>
            </dl>
            <form class="security-form" @submit.prevent="decide('allow')">
              <template v-if="preview.totp_required">
                <label for="device-authorize-totp">{{ t('settings.totp.code') }}<span class="totp-hint">{{ t('deviceAuth.totpHint') }}</span></label>
                <input id="device-authorize-totp" v-model="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
              </template>
              <p v-if="message" role="alert" class="form-error">{{ message }}</p>
          <div class="dialog-actions"><button class="secondary-button" type="button" :disabled="busy" @click="decide('deny')">{{ t('settings.deviceApproval.deny') }}</button><button class="primary-button" type="submit" :disabled="busy">{{ t('settings.deviceApproval.allow') }}</button></div>
            </form>
          </div>
        </div>
      </template>
      <p v-if="successLabel" role="status" class="form-success">{{ successLabel }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Globe } from '@lucide/vue'
import { computed, onMounted } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import ThemedQrCode from '../components/common/ThemedQrCode.vue'
import { useDeviceAuthorizationApproval } from '../composables/useDeviceAuthorizationApproval'
import { useI18n } from '../i18n'
import { useClientRuntime } from '../runtime'

const { t, toggleLocale } = useI18n()
const runtime = useClientRuntime()
const route = useRoute()
const router = useRouter()
const { preview, loading, busy, userCode, totpCode, error: message, success, lookup, decide } = useDeviceAuthorizationApproval(
  runtime.api.oauth,
  { onAuthenticationRequired: async () => { await router.replace({ path: '/login', query: { redirect: route.fullPath } }) } },
)

const statusLabel = computed(() => new Date(preview.value?.expires_at ?? 0).getTime() > Date.now() ? t('deviceAuth.statusPending') : t('deviceAuth.statusExpired'))
const successLabel = computed(() => success.value === 'approved' ? t('deviceAuth.approvedMsg') : success.value === 'denied' ? t('deviceAuth.deniedMsg') : '')
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
.authorize-card-actions { display: flex; align-items: center; gap: var(--space-2); }
.totp-hint { color: var(--color-text-muted); font-weight: 400; font-size: 0.85em; }
</style>
