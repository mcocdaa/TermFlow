<template>
  <div v-if="authorization.open.value" class="dialog-backdrop">
    <section ref="panel" role="dialog" aria-modal="true" :aria-label="t('agent.reauth.title')" class="dialog-panel agent-reauth-dialog" @keydown="onKeydown">
      <header>
        <div>
          <p class="eyebrow">{{ t('settings.totp.securityCheckEyebrow') }}</p>
          <h2>{{ t('agent.reauth.title') }}</h2>
        </div>
        <button type="button" class="icon-button icon-only" :aria-label="t('common.close')" data-action="close-reauth-dialog" @click="cancel">
          <X :size="18" aria-hidden="true" />
        </button>
      </header>
      <p class="agent-reauth-dialog__hint">
        <ShieldCheck :size="16" aria-hidden="true" />
        <span v-if="native">{{ t('agent.reauth.nativeHint') }}</span>
        <span v-else>{{ t('agent.reauth.webHint') }}</span>
      </p>
      <form v-if="!native" class="security-form" @submit.prevent="submit">
        <template v-if="!challenge">
          <label for="reauth-root-credential">{{ t('agent.reauth.rootCredential') }}</label>
          <input id="reauth-root-credential" ref="credentialInput" v-model="credential" name="root_credential" type="password" autocomplete="off" required />
        </template>
        <template v-else>
          <label for="reauth-totp">{{ t('agent.reauth.totp') }}</label>
          <input id="reauth-totp" v-model="totp" name="totp" inputmode="numeric" autocomplete="one-time-code" required />
        </template>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <div class="dialog-actions">
          <button ref="cancelButton" type="button" class="secondary-button" :disabled="busy" @click="cancel">{{ t('common.cancel') }}</button>
          <button type="submit" class="primary-button" :disabled="busy">{{ busy ? t('settings.totp.verifying') : t('agent.reauth.verify') }}</button>
        </div>
      </form>
      <div v-else class="dialog-actions">
        <button ref="cancelButton" type="button" class="secondary-button" @click="cancel">{{ t('common.cancel') }}</button>
      </div>
    </section>
  </div>
</template>
<script setup lang="ts">
import { ShieldCheck, X } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from '../../i18n'
import { useClientRuntime } from '../../runtime'
import { useSensitiveAuthorization } from '../../composables/useSensitiveAuthorization'
const { t } = useI18n()
const runtime = useClientRuntime()
const authorization = useSensitiveAuthorization()
const native = computed(() => runtime.sensitiveAuthorization.mode === 'native-oauth')
const credential = ref(''), totp = ref(''), challenge = ref<string | null>(null), error = ref(''), busy = ref(false)
const panel = ref<HTMLElement | null>(null), credentialInput = ref<HTMLInputElement | null>(null), cancelButton = ref<HTMLButtonElement | null>(null)
let controller = new AbortController()
let previousFocus: HTMLElement | null = null
function clear() { credential.value = ''; totp.value = ''; challenge.value = null }
function cancel() { controller.abort(); clear(); authorization.finish(false) }
watch(authorization.open, async (open) => {
  clear(); error.value = ''
  if (open) { controller = new AbortController(); previousFocus = document.activeElement as HTMLElement; await nextTick(); (credentialInput.value ?? cancelButton.value)?.focus() }
  else { controller.abort(); await nextTick(); if (previousFocus?.isConnected) previousFocus.focus() }
})
async function submit() {
  if (native.value || busy.value) return
  busy.value = true; error.value = ''
  const signal = controller.signal
  try {
    const result = challenge.value
      ? await runtime.api.sessions.completeTotp(challenge.value, totp.value, signal)
      : await runtime.api.sessions.login(credential.value, signal)
    if (signal.aborted) return
    if ('status' in result) { challenge.value = result.challenge_id; await nextTick(); panel.value?.querySelector<HTMLInputElement>('input')?.focus() }
    else if (result.authenticated) { challenge.value = null; authorization.finish(true) }
  } catch { if (!signal.aborted) error.value = t('agent.reauth.failed') }
  finally { credential.value = ''; totp.value = ''; busy.value = false }
}
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); cancel() }
  if (event.key !== 'Tab') return
  const controls = [...(panel.value?.querySelectorAll<HTMLElement>('input, button:not(:disabled)') ?? [])]
  const first = controls[0], last = controls.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onBeforeUnmount(cancel)
</script>
