<template>
  <div class="page settings-page totp-activation-view">
    <header class="page-heading totp-guide-heading">
      <div class="totp-guide-heading-copy"><p class="eyebrow">Two Factor Authentication</p><h1>{{ t('settings.totp.heading') }}</h1></div>
      <RouterLink class="secondary-button" to="/settings">{{ t('settings.totp.backToSettings') }}</RouterLink>
    </header>
    <ol v-if="!loading && status.available" class="totp-guide-steps" :aria-label="t('settings.totp.stepsAria')">
      <li
        v-for="(step, index) in guideSteps"
        :key="index"
        data-guide-step
        :data-state="guideStepState(index + 1)"
        :aria-current="guideStepState(index + 1) === 'current' ? 'step' : undefined"
      >
        <span class="totp-guide-step-marker" aria-hidden="true">{{ guideStepState(index + 1) === 'complete' ? '✓' : index + 1 }}</span>
        <span>{{ step }}</span>
      </li>
    </ol>
    <section class="settings-panel totp-guide-card" aria-live="polite">
      <p v-if="loading" role="status">{{ t('settings.totp.loading') }}</p>
      <p v-else-if="!status.available" class="settings-warning" role="status">{{ t('settings.totp.unavailable') }}</p>
      <template v-else>
        <header class="totp-wizard-card-heading">
          <h2 data-wizard-card-title>{{ wizardTitle }}</h2>
          <span data-wizard-progress>{{ wizardComplete ? t('settings.totp.wizardComplete') : t('settings.totp.wizardProgress', { step: currentStep }) }}</span>
        </header>
        <template v-if="setup">
          <div data-totp-bind-layout class="totp-bind-layout">
            <div class="totp-bind-qr">
              <ThemedQrCode :value="setup.provisioning_uri" :alt="t('settings.totp.qrAlt')" />
              <SetupKeyPopover
                :setup-key="setup.setup_key"
                :copied="setupKeyCopied"
                @copy="copySetupKey"
              />
            </div>
            <form data-action="confirm-totp-setup" class="inline-security-form totp-confirm-form" @submit.prevent="confirmSetup">
              <label for="activation-confirm-code">{{ t('settings.totp.confirmCodeLabel') }}</label>
              <input id="activation-confirm-code" v-model="confirmCode" name="setup-confirm-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
              <p v-if="message" class="form-error" role="alert">{{ message }}</p>
              <button class="primary-button" type="submit" :disabled="busy">{{ t('settings.totp.confirmBind') }}</button>
            </form>
          </div>
        </template>
        <template v-else-if="!status.configured || reconfiguring">
          <form data-action="begin-totp-setup" class="security-form" @submit.prevent="beginSetup">
            <label for="activation-admin-token">{{ t('settings.totp.adminToken') }}</label>
            <input id="activation-admin-token" v-model="adminToken" name="setup-admin-token" type="password" autocomplete="off" required />
            <template v-if="status.configured">
              <label for="activation-current-code">{{ t('settings.totp.currentCodeLabel') }}</label>
              <input id="activation-current-code" v-model="currentCode" name="setup-current-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
            </template>
            <p v-if="message" class="form-error" role="alert">{{ message }}</p>
            <button class="primary-button" type="submit" :disabled="busy">{{ t('common.continue') }}</button>
          </form>
        </template>
        <template v-else>
          <div data-configured-authenticator-heading class="configured-authenticator-heading">
            <strong>{{ t('settings.totp.bound') }}</strong>
            <button data-action="reconfigure-totp" class="compact-secondary-button" type="button" @click="reconfiguring = true">{{ t('settings.totp.reconfigure') }}</button>
          </div>
          <div class="security-setting-row">
            <TotpProtectionLabel />
            <button ref="switchButton" class="toggle-switch" type="button" role="switch" :aria-checked="status.enabled" :aria-label="t('settings.totp.protectionLabel')" @click="dialogOpen = true"><span aria-hidden="true" /></button>
          </div>
        </template>
      </template>
    </section>
    <TotpProtectionDialog
      :open="dialogOpen"
      :target-enabled="!status.enabled"
      :return-focus="switchButton"
      @close="dialogOpen = false"
      @confirmed="applyStatus"
    />
  </div>
</template>

<script setup lang="ts">
import type { TotpSetupResponse, TotpStatusResponse } from '@termflow/client-contracts'
import { computed, onMounted, reactive, ref } from 'vue'
import ThemedQrCode from '../components/common/ThemedQrCode.vue'
import SetupKeyPopover from '../components/settings/SetupKeyPopover.vue'
import TotpProtectionLabel from '../components/settings/TotpProtectionLabel.vue'
import TotpProtectionDialog from '../components/settings/TotpProtectionDialog.vue'
import { useI18n } from '../i18n'
import { useClientRuntime } from '../runtime'

const { t } = useI18n()
const runtime = useClientRuntime()
const guideSteps = computed(() => [t('settings.totp.step1'), t('settings.totp.step2'), t('settings.totp.step3')] as const)
const status = reactive<TotpStatusResponse>({ configured: false, enabled: false, available: false })
const loading = ref(true)
const busy = ref(false)
const reconfiguring = ref(false)
const setup = ref<TotpSetupResponse | null>(null)
const adminToken = ref('')
const currentCode = ref('')
const confirmCode = ref('')
const message = ref('')
const setupKeyCopied = ref(false)
const dialogOpen = ref(false)
const switchButton = ref<HTMLButtonElement | null>(null)

const currentStep = computed<1 | 2 | 3>(() => {
  if (setup.value) return 2
  if (status.configured && !reconfiguring.value) return 3
  return 1
})
const wizardComplete = computed(() => status.enabled && !reconfiguring.value && setup.value === null)
const wizardTitle = computed(() => {
  if (setup.value) return t('settings.totp.wizardTitle.bind')
  if (status.configured && !reconfiguring.value) return status.enabled ? t('settings.totp.wizardTitle.enabled') : t('settings.totp.wizardTitle.enableProtection')
  return status.configured ? t('settings.totp.wizardTitle.reconfigure') : t('settings.totp.wizardTitle.verifyAdmin')
})

function guideStepState(index: number) {
  if (wizardComplete.value || index < currentStep.value) return 'complete'
  return index === currentStep.value ? 'current' : 'upcoming'
}

function applyStatus(next: TotpStatusResponse) {
  Object.assign(status, next)
  reconfiguring.value = false
}

async function copySetupKey() {
  if (!setup.value) return
  await runtime.clipboard.writeText(setup.value.setup_key)
  setupKeyCopied.value = true
}

async function beginSetup() {
  if (busy.value) return
  busy.value = true
  message.value = ''
  try {
    setup.value = await runtime.api.security.createTotpSetup({
      adminToken: adminToken.value,
      ...(status.configured ? { totpCode: currentCode.value } : {}),
    })
    setupKeyCopied.value = false
  } catch {
    message.value = t('settings.totp.verifyFailed')
  } finally {
    adminToken.value = ''
    currentCode.value = ''
    busy.value = false
  }
}

async function confirmSetup() {
  if (busy.value || setup.value === null) return
  busy.value = true
  message.value = ''
  try {
    const next = await runtime.api.security.confirmTotpSetup(setup.value.setup_id, confirmCode.value)
    applyStatus(next)
    setup.value = null
    setupKeyCopied.value = false
  } catch {
    message.value = t('settings.totp.codeInvalidOrExpired')
  } finally {
    confirmCode.value = ''
    busy.value = false
  }
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
