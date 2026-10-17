<template>
  <section class="authorize-view">
    <div class="auth-card consent-card">
      <header class="authorize-card-heading">
        <div><p class="eyebrow">{{ t('nativeAuth.eyebrow') }}</p><h1>{{ t('nativeAuth.title') }}</h1></div>
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
      </header>
      <p v-if="loading">{{ t('nativeAuth.loading') }}</p>
      <template v-else-if="preview">
        <dl class="consent-details">
          <div><dt>{{ t('nativeAuth.server') }}</dt><dd>{{ preview.issuer }}</dd></div>
          <div><dt>{{ t('settings.deviceApproval.client') }}</dt><dd>{{ preview.client_name }}</dd></div>
          <div><dt>{{ t('settings.deviceApproval.platform') }}</dt><dd>{{ preview.platform }}{{ preview.client_version ? ` · ${preview.client_version}` : '' }}</dd></div>
          <div><dt>{{ t('nativeAuth.keyFingerprint') }}</dt><dd><code>{{ preview.key_fingerprint }}</code></dd></div>
          <div><dt>{{ t('settings.deviceApproval.scopes') }}</dt><dd>{{ preview.scopes.join(' · ') }}</dd></div>
        </dl>
        <form class="security-form native-authorize-form" @submit.prevent="decide('allow')">
          <label for="authorize-admin-token">{{ t('settings.totp.adminToken') }}</label>
          <input id="authorize-admin-token" v-model="adminToken" type="password" autocomplete="off" required />
          <template v-if="preview.totp_required">
            <label for="authorize-totp">{{ t('settings.totp.code') }}<span class="totp-hint">{{ t('deviceAuth.totpHint') }}</span></label>
            <input id="authorize-totp" v-model="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
          </template>
          <p v-if="message" role="alert" class="form-error">{{ message }}</p>
          <div class="dialog-actions"><button class="secondary-button" type="button" @click="decide('deny')">{{ t('settings.deviceApproval.deny') }}</button><button class="primary-button" type="submit">{{ t('nativeAuth.allow') }}</button></div>
        </form>
      </template>
      <p v-else role="alert" class="form-error">{{ message || t('nativeAuth.invalidOrExpired') }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Globe } from '@lucide/vue'
import type { OAuthAuthorizationPreviewResponse } from '@termflow/client-contracts'
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError } from '@termflow/client-core'
import { useI18n } from '../i18n'
import { useClientRuntime } from '../runtime'

const { t, toggleLocale } = useI18n()
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
  catch (error) { message.value = error instanceof ApiError ? error.message : t('nativeAuth.invalidOrExpired') }
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
    message.value = error instanceof ApiError ? error.message : t('nativeAuth.failed')
  }
}
</script>

<style scoped>
.totp-hint { color: var(--color-text-muted); font-weight: 400; font-size: 0.85em; }
</style>
