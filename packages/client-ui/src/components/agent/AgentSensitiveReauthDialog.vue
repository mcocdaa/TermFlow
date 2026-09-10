<template>
  <div v-if="authorization.open.value" class="dialog-backdrop">
    <section ref="panel" role="dialog" aria-modal="true" aria-label="重新验证身份" class="dialog-panel" @keydown="onKeydown">
      <h2>重新验证身份</h2>
      <p v-if="native">请在系统浏览器中完成身份验证。</p>
      <form v-else @submit.prevent="submit">
        <label v-if="!challenge">Root 凭据<input ref="credentialInput" v-model="credential" name="root_credential" type="password" autocomplete="off" required /></label>
        <label v-else>动态验证码<input v-model="totp" name="totp" inputmode="numeric" autocomplete="off" required /></label>
        <p v-if="error" role="alert">{{ error }}</p>
        <button type="submit" :disabled="busy">验证</button>
      </form>
      <button ref="cancelButton" type="button" @click="cancel">取消</button>
    </section>
  </div>
</template>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useClientRuntime } from '../../runtime'
import { useSensitiveAuthorization } from '../../composables/useSensitiveAuthorization'
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
  } catch { if (!signal.aborted) error.value = '验证失败，请重试。' }
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
