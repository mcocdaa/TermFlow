<template>
  <div v-if="open" class="dialog-backdrop" @click.self="close">
    <section class="dialog-panel" role="dialog" aria-modal="true" aria-labelledby="enrollment-title">
      <header><div><p class="eyebrow">一次性注册</p><h2 id="enrollment-title">添加 Computer</h2></div><button data-action="close-enrollment" class="icon-button" type="button" aria-label="关闭" @click="close">关闭</button></header>
      <p>一次 <code>termflow login</code> 代表一台 Computer；随后运行 <code>termflow new --name NAME</code> 可创建相互独立的 Terms。</p>
      <p v-if="message" role="alert" class="form-error">{{ message }}</p>
      <button v-if="!code" data-action="create-code" class="primary-button" type="button" :disabled="busy" @click="create">{{ busy ? '正在创建…' : '创建一次性注册码' }}</button>
      <div v-else class="enrollment-secret">
        <p class="warning-text">此注册码只显示一次，将在 {{ secondsRemaining }} 秒后过期。</p>
        <output class="registration-code" aria-label="一次性注册码">{{ code }}</output>
        <code class="login-command">{{ command }}</code>
        <button data-action="copy-command" class="primary-button" type="button" @click="copy">{{ copied ? '已复制' : '复制登录命令' }}</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
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
let timer: ReturnType<typeof setInterval> | null = null

const secondsRemaining = computed(() => expiresAt.value === null ? 0 : Math.max(0, Math.ceil((expiresAt.value - now.value) / 1000)))
const command = computed(() => code.value ? `termflow login --server ${window.location.origin} --code ${code.value}` : '')

function clearSecret() { code.value = null; expiresAt.value = null; copied.value = false; if (timer !== null) clearInterval(timer); timer = null }
function close() { clearSecret(); open.value = false; emit('closed') }
async function create() {
  busy.value = true
  message.value = ''
  try {
    const enrollment = await createEnrollmentCode()
    code.value = enrollment.code
    expiresAt.value = new Date(enrollment.expires_at).getTime()
    now.value = Date.now()
    timer = setInterval(() => {
      now.value = Date.now()
      if (secondsRemaining.value <= 0) clearSecret()
    }, 250)
  } catch (error) { message.value = error instanceof ApiError ? error.message : '无法创建注册码。' }
  finally { busy.value = false }
}
async function copy() { if (!command.value) return; await navigator.clipboard.writeText(command.value); copied.value = true }
onBeforeUnmount(clearSecret)
</script>
