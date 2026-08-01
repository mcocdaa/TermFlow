<template>
  <div v-if="open" class="dialog-backdrop" @click.self="close">
    <section ref="panel" class="dialog-panel" role="dialog" aria-modal="true" aria-labelledby="enrollment-title" @keydown="onDialogKeydown">
      <header>
        <div><p class="eyebrow">一次性注册</p><h2 id="enrollment-title">添加 Computer</h2></div>
        <button data-action="close-enrollment" class="icon-button icon-only" type="button" aria-label="关闭" @click="close"><X :size="18" aria-hidden="true" /></button>
      </header>
      <p v-if="message" role="alert" class="form-error">{{ message }}</p>
      <button v-if="!code" data-action="create-code" class="primary-button" type="button" :disabled="busy" @click="create()">{{ busy ? '正在创建…' : '创建一次性注册码' }}</button>
      <div v-else class="enrollment-secret">
        <p class="warning-text">此注册码只显示一次，将在 {{ secondsRemaining }} 秒后过期并自动刷新。</p>
        <section data-enrollment-field="code" class="enrollment-field">
          <h3>注册码</h3>
          <output class="registration-code" aria-label="一次性注册码">{{ code }}</output>
        </section>
        <section data-enrollment-field="command" class="enrollment-field">
          <div class="enrollment-field-heading">
            <h3>终端执行命令</h3>
            <span class="help-tooltip">
              <button data-help="login-command" class="icon-button icon-only" type="button" aria-label="终端执行命令说明" aria-describedby="login-command-help" title="复制到安装有 TermFlow 的电脑上，在终端中执行">
                <CircleHelp :size="17" aria-hidden="true" />
              </button>
              <span id="login-command-help" role="tooltip">复制到安装有 TermFlow 的电脑上，在终端中执行。</span>
            </span>
          </div>
          <code class="login-command">{{ command }}</code>
        </section>
        <button data-action="copy-command" class="primary-button enrollment-copy-button" type="button" @click="copy"><Copy :size="17" aria-hidden="true" />{{ copied ? '已复制' : '复制命令' }}</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { CircleHelp, Copy, X } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { createEnrollmentCode } from '../../api/computers'
import { ApiError } from '../../api/http'

const emit = defineEmits<{ closed: [] }>()
const open = ref(true)
const busy = ref(false)
const copied = ref(false)
const code = ref<string | null>(null)
const expiresAt = ref<number | null>(null)
const now = ref(Date.now())
const message = ref('')
const panel = ref<HTMLElement | null>(null)
const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
let timer: ReturnType<typeof setInterval> | null = null

const secondsRemaining = computed(() => expiresAt.value === null ? 0 : Math.max(0, Math.ceil((expiresAt.value - now.value) / 1000)))
const command = computed(() => code.value ? `termflow login --server ${window.location.origin} --code ${code.value}` : '')

function stopTimer() { if (timer !== null) clearInterval(timer); timer = null }
function clearSecret() { code.value = null; expiresAt.value = null; copied.value = false; stopTimer() }
function close() {
  clearSecret()
  open.value = false
  emit('closed')
  void nextTick(() => { if (returnFocus?.isConnected) returnFocus.focus() })
}
function focusableElements() {
  return [...(panel.value?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])]
}
function onDialogKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); close(); return }
  if (event.key !== 'Tab') return
  const focusable = focusableElements()
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if ((!event.shiftKey && document.activeElement === last) || (event.shiftKey && document.activeElement === first)) {
    event.preventDefault()
    ;(event.shiftKey ? last : first)?.focus()
  }
}
function startTimer() {
  stopTimer()
  timer = setInterval(() => {
    now.value = Date.now()
    if (secondsRemaining.value > 0) return
    clearSecret()
    if (open.value) void create(true)
  }, 250)
}
async function create(automatic = false) {
  if (busy.value || !open.value) return
  busy.value = true
  message.value = ''
  try {
    const enrollment = await createEnrollmentCode()
    if (!open.value) return
    code.value = enrollment.token
    expiresAt.value = new Date(enrollment.expires_at).getTime()
    now.value = Date.now()
    copied.value = false
    startTimer()
  } catch (error) { message.value = error instanceof ApiError ? error.message : automatic ? '注册码刷新失败，请手动重试。' : '无法创建注册码。' }
  finally { busy.value = false }
}
async function copy() { if (!command.value) return; await navigator.clipboard.writeText(command.value); copied.value = true }
onMounted(() => focusableElements()[0]?.focus())
onBeforeUnmount(() => { clearSecret(); if (returnFocus?.isConnected) returnFocus.focus() })
</script>
