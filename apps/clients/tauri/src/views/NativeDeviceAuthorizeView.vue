<template>
  <section class="login-view">
    <div class="auth-card device-auth-card">
      <header class="native-device-heading">
        <div><p class="eyebrow">Cross-device authorization</p><h1>在其他设备上授权</h1></div>
      </header>

      <template v-if="started">
        <div class="native-device-layout">
          <div class="native-device-details">
            <div class="device-code" aria-live="polite">
              <span class="form-hint">{{ codeLabel }}</span>
              <div class="device-code-value"><strong>{{ response?.user_code }}</strong><button class="icon-button" type="button" data-action="copy-device-code" aria-label="复制设备码" title="复制设备码" :disabled="busy" @click="copyCode"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 8V5.5A1.5 1.5 0 0 1 9.5 4h9A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H16M5.5 8h9A1.5 1.5 0 0 1 16 9.5v9A1.5 1.5 0 0 1 14.5 20h-9A1.5 1.5 0 0 1 4 18.5v-9A1.5 1.5 0 0 1 5.5 8Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg></button></div>
            </div>
            <div class="native-device-server">
              <span class="form-hint">服务器地址</span>
              <code>{{ issuer }}</code>
            </div>
            <div class="native-device-status">
              <p v-if="status" role="status" class="form-success">{{ status }}</p>
              <p v-if="message" role="alert" class="form-error">{{ message }}</p>
            </div>
          </div>
          <div class="native-device-qr">
            <ThemedQrCode v-if="response" :value="response.verification_uri_complete" alt="设备授权二维码" />
          </div>
        </div>
        <div class="native-device-actions">
          <button class="secondary-button" type="button" data-action="back-to-connect" :disabled="busy" @click="backToConnect">返回</button>
          <button class="primary-button" type="button" data-action="regenerate" :disabled="busy" @click="regenerate">重新生成</button>
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
import { ThemedQrCode, useBottomToast, useClientRuntime } from '@termflow/client-ui'
import { beginNativeDeviceAuthorization, verifyNativeConnection } from '../nativeAuth'
import { buildVersion } from '../buildVersion'
import { canonicalIssuer, serverConfig } from '../serverConfig'
import { pollDeviceAuthorization } from '../adapters/tauriAuthorization'

const runtime = useClientRuntime()
const toast = useBottomToast()
const route = useRoute()
const router = useRouter()
const issuer = ref(serverConfig.current)
const response = ref<Awaited<ReturnType<typeof beginNativeDeviceAuthorization>>['response']>()
const session = ref<Awaited<ReturnType<typeof beginNativeDeviceAuthorization>>['session']>()
const busy = ref(false)
const started = ref(false)
const message = ref('')
const status = ref('等待浏览器确认…')
const remaining = ref(0)
let timer: ReturnType<typeof setInterval> | undefined

const codeLabel = computed(() => remaining.value > 0 ? `设备码（${Math.ceil(remaining.value / 1000)} 秒）` : '设备码（已过期）')

function actionableMessage(error: unknown): string {
  const code = error instanceof ApiError ? error.kind : error instanceof Error ? error.message : typeof error === 'string' ? error : ''
  if (code === 'http_capability_denied') return '客户端网络权限配置无效。请升级或重新安装 TermFlow。'
  if (code === 'authorization_pending') return '浏览器尚未确认，请继续等待。'
  if (code === 'slow_down') return '请求过于频繁，已自动放慢轮询。'
  if (code === 'access_denied') return '浏览器拒绝了这次授权。请重新生成设备码。'
  if (code === 'expired_token') return '设备码已过期，请重新生成。'
  if (code === 'offline' || code === 'network_error') return '无法连接服务器。请检查网络后重试。'
  return '设备授权未完成。请重新生成设备码后重试。'
}

function stopTimer() { if (timer !== undefined) { clearInterval(timer); timer = undefined } }

async function start() {
  busy.value = true; message.value = ''
  try {
    const canonical = canonicalIssuer(issuer.value)
    await serverConfig.replace(canonical)
    const metadata = await runtime.api.oauth.metadata()
    if (metadata.issuer !== canonical) throw new Error('issuer_mismatch')
    const result = await beginNativeDeviceAuthorization({
      issuer: canonical,
      scopes: metadata.scopes_supported,
      client: { name: 'TermFlow', platform: `${platform()} ${arch()}`, version: buildVersion },
      create: (input) => runtime.api.oauth.createDeviceAuthorization(input),
      poll: async (input, signal) => {
        try { return await pollDeviceAuthorization({ issuer: canonical, ...input }, signal) }
        catch (error) {
          const code = error instanceof ApiError ? error.code : error instanceof Error ? error.message : typeof error === 'string' ? error : ''
          if (code === 'authorization_pending') status.value = '等待浏览器确认…'
          else if (code === 'slow_down') status.value = '服务器较忙，已放慢轮询…'
          throw error
        }
      },
    })
    response.value = result.response; session.value = result.session; started.value = true
    remaining.value = result.response.expires_in * 1000
    stopTimer(); timer = setInterval(() => {
      remaining.value = Math.max(0, remaining.value - 1000)
      if (remaining.value === 0) { result.session.cancel(); status.value = ''; message.value = '设备码已过期，请重新生成。'; stopTimer() }
    }, 1000)
    busy.value = false
    void result.session.authorize().then(async () => {
      await verifyNativeConnection(runtime)
      status.value = '授权成功，正在打开工作区…'
      stopTimer()
      toast.show({ text: '已连接', tone: 'success' })
      const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') ? route.query.redirect : '/'
      return router.replace(target)
    }).catch((error) => { if ((error as Error)?.name !== 'AbortError') { message.value = actionableMessage(error); status.value = '' } })
  } catch (error) { message.value = actionableMessage(error); busy.value = false }
}

async function copyCode() {
  if (response.value) {
    await runtime.clipboard.writeText(response.value.user_code)
    status.value = '设备码已复制。'
  }
}

function cancel() {
  session.value?.cancel(); stopTimer(); started.value = false; response.value = undefined; session.value = undefined; status.value = '已取消'; message.value = ''
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
.native-device-layout { width: min(100%, 48rem); margin-inline: auto; display: grid; grid-template-columns: minmax(15rem, 18rem) minmax(0, 1fr); align-items: stretch; gap: clamp(var(--space-5), 5vw, 4rem); margin-block-start: var(--space-5); border: 1px solid var(--color-online); border-radius: var(--radius-lg); padding: clamp(var(--space-3), 3vw, var(--space-5)); background: var(--color-panel); }
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
.native-device-actions { display: flex; justify-content: center; gap: var(--space-3); width: min(100%, 48rem); margin-inline: auto; margin-block-start: var(--space-5); }
@media (max-width: 42rem) {
  .device-auth-card { width: 100%; }
  .native-device-layout { width: 100%; grid-template-columns: 1fr; gap: var(--space-5); }
  .native-device-qr { order: -1; }
  .native-device-heading { align-items: flex-start; }
}
</style>
