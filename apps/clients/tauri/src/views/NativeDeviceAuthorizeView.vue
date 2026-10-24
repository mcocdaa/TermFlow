<template>
  <section class="login-view">
    <div class="auth-card device-auth-card">
      <header class="native-device-heading">
        <div><p class="eyebrow">{{ t('deviceAuth.eyebrow') }}</p><h1>{{ t('nativeDevice.title') }}</h1></div>
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
      </header>

      <template v-if="started">
        <div class="native-device-layout">
          <div class="native-device-details">
            <div class="device-code" aria-live="polite">
              <span class="form-hint">{{ codeLabel }}</span>
              <div class="device-code-value"><strong>{{ response?.user_code }}</strong><button class="icon-button" type="button" data-action="copy-device-code" :aria-label="t('nativeDevice.copyCode')" :title="t('nativeDevice.copyCode')" :disabled="busy" @click="copyCode"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 8V5.5A1.5 1.5 0 0 1 9.5 4h9A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H16M5.5 8h9A1.5 1.5 0 0 1 16 9.5v9A1.5 1.5 0 0 1 14.5 20h-9A1.5 1.5 0 0 1 4 18.5v-9A1.5 1.5 0 0 1 5.5 8Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg></button></div>
            </div>
            <div class="native-device-server">
              <span class="form-hint">{{ t('nativeDevice.serverUrl') }}</span>
              <code>{{ issuer }}</code>
            </div>
            <div class="native-device-status">
              <p v-if="status" role="status" class="form-success">{{ status }}</p>
              <p v-if="message" role="alert" class="form-error">{{ message }}</p>
            </div>
          </div>
          <div class="native-device-qr">
            <ThemedQrCode v-if="response" :value="response.verification_uri_complete" :alt="t('deviceAuth.qrAlt')" />
          </div>
        </div>
        <div class="native-device-actions">
          <button class="secondary-button" type="button" data-action="back-to-connect" :disabled="busy" @click="backToConnect">{{ t('nativeDevice.back') }}</button>
          <button class="primary-button" type="button" data-action="regenerate" :disabled="busy" @click="regenerate">{{ t('nativeDevice.regenerate') }}</button>
        </div>
      </template>
      <p v-else-if="message" class="form-error" role="alert">{{ message }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ApiError } from '@termflow/client-core'
import { arch, platform } from '@tauri-apps/plugin-os'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ThemedQrCode, useBottomToast, useClientRuntime, useI18n } from '@termflow/client-ui'
import { beginNativeDeviceAuthorization, verifyNativeConnection } from '../nativeAuth'
import { buildVersion } from '../buildVersion'
import { prepareNativeServer } from '../serverPreparation'
import { serverConfig } from '../serverConfig'
import { pollDeviceAuthorization } from '../adapters/tauriAuthorization'

const { t, locale, toggleLocale } = useI18n()
const runtime = useClientRuntime()
const toast = useBottomToast()
const route = useRoute()
const router = useRouter()
const issuer = ref(serverConfig.current)
const response = ref<Awaited<ReturnType<typeof beginNativeDeviceAuthorization>>['response']>()
const session = ref<Awaited<ReturnType<typeof beginNativeDeviceAuthorization>>['session']>()
const busy = ref(false)
const started = ref(false)
const errorCode = ref<string | null>(null)
const statusKey = ref<string | null>('nativeDevice.status.waiting')
const remaining = ref(0)
let timer: ReturnType<typeof setInterval> | undefined

const codeLabel = computed(() => {
  void locale.value
  return remaining.value > 0
    ? t('nativeDevice.codeCountdown', { seconds: Math.ceil(remaining.value / 1000) })
    : t('nativeDevice.codeExpired')
})

function extractErrorCode(error: unknown): string {
  return error instanceof ApiError ? error.kind : error instanceof Error ? error.message : typeof error === 'string' ? error : ''
}

function actionableMessage(code: string): string {
  if (code === 'http_capability_denied') return t('nativeDevice.error.http_capability_denied')
  if (code === 'authorization_pending') return t('nativeDevice.error.authorization_pending')
  if (code === 'slow_down') return t('nativeDevice.error.slow_down')
  if (code === 'access_denied') return t('nativeDevice.error.access_denied')
  if (code === 'expired_token') return t('nativeDevice.error.expired_token')
  if (code === 'offline' || code === 'network_error') return t('nativeDevice.error.offline')
  if (code === 'issuer_mismatch') return t('nativeDevice.error.issuer_mismatch')
  if (code === 'authorization_verify_timeout') return t('nativeDevice.error.authorization_verify_timeout')
  return t('nativeDevice.error.default')
}

const status = computed(() => {
  void locale.value
  return statusKey.value === null ? '' : t(statusKey.value)
})

const message = computed(() => {
  void locale.value
  return errorCode.value === null ? '' : actionableMessage(errorCode.value)
})

function stopTimer() { if (timer !== undefined) { clearInterval(timer); timer = undefined } }

