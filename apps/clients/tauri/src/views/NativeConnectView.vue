<template>
  <section class="login-view">
    <div class="auth-card">
      <div class="native-connect-heading">
        <div>
          <p class="eyebrow">{{ t('nativeConnect.eyebrow') }}</p>
          <h1>{{ t('nativeConnect.title') }}</h1>
        </div>
        <button
          data-action="toggle-locale"
          class="text-button locale-toggle-button"
          type="button"
          :aria-label="t('app.switchLangAria')"
          :title="t('app.switchLangAria')"
          @click="toggleLocale"
        >
          <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></svg>
          <span class="locale-badge">{{ t('app.switchLangBadge') }}</span>
        </button>
      </div>
      <form @submit.prevent="connect">
        <label for="server-url">{{ t('nativeConnect.serverUrl') }}</label><input id="server-url" v-model="issuer" type="url" inputmode="url" autocomplete="url" required />
        <p v-if="message" class="form-error" role="alert">{{ message }}</p>
        <div class="native-auth-options">
          <button
            class="primary-button"
            type="submit"
            data-action="browser-login"
            :title="t('nativeConnect.browserLoginTitle')"
            :disabled="busy"
          >{{ busy ? t('nativeConnect.waitingBrowser') : t('nativeConnect.browserLogin') }}</button>
          <button
            class="secondary-button"
            type="button"
            data-action="device-authorize"
            :title="t('nativeConnect.deviceAuthTitle')"
            :disabled="busy"
            @click="connectDevice"
          >{{ t('nativeConnect.deviceAuth') }}</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ApiError } from '@termflow/client-core'
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBottomToast, useClientRuntime, useI18n } from '@termflow/client-ui'
import { authorizeNativeClient, verifyNativeConnection } from '../nativeAuth'
import { prepareNativeServer } from '../serverPreparation'
import { canonicalAuthorizeEndpoint, serverConfig } from '../serverConfig'

const { t, locale, toggleLocale } = useI18n()
const runtime = useClientRuntime(); const router = useRouter(); const route = useRoute()
const toast = useBottomToast()
const issuer = ref(serverConfig.current); const errorCode = ref<string | null>(null); const busy = ref(false)

function extractErrorCode(error: unknown): string {
  return error instanceof ApiError
    ? error.kind
    : error instanceof Error
      ? error.message
      : typeof error === 'string' ? error : ''
}

function registrationErrorMessage(code: string): string {
  if (code === 'http_capability_denied') {
    return t('nativeConnect.error.http_capability_denied')
  }
  if (code === 'offline' || code === 'network_error') {
    return t('nativeConnect.error.offline')
  }
  if (code === 'issuer_mismatch') {
    return t('nativeConnect.error.issuer_mismatch')
  }
  if (code === 'authorization_cancelled' || code === 'aborted') {
    return t('nativeConnect.error.cancelled')
  }
  if (code === 'authorization_callback_timeout' || code === 'authorization_callback_invalid' || code === 'authorization_listener_missing') {
    return t('nativeConnect.error.callback')
  }
  if (code === 'https_required') {
    return t('nativeConnect.error.https_required')
  }
  if (code === 'authorization_verify_timeout') {
    return t('nativeConnect.error.verify_timeout')
  }
  return t('nativeConnect.error.default')
}

const message = computed(() => {
  void locale.value
  return errorCode.value === null ? '' : registrationErrorMessage(errorCode.value)
})

function prepareServer() {
  return prepareNativeServer(issuer.value, () => runtime.api.oauth.metadata())
}

async function connectDevice() {
  busy.value = true; errorCode.value = null
  try {
    await prepareServer()
    await router.push({ path: '/connect/device', query: route.query })
  } catch (error) { errorCode.value = extractErrorCode(error) }
  finally { busy.value = false }
}

async function connect() {
  busy.value = true; errorCode.value = null
  try {
    const prepared = await prepareServer()
    const authorizeEndpoint = canonicalAuthorizeEndpoint(prepared.issuer, prepared.metadata.authorization_endpoint)
    await authorizeNativeClient(prepared.issuer, authorizeEndpoint, prepared.metadata.scopes_supported)
    await verifyNativeConnection(runtime)
    toast.show({ text: t('nativeConnect.connectedToast'), tone: 'success' })
    const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') ? route.query.redirect : '/'
    await router.replace(target)
  } catch (error) { errorCode.value = extractErrorCode(error) }
  finally { busy.value = false }
}
</script>

<style scoped>
.native-connect-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.native-auth-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-4);
}

.native-auth-options > button {
  width: 100%;
}

@media (max-width: 35rem) {
  .native-auth-options {
    grid-template-columns: 1fr;
  }
}
</style>
