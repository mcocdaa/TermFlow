<template>
  <section class="login-view">
    <div class="auth-card">
      <div class="login-card-heading">
        <h1>{{ t('login.title') }}</h1>
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
      </div>
      <form @submit.prevent="submit">
        <template v-if="challengeId === null">
          <label for="admin-token">{{ t('login.adminToken') }}</label>
          <input id="admin-token" v-model="adminToken" type="password" autocomplete="off" required autofocus />
        </template>
        <template v-else>
          <label for="totp-code">{{ t('login.totpCode') }}<span class="totp-hint">{{ t('login.totpHint') }}</span></label>
          <input id="totp-code" ref="totpInput" v-model="totpCode" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
        </template>
        <p v-if="message" role="alert" class="form-error">{{ message }}</p>
        <div class="dialog-actions"><button class="primary-button" type="submit" :disabled="busy">{{ busy ? t('settings.totp.verifying') : challengeId === null ? t('login.submit') : t('login.verifyAndLogin') }}</button></div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Globe } from '@lucide/vue'
import { computed, nextTick, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, safeMessageFor, type ApiErrorKind } from '@termflow/client-core'
import { useSession } from '../composables/useSession'
import { useI18n } from '../i18n'

const { t, toggleLocale, locale } = useI18n()
const adminToken = ref('')
const totpCode = ref('')
const challengeId = ref<string | null>(null)
const totpInput = ref<HTMLInputElement | null>(null)
const busy = ref(false)
const errorState = ref<{ type: 'api'; kind: ApiErrorKind } | { type: 'key'; key: string } | null>(null)
const message = computed(() => {
  void locale.value
  if (errorState.value === null) return ''
  return errorState.value.type === 'api' ? safeMessageFor(errorState.value.kind) : t(errorState.value.key)
})
const route = useRoute()
const router = useRouter()
const { loginWithToken, completeTotp } = useSession()

async function submit() {
  if (busy.value || (challengeId.value === null ? !adminToken.value : !/^[0-9]{6}$/.test(totpCode.value))) return
  busy.value = true
  errorState.value = null
  try {
    if (challengeId.value === null) {
      const result = await loginWithToken(adminToken.value)
      adminToken.value = ''
      if ('status' in result) {
        challengeId.value = result.challenge_id
        await nextTick()
        totpInput.value?.focus()
        return
      }
    } else {
      await completeTotp(totpCode.value)
      totpCode.value = ''
    }
    const requested = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(requested.startsWith('/') && !requested.startsWith('//') ? requested : '/')
  } catch (error) {
    errorState.value = error instanceof ApiError ? { type: 'api', kind: error.kind } : { type: 'key', key: 'login.failed' }
  } finally {
    adminToken.value = ''
    totpCode.value = ''
    busy.value = false
  }
}
</script>

<style scoped>
.login-card-heading { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.totp-hint { color: var(--color-text-muted); font-weight: 400; font-size: 0.85em; }
</style>
