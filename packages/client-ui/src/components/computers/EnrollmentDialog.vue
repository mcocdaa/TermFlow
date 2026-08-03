<template>
  <div v-if="open" class="dialog-backdrop" @click.self="close">
    <section ref="panel" class="dialog-panel" role="dialog" aria-modal="true" aria-labelledby="enrollment-title" @keydown="onDialogKeydown">
      <header>
        <h2 id="enrollment-title">添加电脑</h2>
        <button data-action="close-enrollment" class="icon-button icon-only" type="button" aria-label="关闭" @click="close"><X :size="18" aria-hidden="true" /></button>
      </header>
      <p v-if="message" role="alert" class="form-error">{{ message }}</p>
      <form v-if="!code" class="enrollment-create-form" @submit.prevent="create()">
        <label for="enrollment-computer-name">电脑名称</label>
        <input id="enrollment-computer-name" ref="nameInput" v-model="displayName" name="computer-name" type="text" maxlength="128" placeholder="输入电脑名称" autocomplete="off" required />
        <button data-action="create-code" class="primary-button" type="submit" :disabled="busy || !displayName">{{ busy ? '正在创建…' : '创建' }}</button>
      </form>
      <div v-else class="enrollment-secret">
        <p class="warning-text">此注册码只显示一次，将在 {{ secondsRemaining }} 秒后过期并自动刷新。</p>
        <section data-enrollment-field="code" class="enrollment-field">
          <h3>注册码</h3>
          <output class="registration-code" aria-label="一次性注册码">{{ code }}</output>
        </section>
        <section data-enrollment-field="command" class="enrollment-field">
          <div class="enrollment-field-heading">
            <h3>终端执行</h3>
            <span class="help-tooltip">
              <button data-help="login-command" class="icon-button icon-only" type="button" aria-label="终端执行说明" aria-describedby="login-command-help" title="复制到安装有 TermFlow 的电脑上，在终端中执行">
                <CircleHelp :size="17" aria-hidden="true" />
              </button>
              <span id="login-command-help" role="tooltip">复制到安装有 TermFlow 的电脑上，在终端中执行。</span>
            </span>
          </div>
          <code class="login-command">{{ command }}</code>
        </section>
        <button ref="copyButton" data-action="copy-command" class="primary-button enrollment-copy-button" type="button" @click="copy"><Copy :size="17" aria-hidden="true" />{{ copied ? '已复制' : '复制命令' }}</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { CircleHelp, Copy, X } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError } from '@termflow/client-core'
import { useClientRuntime } from '../../runtime'

const emit = defineEmits<{ closed: []; added: [] }>()
const runtime = useClientRuntime()
const open = ref(true)
const busy = ref(false)
const copied = ref(false)
const displayName = ref('')
const code = ref<string | null>(null)
const command = ref('')
const expiresAt = ref<number | null>(null)
const now = ref(runtime.clock.now())
const message = ref('')
const panel = ref<HTMLElement | null>(null)
const nameInput = ref<HTMLInputElement | null>(null)
const copyButton = ref<HTMLButtonElement | null>(null)
const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
let timer: unknown | null = null
let enrollmentPollTimer: unknown | null = null
let pollingEnrollment = false
let baselineComputerIds = new Set<string>()

const secondsRemaining = computed(() => expiresAt.value === null ? 0 : Math.max(0, Math.ceil((expiresAt.value - now.value) / 1000)))
function stopTimer() { if (timer !== null) runtime.clock.clearInterval(timer); timer = null }
function stopEnrollmentPolling() { if (enrollmentPollTimer !== null) runtime.clock.clearInterval(enrollmentPollTimer); enrollmentPollTimer = null }
function clearSecret() { code.value = null; command.value = ''; expiresAt.value = null; copied.value = false; stopTimer(); stopEnrollmentPolling() }
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
  timer = runtime.clock.setInterval(() => {
    now.value = runtime.clock.now()
    if (secondsRemaining.value > 0) return
    clearSecret()
    if (open.value) void create(true)
  }, 250)
}
async function pollForEnrollment() {
  if (pollingEnrollment || !open.value || !code.value) return
  pollingEnrollment = true
  try {
    const result = await runtime.api.computers.list()
    if (!open.value || !code.value) return
    const added = result.computers.find((computer) => !baselineComputerIds.has(computer.installation_id) && computer.display_name === displayName.value)
    if (added === undefined) return
    clearSecret()
    open.value = false
    emit('added')
    void nextTick(() => { if (returnFocus?.isConnected) returnFocus.focus() })
  } catch (error) { if (open.value) message.value = error instanceof ApiError ? error.message : '无法检查电脑是否已添加。' }
  finally { pollingEnrollment = false }
}
function startEnrollmentPolling() {
  stopEnrollmentPolling()
  enrollmentPollTimer = runtime.clock.setInterval(() => { void pollForEnrollment() }, 1000)
}
function validName(value: string) {
  return value.length >= 1 && value.length <= 128 && !/[\u0000-\u001f\u007f-\u009f]/.test(value)
}
async function create(automatic = false) {
  if (busy.value || !open.value) return
  if (!validName(displayName.value)) {
    message.value = '电脑名称须为 1 至 128 个无控制字符的字符。'
    return
  }
  busy.value = true
  message.value = ''
  try {
    const baseline = await runtime.api.computers.list()
    if (!open.value) return
    baselineComputerIds = new Set(baseline.computers.map((computer) => computer.installation_id))
    const enrollment = await runtime.api.computers.createEnrollment(displayName.value)
    if (!open.value) return
    code.value = enrollment.token
    command.value = enrollment.login_command
    expiresAt.value = new Date(enrollment.expires_at).getTime()
    now.value = runtime.clock.now()
    copied.value = false
    startTimer()
    startEnrollmentPolling()
    if (!automatic) void nextTick(() => copyButton.value?.focus())
  } catch (error) { message.value = error instanceof ApiError ? error.message : automatic ? '注册码刷新失败，请手动重试。' : '无法创建注册码。' }
  finally { busy.value = false }
}
async function copy() { if (!command.value) return; await runtime.clipboard.writeText(command.value); copied.value = true }
onMounted(() => nameInput.value?.focus())
onBeforeUnmount(() => { open.value = false; clearSecret(); if (returnFocus?.isConnected) returnFocus.focus() })
</script>
