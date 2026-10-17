<template>
  <div v-if="open" class="dialog-backdrop" @click.self="close">
    <section class="dialog-panel protection-dialog" role="dialog" aria-modal="true" aria-labelledby="protection-dialog-title" @keydown="onKeydown">
      <header>
        <div>
          <p class="eyebrow">{{ t('settings.totp.securityCheckEyebrow') }}</p>
          <h2 id="protection-dialog-title">{{ targetEnabled ? t('settings.totp.protectionLabel') : t('settings.totp.disableTitle') }}</h2>
        </div>
        <button data-action="close-protection-dialog" class="icon-button icon-only" type="button" :aria-label="t('common.close')" @click="close"><X :size="18" aria-hidden="true" /></button>
      </header>
      <form class="security-form" @submit.prevent="submit">
        <label for="protection-admin-token">{{ t('settings.totp.adminToken') }}</label>
        <input id="protection-admin-token" ref="adminInput" v-model="adminToken" name="admin-token" type="password" autocomplete="off" required />
        <label for="protection-totp-code">{{ t('settings.totp.code') }}</label>
        <input id="protection-totp-code" v-model="code" name="totp-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
        <p v-if="message" class="form-error" role="alert">{{ message }}</p>
        <div class="dialog-actions">
          <button class="secondary-button" type="button" :disabled="busy" @click="close">{{ t('common.cancel') }}</button>
          <button class="primary-button" type="submit" :disabled="busy">{{ busy ? t('settings.totp.verifying') : t('common.confirm') }}</button>
        </div>
      </form>
    </section>
  </div>
</template>

<script setup lang="ts">
import type { TotpStatusResponse } from '@termflow/client-contracts'
import { X } from '@lucide/vue'
import { nextTick, ref, watch } from 'vue'
import { useI18n } from '../../i18n'
import { useClientRuntime } from '../../runtime'

const { t } = useI18n()
const props = defineProps<{
  open: boolean
  targetEnabled: boolean
  returnFocus?: HTMLElement | null
}>()
const emit = defineEmits<{ close: []; confirmed: [TotpStatusResponse] }>()
const runtime = useClientRuntime()
const adminToken = ref('')
const code = ref('')
const message = ref('')
const busy = ref(false)
const adminInput = ref<HTMLInputElement | null>(null)

function clearSecrets() {
  adminToken.value = ''
  code.value = ''
  message.value = ''
}

function restoreFocus() {
  void nextTick(() => {
    if (props.returnFocus?.isConnected) props.returnFocus.focus()
  })
}

function close() {
  clearSecrets()
  emit('close')
  restoreFocus()
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  event.preventDefault()
  close()
}

async function submit() {
  if (busy.value) return
  busy.value = true
  message.value = ''
  try {
    const credentials = { adminToken: adminToken.value, totpCode: code.value }
    const status = props.targetEnabled
      ? await runtime.api.security.enableTotpProtection(credentials)
      : await runtime.api.security.disableTotpProtection(credentials)
    clearSecrets()
    emit('confirmed', status)
    emit('close')
    restoreFocus()
  } catch {
    message.value = t('settings.totp.verifyFailed')
  } finally {
    busy.value = false
  }
}

watch(() => props.open, (open) => {
  if (open) void nextTick(() => adminInput.value?.focus())
  else clearSecrets()
}, { immediate: true })
</script>
