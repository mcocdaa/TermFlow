<template>
  <section class="login-view">
    <div class="auth-card device-auth-card">
      <p class="eyebrow">Cross-device authorization</p>
      <h1>在其他设备上授权</h1>
      <p class="form-hint">在已登录的浏览器中确认这台设备，然后返回这里完成连接。</p>

      <template v-if="!started">
        <label for="device-server-url">服务器地址</label>
        <input id="device-server-url" v-model="issuer" type="url" inputmode="url" autocomplete="url" required />
        <p v-if="message" class="form-error" role="alert">{{ message }}</p>
        <button class="primary-button" type="button" :disabled="busy" @click="start">{{ busy ? '正在生成设备码…' : '生成设备授权码' }}</button>
      </template>

      <template v-else>
        <div class="device-code" aria-live="polite">
          <span class="form-hint">设备码</span>
          <strong>{{ response?.user_code }}</strong>
          <span class="form-hint">{{ expiryLabel }}</span>
        </div>
        <ThemedQrCode v-if="response" :value="response.verification_uri_complete" alt="设备授权二维码" />
        <p class="form-hint">验证地址：<code>{{ response?.verification_uri }}</code></p>
        <p v-if="status" role="status" class="form-success">{{ status }}</p>
        <p v-if="message" role="alert" class="form-error">{{ message }}</p>
        <div class="dialog-actions">
          <button class="secondary-button" type="button" :disabled="busy" @click="copyCode">复制设备码</button>
          <button class="secondary-button" type="button" :disabled="busy" @click="cancel">取消</button>
          <button class="primary-button" type="button" :disabled="busy" @click="regenerate">重新生成</button>
        </div>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ApiError } from '@termflow/client-core'
import { arch, platform } from '@tauri-apps/plugin-os'
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ThemedQrCode, useClientRuntime } from '@termflow/client-ui'
import { beginNativeDeviceAuthorization } from '../nativeAuth'
import { buildVersion } from '../buildVersion'
import { canonicalIssuer, serverConfig } from '../serverConfig'
import { pollDeviceAuthorization } from '../adapters/tauriAuthorization'

const runtime = useClientRuntime()
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

const expiryLabel = computed(() => remaining.value > 0 ? `有效期剩余 ${Math.ceil(remaining.value / 1000)} 秒` : '设备码已过期')

function actionableMessage(error: unknown): string {
  const code = error instanceof ApiError ? error.code : error instanceof Error ? error.message : typeof error === 'string' ? error : ''
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
    void result.session.authorize().then(() => {
      status.value = '授权成功，正在打开工作区…'
      stopTimer()
      const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') ? route.query.redirect : '/'
      return router.replace(target)
    }).catch((error) => { if ((error as Error)?.name !== 'AbortError') { message.value = actionableMessage(error); status.value = '' } })
  } catch (error) { message.value = actionableMessage(error); busy.value = false }
}

async function copyCode() {
  if (response.value) await runtime.clipboard.writeText(response.value.user_code)
}

function cancel() {
  session.value?.cancel(); stopTimer(); started.value = false; response.value = undefined; session.value = undefined; status.value = '已取消'; message.value = ''
}

async function regenerate() { session.value?.cancel(); stopTimer(); started.value = false; response.value = undefined; session.value = undefined; await start() }

onBeforeUnmount(() => { session.value?.cancel(); stopTimer() })
</script>