async function start() {
  busy.value = true; errorCode.value = null
  try {
    const prepared = await prepareNativeServer(serverConfig.current, () => runtime.api.oauth.metadata())
    issuer.value = prepared.issuer
    const result = await beginNativeDeviceAuthorization({
      issuer: prepared.issuer,
      scopes: prepared.metadata.scopes_supported,
      client: { name: 'TermFlow', platform: `${platform()} ${arch()}`, version: buildVersion },
      create: (input) => runtime.api.oauth.createDeviceAuthorization(input),
      poll: async (input, signal) => {
        try { return await pollDeviceAuthorization({ issuer: prepared.issuer, ...input }, signal) }
        catch (error) {
          const code = error instanceof ApiError ? error.code : error instanceof Error ? error.message : typeof error === 'string' ? error : ''
          if (code === 'authorization_pending') statusKey.value = 'nativeDevice.status.waiting'
          else if (code === 'slow_down') statusKey.value = 'nativeDevice.status.slowDown'
          throw error
        }
      },
    })
    response.value = result.response; session.value = result.session; started.value = true
    remaining.value = result.response.expires_in * 1000
    stopTimer(); timer = setInterval(() => {
      remaining.value = Math.max(0, remaining.value - 1000)
      if (remaining.value === 0) { result.session.cancel(); statusKey.value = null; errorCode.value = 'expired_token'; stopTimer() }
    }, 1000)
    busy.value = false
    void result.session.authorize().then(async () => {
      await verifyNativeConnection(runtime)
      statusKey.value = 'nativeDevice.status.opening'
      stopTimer()
      toast.show({ text: t('nativeConnect.connectedToast'), tone: 'success' })
      const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') ? route.query.redirect : '/'
      return router.replace(target)
    }).catch((error) => { if ((error as Error)?.name !== 'AbortError') { errorCode.value = extractErrorCode(error); statusKey.value = null } })
  } catch (error) { errorCode.value = extractErrorCode(error); busy.value = false }
}

async function copyCode() {
  if (response.value) {
    await runtime.clipboard.writeText(response.value.user_code)
    statusKey.value = 'nativeDevice.status.copied'
  }
}

function cancel() {
  session.value?.cancel(); stopTimer(); started.value = false; response.value = undefined; session.value = undefined; statusKey.value = 'nativeDevice.status.cancelled'; errorCode.value = null
}

async function backToConnect() {
  cancel()
  await router.replace({ path: '/connect', query: route.query })
}

async function regenerate() { session.value?.cancel(); stopTimer(); started.value = false; response.value = undefined; session.value = undefined; await start() }

onMounted(() => { void start() })
onBeforeUnmount(() => { session.value?.cancel(); stopTimer() })
</script>

<style scoped>
.device-auth-card { width: min(100%, 56rem); }
.native-device-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
.native-device-heading h1 { margin-block-end: 0; }
.device-start-form { display: grid; gap: var(--space-3); margin-block-start: var(--space-5); }
.native-device-layout { width: min(100%, 48rem); margin-inline: auto; display: grid; grid-template-columns: minmax(15rem, 18rem) minmax(0, 1fr); align-items: stretch; gap: clamp(var(--space-5), 5vw, 4rem); margin-block-start: 2rem; }
.native-device-qr { display: grid; justify-items: center; align-content: center; gap: var(--space-3); }
.native-device-qr .themed-qr-code { width: min(100%, 18rem); }
.native-device-details { display: flex; flex-direction: column; justify-content: center; gap: var(--space-5); min-width: 0; }
.native-device-server { display: grid; gap: var(--space-2); min-width: 0; }
.native-device-server code { overflow-wrap: anywhere; padding: var(--space-3); border: 1px solid var(--color-border); border-radius: var(--radius-md); background: var(--color-terminal); color: var(--color-terminal-foreground); font-family: var(--font-mono); }
.device-code { display: grid; gap: var(--space-2); }
.device-code-value { display: flex; align-items: center; gap: var(--space-3); min-width: 0; }
.device-code-value strong { font-family: var(--font-mono); font-size: clamp(1.4rem, 3vw, 2rem); letter-spacing: .08em; overflow-wrap: anywhere; }
.icon-button { display: inline-grid; flex: 0 0 auto; place-items: center; width: 2.5rem; height: 2.5rem; padding: .5rem; border: 1px solid var(--color-border); border-radius: var(--radius-md); background: var(--color-panel-raised); color: var(--color-text); cursor: pointer; }
.icon-button svg { width: 1.25rem; height: 1.25rem; }
.icon-button:disabled { cursor: not-allowed; opacity: .55; }
.native-device-status { display: grid; gap: var(--space-2); min-width: 0; }
.native-device-status p { margin: 0; }
.native-device-actions { display: flex; justify-content: center; gap: var(--space-3); width: min(100%, 48rem); margin-inline: auto; margin-block-start: 2rem; }
@media (max-width: 42rem) {
  .device-auth-card { width: 100%; }
  .native-device-layout { width: 100%; grid-template-columns: 1fr; gap: var(--space-5); }
  .native-device-qr { order: -1; }
  .native-device-heading { align-items: flex-start; }
}
</style>
