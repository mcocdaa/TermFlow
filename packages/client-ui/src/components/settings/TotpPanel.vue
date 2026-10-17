<template>
  <section class="settings-panel" aria-labelledby="totp-heading">
    <div class="settings-panel-heading">
      <div>
        <p class="eyebrow">{{ t('settings.totp.eyebrow') }}</p>
        <div class="settings-heading-label">
          <h2 id="totp-heading">{{ t('settings.totp.heading') }}</h2>
          <ContextHelp
            v-if="!loading && status.available && !status.configured"
            data-action="explain-totp"
            :label="t('settings.totp.helpLabel')"
            :text="t('settings.totp.helpText')"
          />
        </div>
      </div>
      <div v-if="!loading && status.configured" data-authenticator-actions class="settings-panel-heading-actions">
        <span class="status-chip">{{ t('settings.totp.bound') }}</span>
        <button data-action="reconfigure-totp" class="compact-secondary-button" type="button" @click="activate">{{ t('settings.totp.reconfigure') }}</button>
      </div>
      <span v-else-if="!loading" class="status-chip" data-status="disabled">{{ t('settings.totp.inactive') }}</span>
    </div>
    <p v-if="loading" role="status">{{ t('settings.totp.loading') }}</p>
    <template v-else-if="!status.available">
      <p class="settings-warning" role="status">{{ t('settings.totp.unavailable') }}</p>
    </template>
    <template v-else-if="!status.configured">
      <button data-action="activate-totp" class="primary-button settings-action-button" type="button" @click="activate">{{ t('settings.totp.activate') }}</button>
    </template>
    <template v-else>
      <div class="security-setting-row">
        <TotpProtectionLabel />
        <button
          ref="switchButton"
          class="toggle-switch"
          type="button"
          role="switch"
          :aria-checked="status.enabled"
          :aria-label="t('settings.totp.protectionLabel')"
          @click="openProtectionDialog"
        ><span aria-hidden="true" /></button>
      </div>
    </template>
    <TotpProtectionDialog
      :open="dialogOpen"
      :target-enabled="!status.enabled"
      :return-focus="switchButton"
      @close="dialogOpen = false"
      @confirmed="applyStatus"
    />
  </section>
</template>

<script setup lang="ts">
import type { TotpStatusResponse } from '@termflow/client-contracts'
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import ContextHelp from '../common/ContextHelp.vue'
import { useI18n } from '../../i18n'
import { useClientRuntime } from '../../runtime'
import TotpProtectionLabel from './TotpProtectionLabel.vue'
import TotpProtectionDialog from './TotpProtectionDialog.vue'

const { t } = useI18n()
const emit = defineEmits<{ changed: [TotpStatusResponse] }>()
const runtime = useClientRuntime()
const router = useRouter()
const status = reactive<TotpStatusResponse>({ configured: false, enabled: false, available: false })
const loading = ref(true)
const dialogOpen = ref(false)
const switchButton = ref<HTMLButtonElement | null>(null)

function applyStatus(next: TotpStatusResponse) {
  Object.assign(status, next)
  emit('changed', next)
}

function activate() {
  void router.push('/settings/two-factor-auth')
}

function openProtectionDialog() {
  dialogOpen.value = true
}

onMounted(async () => {
  try {
    applyStatus(await runtime.api.security.totpStatus())
  } catch {
    Object.assign(status, { configured: false, enabled: false, available: false })
  } finally {
    loading.value = false
  }
})
</script>
