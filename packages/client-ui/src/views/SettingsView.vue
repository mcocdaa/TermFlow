<template>
  <div class="page settings-page">
    <header class="page-heading"><div><p class="eyebrow">Preferences & Security</p><h1>设置</h1><p>主题在客户端本地保存；认证和客户端授权由当前 B 管理。</p></div></header>
    <section class="settings-panel" aria-labelledby="appearance-heading"><div class="settings-panel-heading"><div><p class="eyebrow">Appearance</p><h2 id="appearance-heading">界面主题</h2></div></div><ThemePicker /></section>
    <ServerConnectionPanel :issuer="issuer" />
    <template v-if="runtime.capabilities.manageSecurity">
      <TotpPanel @changed="totpEnabled = $event.enabled" />
      <AuthorizedClientsPanel v-if="runtime.capabilities.manageAuthorizedClients" :totp-enabled="totpEnabled" />
    </template>
    <section v-else class="settings-panel"><h2>设备连接</h2><p>安全设置只能从已认证 Web C 管理；本机只保存自己的设备凭据。</p></section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useClientRuntime } from '../runtime'
import AuthorizedClientsPanel from '../components/settings/AuthorizedClientsPanel.vue'
import ServerConnectionPanel from '../components/settings/ServerConnectionPanel.vue'
import ThemePicker from '../components/settings/ThemePicker.vue'
import TotpPanel from '../components/settings/TotpPanel.vue'

const runtime = useClientRuntime()
const issuer = ref(runtime.canonicalServerUrl)
const totpEnabled = ref(false)
onMounted(async () => { issuer.value = (await runtime.api.oauth.metadata()).issuer })
</script>
