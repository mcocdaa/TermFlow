<template>
  <section class="login-view">
    <div class="auth-card">
      <h1>登录</h1>
      <form @submit.prevent="submit">
        <template v-if="challengeId === null">
          <label for="admin-token">管理员令牌</label>
          <input id="admin-token" v-model="adminToken" type="password" autocomplete="off" required autofocus />
        </template>
        <template v-else>
          <label for="totp-code">双重验证码<span class="totp-hint">（请输入验证器应用当前显示的 6 位验证码。）</span></label>
          <input id="totp-code" ref="totpInput" v-model="totpCode" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
        </template>
        <p v-if="message" role="alert" class="form-error">{{ message }}</p>
        <div class="dialog-actions"><button class="primary-button" type="submit" :disabled="busy">{{ busy ? '正在验证…' : challengeId === null ? '登录' : '验证并登录' }}</button></div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '@termflow/client-core'
import { useSession } from '../composables/useSession'

const adminToken = ref('')
const totpCode = ref('')
const challengeId = ref<string | null>(null)
const totpInput = ref<HTMLInputElement | null>(null)
const busy = ref(false)
const message = ref('')
const route = useRoute()
const router = useRouter()
const { loginWithToken, completeTotp } = useSession()

async function submit() {
  if (busy.value || (challengeId.value === null ? !adminToken.value : !/^[0-9]{6}$/.test(totpCode.value))) return
  busy.value = true
  message.value = ''
  try {
    if (challengeId.value === null) {
      const result = await loginWithToken(adminToken.value)
      adminToken.value = ''
      if ('status' in result) {
        challengeId.value = result.challenge_id
        await nextTick()
        totpInput.value?.focus()
        return
      }
    } else {
      await completeTotp(totpCode.value)
      totpCode.value = ''
    }
    const requested = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(requested.startsWith('/') && !requested.startsWith('//') ? requested : '/')
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : '登录失败，请重试。'
  } finally {
    adminToken.value = ''
    totpCode.value = ''
    busy.value = false
  }
}
</script>

<style scoped>
.totp-hint { color: var(--color-text-muted); font-weight: 400; font-size: 0.85em; }
</style>
