<template>
  <section class="login-view">
    <div class="auth-card">
      <p class="eyebrow">Native client</p><h1>连接 TermFlow Server</h1>
      <p>输入部署者提供的 B HTTPS 地址。管理员 Token 和验证码只会在系统浏览器中的 Web C 输入。</p>
      <form @submit.prevent="connect">
        <label for="server-url">B 地址</label><input id="server-url" v-model="issuer" type="url" inputmode="url" autocomplete="url" required />
        <p v-if="message" class="form-error" role="alert">{{ message }}</p>
        <button class="primary-button" type="submit" :disabled="busy">{{ busy ? '等待浏览器授权…' : '在系统浏览器中授权' }}</button>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useClientRuntime } from '@termflow/client-ui'
import { authorizeNativeClient } from '../nativeAuth'
import { canonicalAuthorizeEndpoint, canonicalIssuer, serverConfig } from '../serverConfig'

const runtime = useClientRuntime(); const router = useRouter(); const route = useRoute()
const issuer = ref(serverConfig.current); const message = ref(''); const busy = ref(false)
async function connect() {
  busy.value = true; message.value = ''
  try {
    const canonical = canonicalIssuer(issuer.value)
    await serverConfig.replace(canonical)
    const metadata = await runtime.api.oauth.metadata()
    if (metadata.issuer !== canonical) throw new Error('issuer_mismatch')
    const authorizeEndpoint = canonicalAuthorizeEndpoint(canonical, metadata.authorization_endpoint)
    await authorizeNativeClient(canonical, authorizeEndpoint, metadata.scopes_supported)
    const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') ? route.query.redirect : '/'
    await router.replace(target)
  } catch { message.value = '无法完成授权。请确认 B 地址、HTTPS 证书和浏览器中的登录步骤。' }
  finally { busy.value = false }
}
</script>
