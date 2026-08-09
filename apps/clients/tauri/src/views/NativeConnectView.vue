<template>
  <section class="login-view">
    <div class="auth-card">
      <p class="eyebrow">Connect to Server</p><h1>连接到服务器</h1>
      <form @submit.prevent="connect">
        <label for="server-url">服务器地址</label><input id="server-url" v-model="issuer" type="url" inputmode="url" autocomplete="url" required />
        <p v-if="message" class="form-error" role="alert">{{ message }}</p>
        <div class="native-auth-options">
          <button
            class="primary-button"
            type="submit"
            data-action="browser-login"
            title="通过本机系统浏览器登录并审批此客户端。"
            :disabled="busy"
          >{{ busy ? '等待浏览器审批' : '本机浏览器登录' }}</button>
          <button
            class="secondary-button"
            type="button"
            data-action="device-authorize"
            title="使用已登录的其他设备完成授权，不会打开本机浏览器。"
            :disabled="busy"
            @click="router.push({ path: '/connect/device', query: route.query })"
          >其他设备授权</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ApiError } from '@termflow/client-core'
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBottomToast, useClientRuntime } from '@termflow/client-ui'
import { authorizeNativeClient, verifyNativeConnection } from '../nativeAuth'
import { canonicalAuthorizeEndpoint, canonicalIssuer, serverConfig } from '../serverConfig'

const runtime = useClientRuntime(); const router = useRouter(); const route = useRoute()
const toast = useBottomToast()
const issuer = ref(serverConfig.current); const message = ref(''); const busy = ref(false)

function registrationErrorMessage(error: unknown): string {
  const code = error instanceof ApiError
    ? error.kind
    : error instanceof Error
      ? error.message
      : typeof error === 'string' ? error : ''
  if (code === 'http_capability_denied') {
    return '客户端网络权限配置无效。请升级或重新安装 TermFlow。'
  }
  if (code === 'offline') {
    return '无法连接服务器。请检查服务器地址、网络连接和本机服务是否正在运行。'
  }
  if (code === 'authorization_cancelled' || code === 'aborted') {
    return '注册申请已取消。请重新申请，并在系统浏览器中完成审批。'
  }
  if (code === 'authorization_callback_timeout' || code === 'authorization_callback_invalid' || code === 'authorization_listener_missing') {
    return '未收到有效的 TermFlow 回调。请确认系统允许 termflow:// 链接打开本应用，然后重新申请。'
  }
  if (code === 'https_required') {
    return '远程服务器必须使用 HTTPS；只有本机服务器可以使用 HTTP。'
  }
  return '无法完成远程控制注册。请检查服务器地址和系统浏览器中的审批状态后重试。'
}

async function connect() {
  busy.value = true; message.value = ''
  try {
    const canonical = canonicalIssuer(issuer.value)
    await serverConfig.replace(canonical)
    const metadata = await runtime.api.oauth.metadata()
    if (metadata.issuer !== canonical) throw new Error('issuer_mismatch')
    const authorizeEndpoint = canonicalAuthorizeEndpoint(canonical, metadata.authorization_endpoint)
    await authorizeNativeClient(canonical, authorizeEndpoint, metadata.scopes_supported)
    await verifyNativeConnection(runtime)
    toast.show({ text: '已连接', tone: 'success' })
    const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') ? route.query.redirect : '/'
    await router.replace(target)
  } catch (error) { message.value = registrationErrorMessage(error) }
  finally { busy.value = false }
}
</script>

<style scoped>
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
