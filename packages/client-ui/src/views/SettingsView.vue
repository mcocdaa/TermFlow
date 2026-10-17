<template>
  <div class="page settings-page">
    <header class="page-heading"><div><p class="eyebrow">{{ t('settings.eyebrow') }}</p><h1>{{ t('settings.title') }}</h1></div></header>
    <section class="settings-panel" aria-labelledby="appearance-heading"><div class="settings-panel-heading"><div><p class="eyebrow">{{ t('settings.appearance.eyebrow') }}</p><h2 id="appearance-heading">{{ t('settings.appearance.heading') }}</h2></div></div><ThemePicker /></section>
    <ServerConnectionPanel :issuer="issuer" />
    <template v-if="runtime.capabilities.manageSecurity">
      <TotpPanel @changed="totpEnabled = $event.enabled" />
      <AuthorizedClientsPanel v-if="runtime.capabilities.manageAuthorizedClients" :totp-enabled="totpEnabled" />
    </template>
    <section v-else class="settings-panel" aria-labelledby="device-connection-heading"><div class="settings-panel-heading"><div><p class="eyebrow">{{ t('settings.deviceConnection.eyebrow') }}</p><h2 id="device-connection-heading">{{ t('settings.deviceConnection.heading') }}</h2></div></div><p>{{ t('settings.deviceConnection.note') }}</p></section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from '../i18n'
import { useClientRuntime } from '../runtime'
import AuthorizedClientsPanel from '../components/settings/AuthorizedClientsPanel.vue'
import ServerConnectionPanel from '../components/settings/ServerConnectionPanel.vue'
import ThemePicker from '../components/settings/ThemePicker.vue'
import TotpPanel from '../components/settings/TotpPanel.vue'

const { t } = useI18n()
const runtime = useClientRuntime()
const issuer = ref(runtime.canonicalServerUrl)
const totpEnabled = ref(false)
onMounted(async () => { issuer.value = (await runtime.api.oauth.metadata()).issuer })
</script>
